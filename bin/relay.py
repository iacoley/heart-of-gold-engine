#!/usr/bin/env python3
"""
Karakos Relay — Discord + Dispatch + Capture

Adapters:
- DiscordAdapter: Routes Discord messages to agent server
- DispatchAdapter: Watches inbox dirs, invokes builder/reviewer
- CaptureAdapter: Persists Discord messages to JSONL
"""

import asyncio
import discord
import fcntl
import functools
import json
import logging
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
from logging.handlers import RotatingFileHandler

from reply_gate import Decision, GateMessage, ReplyGate, SCORER_PROMPT
from handoff import parse_handoff, required_but_misdirected
from outbox import add_pending
import context_box
import speaking_banana as banana

# =============================================================================
# Graceful shutdown / in-flight tracking
# =============================================================================
# 2026-08-11: Amos flagged a real gap in reload-on-commit.py's relay bounce —
# a change to bin/relay.py or bin/reply_gate.py sends this process SIGTERM
# with no grace period, so a commit landing mid-handler (an in-flight
# Discord reply, a /sys command response, a dispatch to agent-server) can be
# torn down mid-flight and silently lost. bin/agent-server.py already has an
# equivalent gap covered by simply being excluded from auto-bounce entirely
# (see reload-on-commit.py's SELF_PROCESS_WARN) — that option isn't
# available here since relay *needs* to pick up its own code changes.
#
# Fix: track how many handler coroutines are currently running
# (_INFLIGHT_COUNT, safe as a plain int — asyncio is single-threaded, no
# handler can preempt another's read-modify-write of it), and on SIGTERM,
# drain up to GRACEFUL_SHUTDOWN_TIMEOUT_SEC before actually closing the
# client, instead of dying instantly. This is "idle-gating" done from the
# inside (the process finishing its own work) rather than an external
# fixed sleep guessing how long that takes — reload-on-commit.py's kill
# doesn't need to know; it just signals and moves on.
GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 10
_INFLIGHT_COUNT = 0


def _inflight_tracked(fn):
    """Decorator for Discord-facing handler entry points (on_message, slash
    command callbacks). Increments/decrements the module-level in-flight
    counter around the full handler body so a SIGTERM shutdown knows
    whether it's safe to close the client yet."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        global _INFLIGHT_COUNT
        _INFLIGHT_COUNT += 1
        try:
            return await fn(*args, **kwargs)
        finally:
            _INFLIGHT_COUNT -= 1
    return wrapper


async def _graceful_shutdown(discord_client) -> None:
    """SIGTERM handler: wait for in-flight handlers to finish (bounded),
    then close the client so main()'s await returns and the process exits
    cleanly for supervisor's autorestart to pick up. Never blocks forever —
    a stuck handler gets logged and overridden, not allowed to wedge a
    restart indefinitely."""
    log.warning(
        "relay: SIGTERM received, %d handler(s) in flight — draining up to %ss "
        "before shutdown", _INFLIGHT_COUNT, GRACEFUL_SHUTDOWN_TIMEOUT_SEC,
    )
    deadline = time.monotonic() + GRACEFUL_SHUTDOWN_TIMEOUT_SEC
    while _INFLIGHT_COUNT > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if _INFLIGHT_COUNT > 0:
        log.warning(
            "relay: shutdown proceeding with %d handler(s) still in flight "
            "after %ss grace period — that work is being dropped, not waited "
            "on further", _INFLIGHT_COUNT, GRACEFUL_SHUTDOWN_TIMEOUT_SEC,
        )
    else:
        log.info("relay: drained cleanly, closing client")
    await discord_client.close()

# =============================================================================
# Utilities
# =============================================================================

def split_discord_message(text: str, max_length: int = 2000) -> List[str]:
    """Split text into chunks Discord will accept (max 2000 chars each).

    Splits on the largest boundary that fits — paragraph, then line, then a
    hard cut mid-line. The hard cut is the part that matters: a reply with no
    blank line and no newline in it has no boundary to split on, and the
    previous implementation returned it as a single oversize chunk. Discord
    rejects anything over 2000 with a 400 and the message is lost.
    """
    if len(text) <= max_length:
        return [text] if text else []

    chunks: List[str] = []
    remaining = text

    while len(remaining) > max_length:
        window = remaining[:max_length]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            # A solid wall of text. Cut it at the limit rather than handing
            # Discord something it will refuse.
            cut = max_length
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks if chunks else [text]


# =============================================================================
# Attachments (2026-08-09, ported from mcarmody/karakos-package#127)
# =============================================================================

# Anything outside this set is replaced. That covers `/` and `\` — an
# uploader controls the filename, and a name like `../../config/agents.json`
# must not be able to choose where the relay writes.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_attachment_name(filename: str, index: int) -> str:
    """Return a filesystem-safe name for a Discord-supplied filename.

    The index prefix is not decoration: two attachments on one message may
    share a filename, and without it the second silently overwrites the
    first and the agent is handed the same bytes twice.
    """
    cleaned = _UNSAFE_FILENAME_RE.sub("_", filename or "")
    # Leading dots are stripped so a name of `..` or `.` cannot survive as a
    # path component, and so uploads do not land as dotfiles.
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        cleaned = "attachment"
    # Long names are truncated from the front, keeping the tail so the
    # extension (which is how the agent knows it is an image) survives.
    if len(cleaned) > 96:
        cleaned = cleaned[-96:]
    return f"{index}-{cleaned}"

# =============================================================================
# Configuration
# =============================================================================

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.json"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
MESSAGES_DIR = WORKSPACE_ROOT / "data" / "messages"
ATTACHMENTS_DIR = WORKSPACE_ROOT / "data" / "attachments"
HEALTH_FILE = WORKSPACE_ROOT / "data" / "health" / "relay.json"
PRESENCE_FILE = WORKSPACE_ROOT / "data" / "presence.json"

# Self-presence: the mirror image of PRESENCE_FILE above. That one is
# relay reading *other* members' presence (Amos et al); this one is the
# set-status skill (run from agent-server, a different process than the
# one holding the Discord websocket) writing what Marvin's own presence
# should be, for relay to actually apply. See skills/set-status/.
STATUS_FILE = WORKSPACE_ROOT / "data" / "status" / "marvin.json"
STATUS_POLL_INTERVAL_SEC = int(os.environ.get("STATUS_POLL_INTERVAL_SEC", "10"))
_STATUS_TO_DISCORD = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
}

# Attachments the relay will pull down before handing a message to an agent
# (2026-08-09, ported from mcarmody/karakos-package#127 — posting an image
# with "what's in this" got answered about the text only, the file was
# never downloaded or described). Discord's own ceiling is 25 MB on an
# unboosted server, so the default cap refuses nothing Discord would have
# accepted while still bounding what a single message can write to the
# data volume.
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("MAX_ATTACHMENTS_PER_MESSAGE", "10"))

# Retry-spooling for messages the agent server rejects or can't be reached
# for (Task #9, built 2026-08-06). Before this, send_to_agent_server()
# logged a non-202/exception and dropped the message — confirmed as a
# real, live failure (Marvin lost a message to a 429 on 2026-08-05, no
# retry, no notice) and matches upstream karakos-package issue #88, which
# names this install as the reproduction case. Shape (spool-and-retry,
# not spool-forever) follows the pattern Amos described for his own
# `poke-amos.sh` — not his source, ported from the description only.
DEFERRED_POKE_DIR = WORKSPACE_ROOT / "data" / "deferred-pokes"
DEFERRED_POKE_DEAD_DIR = DEFERRED_POKE_DIR / "dead"
DEFERRED_POKE_FLUSH_INTERVAL_SEC = 30
DEFERRED_POKE_MAX_AGE_SEC = 24 * 3600  # give up and move to dead/ after this

AGENT_SERVER_PORT = os.environ.get("AGENT_SERVER_PORT", "18791")
AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", f"http://localhost:{AGENT_SERVER_PORT}")
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
OWNER_DISCORD_ID = int(os.environ.get("OWNER_DISCORD_ID", "0"))

# `/sys restart-server` (design: agents/Marvin/journal/sys-restart-server-
# spec-2026-08-16.md, decided 2026-08-16). SAFE_PKILL is the same script
# recovery-agent.py already uses for its own autonomous agent-server
# bounce — one source of truth for "how do we bounce agent-server.py"
# rather than two scripts that can drift.
SAFE_PKILL = WORKSPACE_ROOT / "bin" / "safe-pkill.sh"
RESTART_SERVER_CONFIRM_WINDOW_SEC = 30  # second invocation within this window actually acts
RESTART_SERVER_COOLDOWN_SEC = 60  # guards a fat-fingered double-SIGTERM into one RestartSec=2 window
# How long to wait for every agent to go IDLE before giving up. Ian,
# 2026-08-16 19:50 #general ("Escalate and stop"): no force-past-ceiling
# variant like Amos's — on timeout this posts to #signals (with the
# required direct ping, see facts/signals-decisions-need-a-direct-ping-
# 2026-08-16.md) and stops. A human decides whether to force it from there.
RESTART_SERVER_IDLE_WAIT_TIMEOUT_SEC = int(os.environ.get("RESTART_SERVER_IDLE_WAIT_TIMEOUT_SEC", "300"))
RESTART_SERVER_STATUS_INTERVAL_SEC = 30  # interim "still waiting, won't force it" pushes while idle-waiting
# Bound on polling for agent-server to come back after SIGTERM. Was 30 —
# too tight even before 2026-08-30's boot_id fix: graceful_shutdown() alone
# can run up to ~30s idle-wait plus up to 25s PER AGENT for session
# summaries (sequential, not parallel) before it even starts dying, and
# journalctl shows real SIGTERM-to-"Running on http" gaps of ~25-31s on
# this install with just two agents. The old code never noticed because it
# accepted the first (falsely-positive) health response in ~1s, well
# inside any timeout. Now that a real restart is required to satisfy this
# poll, the bound needs real headroom instead of getting lucky — 120s
# covers the measured worst case with margin before this starts reporting
# a false crash-loop.
RESTART_SERVER_POST_RESTART_TIMEOUT_SEC = 120

# Dispatch config
DISPATCH_INBOX_DIR = WORKSPACE_ROOT / "inbox"
DISPATCH_POLL_INTERVAL = 30
MAX_CONCURRENT_BUILDERS = int(os.environ.get("MAX_CONCURRENT_BUILDERS", "1"))
MAX_CONCURRENT_REVIEWERS = int(os.environ.get("MAX_CONCURRENT_REVIEWERS", "2"))
DISPATCH_TIMEOUTS = {
    "reviewer": 3600,    # 1 hour
    "builder": 21600,    # 6 hours
}

# Logging
log = logging.getLogger("relay")
log.setLevel(logging.INFO)
# Guard against duplicate handlers — same reasoning and same 2026-08-29
# incident as agent-server.py's identical guard, see that comment for
# the full writeup. KARAKOS_LOG_DIR mirrors agent-server.py's override.
if not log.handlers:
    log_dir = Path(os.environ.get("KARAKOS_LOG_DIR", str(WORKSPACE_ROOT / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "relay.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(console)

    # Modules relay.py imports and calls in-process (handoff, banana) each
    # define their own named logger (logging.getLogger(__name__) /
    # getLogger("banana")) but were never wired to a handler -- same class
    # of bug DISCORD_SERVER_ID_FIX.md already diagnosed once for discord.py's
    # own logger: with no handler anywhere in the chain, messages don't
    # error, they just vanish. Confirmed live 2026-08-30: a real
    # "unrecognized context_box.state" warning from handoff.py never
    # reached relay.log, only turned up in the raw systemd journal (a
    # logging.Handler-of-last-resort artifact, not a real log destination
    # anyone watches). Wiring both into the same handlers "relay" already
    # uses, rather than a separate file, keeps one log to check instead of
    # a growing list of module-specific ones.
    for _extra_logger_name in ("handoff", "banana"):
        _extra_log = logging.getLogger(_extra_logger_name)
        _extra_log.setLevel(logging.INFO)
        if not _extra_log.handlers:
            _extra_log.addHandler(handler)
            _extra_log.addHandler(console)

# Singleton-instance guard (2026-08-07) — added after the 08:02-08:05
# duplicate-process incident: a rogue duplicate supervisord launched a
# second copy of this process alongside the real one. relay.py doesn't
# bind a listening port, so nothing about a normal double-launch failed
# loudly the way agent-server's port conflict did — the second copy just
# ran, undetected, with its own Discord connection and its own 30s
# retry-spool loop, racing the real one on every message. See
# agents/Marvin/memory/facts/agent-server-duplicate-process-incident.md
# for what that actually caused (alternating 401/500s, duplicate
# spool entries). Kept as a module-level reference so the flock isn't
# released by garbage collection; the OS releases it automatically the
# instant this process exits for ANY reason, including a hard kill —
# deliberately not a PID file, which would need its own stale-cleanup
# logic that could itself get skipped the same way the duplicate
# supervisord's children were.
_SINGLETON_LOCK_FD = None

def _acquire_singleton_lock(name: str) -> None:
    global _SINGLETON_LOCK_FD
    lock_path = WORKSPACE_ROOT / "data" / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.critical(
            f"Another {name} instance already holds {lock_path} — refusing "
            "to start as a duplicate. If this is unexpected (e.g. a stale "
            "lock after a hard crash), the OS should already have released "
            "it on process exit — check for a genuinely live process before "
            "assuming the lock file itself needs manual cleanup."
        )
        sys.exit(1)
    fd.write(str(os.getpid()))
    fd.flush()
    _SINGLETON_LOCK_FD = fd

# Global state
agent_config: Dict = {}
channels_config: Dict = {}
discord_id_to_agent: Dict[int, str] = {}
active_dispatches: Dict[str, asyncio.Task] = {}
dispatch_semaphores: Dict[str, asyncio.Semaphore] = {}

# =============================================================================
# Configuration Loading
# =============================================================================

def load_config():
    """Load agent and channel configuration"""
    global agent_config, channels_config, discord_id_to_agent

    # Load agents
    if AGENTS_CONFIG_PATH.exists():
        with open(AGENTS_CONFIG_PATH) as f:
            config_data = json.load(f)
            agent_config = config_data.get("agents", {})
    else:
        agent_config = {}
        log.warning(f"Agents config not found: {AGENTS_CONFIG_PATH}")

    # Load channels
    if CHANNELS_CONFIG_PATH.exists():
        with open(CHANNELS_CONFIG_PATH) as f:
            channels_config = json.load(f)
    else:
        channels_config = {}
        log.warning(f"Channels config not found: {CHANNELS_CONFIG_PATH}")

    # Build Discord ID map
    for agent_name, config in agent_config.items():
        bot_id_env = config.get("discord_bot_id_env")
        if bot_id_env:
            bot_id = os.environ.get(bot_id_env)
            if bot_id:
                discord_id_to_agent[int(bot_id)] = agent_name

    log.info(f"Loaded config for {len(agent_config)} agents, {len(channels_config.get('channels', {}))} channels")


def load_server_ids(config: Dict) -> set:
    """Discord server IDs this relay will accept messages from.

    `server_id` (a single string, what setup.sh writes) stays supported. A
    system that also needs to reach a shared server — a second household, a
    server where agents from different installs talk to each other — adds
    `server_ids` alongside it, and both are honoured:

        {"server_id": "111", "server_ids": ["222", "333"], "channels": {...}}

    Channels are still matched by ID, so a channel only routes if it's listed
    in `channels` regardless of which server it lives in.
    """
    ids = set()
    single = config.get("server_id")
    if single:
        ids.add(str(single))
    extra = config.get("server_ids") or []
    if isinstance(extra, (str, int)):
        extra = [extra]
    ids.update(str(s) for s in extra if s)
    return ids

# =============================================================================
# Discord Adapter
# =============================================================================

class DiscordAdapter(discord.Client):
    """Discord message routing to agent server"""

    def __init__(self, *args, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True
        # Presence intent (2026-08-18, Ian): read online/idle/dnd status
        # and custom activity text for other members/bots, e.g. Amos, so
        # a busy-on-a-long-task signal can eventually be read instead of
        # guessed. Both this flag AND the "Presence Intent" toggle in the
        # Discord Developer Portal are required — Discord rejects the
        # gateway handshake with PrivilegedIntentsRequired if the portal
        # side isn't also on. members is needed alongside it for presence
        # to resolve to real member objects rather than IDs only; both
        # confirmed enabled portal-side via a standalone test connection
        # before wiring this in live.
        intents.presences = True
        intents.members = True
        super().__init__(intents=intents, *args, **kwargs)

        self.http_session = None
        self.server_ids = set()
        self.gate: Optional[ReplyGate] = None
        self._health_task: Optional[asyncio.Task] = None
        self._deferred_poke_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None
        self._last_applied_status: Optional[tuple] = None

        # /sys restart-server confirm-arm + cooldown state, see
        # _sys_restart_server(). Plain instance attrs are safe here for the
        # same reason _INFLIGHT_COUNT is safe as a plain int — asyncio is
        # single-threaded, no handler can preempt another's read/write.
        self._restart_server_pending: Optional[dict] = None
        self._restart_server_last_run: Optional[float] = None

        # Native Discord slash commands for the /sys commands that have a
        # real handler (Task #12, 2026-08-07; override/override-clear
        # added 2026-08-10). We're a single bare discord.Client, unlike
        # Amos's five-bots-per-process setup — his reason for staying on
        # raw REST registration instead of discord.py's CommandTree
        # (avoiding a restructure across all five) doesn't apply here, so
        # CommandTree is the better call for us: officially supported,
        # handles registration and interaction dispatch itself, one less
        # hand-rolled REST surface to get subtly wrong. Registers
        # status/usage/context/clear/reload/halt/compact/override/override-clear
        # — every /sys command with a real handler. Amos's explicit warning, taken
        # seriously: a command that registers cleanly and has no matching
        # branch silently does nothing when clicked, nothing errors
        # anywhere. The text `/sys` intercept stays as-is, unchanged,
        # both paths call the same _run_sys_command().
        self.tree = discord.app_commands.CommandTree(self)
        self._register_slash_commands()

    def _register_slash_commands(self):
        adapter = self

        async def _owner_check(interaction: discord.Interaction) -> bool:
            if OWNER_DISCORD_ID == 0 or interaction.user.id != OWNER_DISCORD_ID:
                await interaction.response.send_message(
                    "`[SYS]` Permission denied.", ephemeral=True
                )
                return False
            return True

        def _default_agent() -> Optional[str]:
            return next(
                (name for name, cfg in agent_config.items()
                 if cfg.get("discord_bot_token_env")),
                next(iter(agent_config), None)
            )

        @self.tree.command(name="status", description="Agent server status")
        async def status_cmd(interaction: discord.Interaction):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                reply = await adapter._run_sys_command("status", None)
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="usage", description="Rate-limit headroom for the whole install (shared across agents)")
        async def usage_cmd(interaction: discord.Interaction):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                reply = await adapter._run_sys_command("usage", None)
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="context", description="Show the #agent-chat blocker/status board (context_box) — no need to open the channel")
        @discord.app_commands.describe(include_resolved="Include resolved threads too (default: open only)")
        async def context_cmd(interaction: discord.Interaction, include_resolved: Optional[bool] = False):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                reply = await adapter._run_sys_command(
                    "context", None, extra_args=["--all"] if include_resolved else []
                )
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="clear", description="Clear session + restart subprocess (destructive)")
        @discord.app_commands.describe(agent="Target agent (default: the channel's owning agent)")
        async def clear_cmd(interaction: discord.Interaction, agent: Optional[str] = None):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                reply = await adapter._run_sys_command("clear", agent or _default_agent())
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="reload", description="Restart subprocess, keep session")
        @discord.app_commands.describe(agent="Target agent (default: the channel's owning agent)")
        async def reload_cmd(interaction: discord.Interaction, agent: Optional[str] = None):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                reply = await adapter._run_sys_command("reload", agent or _default_agent())
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="halt", description="Interrupt the current in-flight turn — session and subprocess stay alive")
        @discord.app_commands.describe(
            agent="Target agent (default: the channel's owning agent)",
            message="Optional instruction to queue as the next turn once the halt lands",
        )
        async def halt_cmd(interaction: discord.Interaction, agent: Optional[str] = None, message: Optional[str] = None):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                extra_args = [message] if message else []
                reply = await adapter._run_sys_command(
                    "halt", agent or _default_agent(), extra_args, str(interaction.user),
                    channel=interaction.channel,
                )
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="compact", description="Summarize session and restart fresh (lowers context utilization)")
        @discord.app_commands.describe(agent="Target agent (default: the channel's owning agent)")
        async def compact_cmd(interaction: discord.Interaction, agent: Optional[str] = None):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                # Defer immediately — 2026-09-01, Ian saw "The application did
                # not respond" on a real /compact call. Root cause: this hits
                # compact_session() -> summarize-session.py, which has a 75s
                # timeout (see agent-server.py), while Discord's initial ack
                # window is 3s. Same failure mode restart-server_cmd above was
                # already fixed for; compact_cmd just never got the same fix
                # when it was added. The compaction itself was very likely
                # completing fine server-side — Discord was just giving up on
                # waiting for an ack that never came.
                await interaction.response.defer()
                reply = await adapter._run_sys_command("compact", agent or _default_agent())
                await interaction.followup.send(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="override", description="Bypass rate-limit pause for an agent (owner, auto-expiring, capped)")
        @discord.app_commands.describe(
            minutes="Duration in minutes (server caps this regardless)",
            agent="Target agent (default: the channel's owning agent)",
            reason="Optional reason, logged with the override",
        )
        async def override_cmd(
            interaction: discord.Interaction, minutes: float,
            agent: Optional[str] = None, reason: Optional[str] = None,
        ):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                extra_args = [str(minutes)] + ([reason] if reason else [])
                reply = await adapter._run_sys_command(
                    "override", agent or _default_agent(), extra_args, str(interaction.user)
                )
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="override-clear", description="Clear an active rate-limit override")
        @discord.app_commands.describe(agent="Target agent (default: the channel's owning agent)")
        async def override_clear_cmd(interaction: discord.Interaction, agent: Optional[str] = None):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                reply = await adapter._run_sys_command("override-clear", agent or _default_agent())
                await interaction.response.send_message(reply)
            finally:
                _INFLIGHT_COUNT -= 1

        @self.tree.command(name="restart-server", description="Bounce agent-server.py itself (whole install, waits for idle, confirm required)")
        async def restart_server_cmd(interaction: discord.Interaction):
            global _INFLIGHT_COUNT
            _INFLIGHT_COUNT += 1
            try:
                if not await _owner_check(interaction):
                    return
                # Defer immediately — the idle-wait alone can run up to
                # RESTART_SERVER_IDLE_WAIT_TIMEOUT_SEC, and the interaction
                # token expires long before that (Discord's 3s ack window
                # for the initial response, ~15min for the deferred one).
                await interaction.response.defer()
                reply = await adapter._run_sys_command(
                    "restart-server", None, author=str(interaction.user),
                    channel=interaction.channel,
                )
                await interaction.followup.send(reply)
            finally:
                _INFLIGHT_COUNT -= 1

    async def setup_hook(self):
        """Initialize HTTP session"""
        import aiohttp
        self.http_session = aiohttp.ClientSession()
        self.server_ids = load_server_ids(channels_config)
        # `server_ids` is a set (order isn't meaningful, and sets aren't
        # subscriptable — `server_ids[0]` below used to raise "'set' object
        # is not subscriptable"). The primary guild (Heart of Gold, not
        # Amos's Crab Cavern) has to be picked from *ordered* config data,
        # not the set: this config only has `server_ids` (plural), no
        # singular `server_id`, and an earlier version of this fix fell
        # back to `next(iter(self.server_ids))` for that case — which
        # silently picked a different guild on every other restart
        # (Python set iteration order isn't stable across processes),
        # confirmed live: two consecutive boots synced slash commands to
        # two different guilds. Config list order is stable; use that.
        single = channels_config.get("server_id")
        extra = channels_config.get("server_ids") or []
        if isinstance(extra, (str, int)):
            extra = [extra]
        self.primary_server_id = str(single) if single else (
            str(extra[0]) if extra else None
        )
        log.info(
            "Discord adapter initialized (servers: %s)",
            ", ".join(sorted(self.server_ids)) or "none configured",
        )

    async def on_ready(self):
        """Bot logged in"""
        log.info(f"Discord bot ready as {self.user.name} (ID: {self.user.id})")
        # Reply gate: graduated wake logic for channels marked gate_mode
        # "tier2" in channels.json (currently #agent-chat only). Design is
        # Amos's (Mike's Karakos instance), ported with credit — see
        # reply_gate.py docstring. One instance covers every gated channel;
        # cooldown state is tracked per-channel internally.
        self.gate = ReplyGate(
            self_id=str(self.user.id),
            names=(self.user.name.lower(),),
            threshold=0.5,
            cooldown_sec=300,
            # 2026-08-08, per Ian: a real miss (Amos wrote "Marvin -- ..."
            # in plain prose in #agent-chat and it sat unread, deliberately
            # Tier 2 per reply_gate.py's "being named is not being
            # addressed" rule) showed that relying on both sides
            # remembering real @mention syntax across two separate Karakos
            # instances isn't reliable enough for a channel that's meant
            # to be near-100% direct address. 📨 anywhere in a message
            # forces the same free, no-cooldown Tier 1 wake as an
            # @mention. Convention still needs to be agreed with Amos on
            # his side -- this only makes our gate recognize it.
            attention_marker="\U0001F4E8",  # 📨 incoming envelope
        )
        await self.write_health_heartbeat()

        # Initial presence snapshot. Member/presence caches aren't
        # guaranteed fully populated the instant on_ready fires (chunking
        # is async), so this runs as a short-delayed background task
        # rather than blocking the rest of on_ready — on_presence_update
        # keeps it current from here on regardless.
        asyncio.create_task(self._initial_presence_snapshot())

        # health-monitor.py checks relay.json's age against a 5-minute
        # threshold, but write_health_heartbeat() used to only fire once
        # here in on_ready — so a relay that's been happily connected for
        # longer than 5 minutes without a reconnect would false-positive
        # as "stale" even though it's fine. Found via a real alert
        # 2026-08-06. Refresh periodically instead of only on (re)connect.
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_heartbeat_loop())

        # Task #9, 2026-08-06: retry spooled messages the agent server
        # rejected or couldn't be reached for. See DEFERRED_POKE_DIR above
        # and send_to_agent_server()/_flush_deferred_pokes() below.
        if self._deferred_poke_task is None or self._deferred_poke_task.done():
            self._deferred_poke_task = asyncio.create_task(self._deferred_poke_flush_loop())

        # 2026-08-18, Ian: self-presence, so status reflects current
        # thinking state and is readable by both him and other bots
        # (Amos), not just narrated in chat. See STATUS_FILE / set-status
        # skill above.
        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.create_task(self._status_poll_loop())

        # Task #12, 2026-08-07: sync the native slash commands to the
        # primary guild only (server_ids[0] — Heart of Gold, not Amos's
        # Crab Cavern, which we're also connected to). Guild-scoped sync
        # is instant per Amos's own note; global sync takes up to an hour
        # to propagate, which makes iterating on it miserable. Re-synced
        # on every reconnect — idempotent, discord.py only pushes an
        # update if the command set actually changed.
        if self.primary_server_id:
            try:
                guild = discord.Object(id=int(self.primary_server_id))
                # Commands registered via @self.tree.command(...) with no
                # explicit guild= are tracked as global in the tree's own
                # bookkeeping — sync(guild=X) alone only pushes commands
                # already associated with X, which is none of them.
                # copy_global_to() copies the global set into that
                # guild's local set first. First deploy synced 0 commands
                # without this — found live, not assumed.
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(f"Synced {len(synced)} slash command(s) to guild {self.primary_server_id}")
            except Exception as e:
                log.error(f"Slash command sync failed: {e}")

    async def _health_heartbeat_loop(self):
        """Refresh the health file every 60s for as long as the client is
        connected, so file age actually reflects current liveness."""
        try:
            while not self.is_closed():
                await asyncio.sleep(60)
                if not self.is_closed():
                    await self.write_health_heartbeat()
        except asyncio.CancelledError:
            pass

    async def _initial_presence_snapshot(self):
        """Give member/presence chunking a moment, then write the first
        data/presence.json so it isn't empty until the next live change."""
        try:
            await asyncio.sleep(3)
            self.write_presence_snapshot()
            log.info("Initial presence snapshot written (%d guild(s))", len(self.guilds))
        except Exception:
            log.exception("Initial presence snapshot failed")

    async def _deferred_poke_flush_loop(self):
        """Periodically retry spooled messages for as long as the client is
        connected. 30s interval matches issue #88's acceptance test shape
        ("stop the agent server, send a message, start it again within
        five minutes, pass = the reply arrives without resending") with
        room to spare."""
        try:
            while not self.is_closed():
                await asyncio.sleep(DEFERRED_POKE_FLUSH_INTERVAL_SEC)
                if not self.is_closed():
                    await self._flush_deferred_pokes()
        except asyncio.CancelledError:
            pass

    async def _flush_deferred_pokes(self):
        """Retry every spooled payload once. Successes are deleted; still-
        failing ones are left for the next pass; anything older than
        DEFERRED_POKE_MAX_AGE_SEC gets moved to dead/ instead of retried
        forever — a permanently malformed payload shouldn't spin here for
        the life of the container."""
        if not DEFERRED_POKE_DIR.is_dir():
            return
        files = sorted(DEFERRED_POKE_DIR.glob("*.json"))
        if not files:
            return

        now = time.time()
        for f in files:
            try:
                record = json.loads(f.read_text())
            except Exception as e:
                log.error(f"Deferred poke {f.name} unreadable, moving to dead/: {e}")
                self._move_deferred_poke_to_dead(f)
                continue

            ok, detail = await self._post_to_agent_server(record.get("payload", {}))
            if ok:
                log.info(f"Deferred poke {f.name} delivered on retry")
                f.unlink(missing_ok=True)
                continue

            age_sec = now - record.get("spooled_at", now)
            if age_sec > DEFERRED_POKE_MAX_AGE_SEC:
                log.error(
                    f"Deferred poke {f.name} exceeded {DEFERRED_POKE_MAX_AGE_SEC}s "
                    f"({age_sec:.0f}s), giving up: {detail}"
                )
                self._move_deferred_poke_to_dead(f)
            else:
                log.info(f"Deferred poke {f.name} still failing ({detail}), will retry")

    def _move_deferred_poke_to_dead(self, path: Path):
        try:
            DEFERRED_POKE_DEAD_DIR.mkdir(parents=True, exist_ok=True)
            path.rename(DEFERRED_POKE_DEAD_DIR / path.name)
        except Exception as e:
            log.error(f"Failed to move {path.name} to dead/: {e}")

    async def handle_sys_command(self, message: discord.Message) -> bool:
        """Intercept /sys owner commands before any normal routing.
        Returns True if this message was a /sys command (caller returns
        immediately after). Design from Amos (Mike's Karakos instance),
        2026-08-06: OWNER_DISCORD_ID existed in this file already but
        nothing ever checked it — this is what it's for. A session-clear
        command has to be reachable even when the agent it targets is
        completely wedged, which is exactly why this is handled here,
        in relay, rather than routed through the normal message queue
        like everything else — a wedged session can't process the
        command that unwedges it."""
        if OWNER_DISCORD_ID == 0 or message.author.id != OWNER_DISCORD_ID:
            return False
        content = message.content.strip()
        if not content.startswith("/sys"):
            return False

        parts = content.split()
        cmd = parts[1] if len(parts) > 1 else "status"
        target_agent = parts[2] if len(parts) > 2 else None
        extra_args = parts[3:] if len(parts) > 3 else []
        if not target_agent:
            target_agent = next(
                (name for name, cfg in agent_config.items()
                 if cfg.get("discord_bot_token_env")),
                next(iter(agent_config), None)
            )

        reply = await self._run_sys_command(
            cmd, target_agent, extra_args, str(message.author),
            channel=message.channel,
        )
        await message.channel.send(reply)
        return True

    async def _run_sys_command(
        self, cmd: str, agent: Optional[str],
        extra_args: Optional[List[str]] = None, author: str = "unknown",
        channel: Optional["discord.abc.Messageable"] = None,
    ) -> str:
        """Talk to agent-server's existing /agents endpoints directly —
        not adding a mechanism, just a Discord surface for what already
        exists (GET /agents, POST /agents/{name}/reset|reload)."""
        extra_args = extra_args or []
        headers = {"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
        try:
            if cmd == "status":
                async with self.http_session.get(
                    f"{AGENT_SERVER_URL}/agents", headers=headers
                ) as resp:
                    data = await resp.json()
                lines = []
                for a in data.get("agents", []):
                    line = f"`{a['name']}`: {a['state']}"
                    # Anthropic's own live rate-limit signal, added
                    # 2026-08-06 — see agent-server.py's
                    # _record_rate_limit_event(). Empty until that
                    # agent's subprocess has completed a turn since the
                    # server last started.
                    rl = a.get("rate_limit") or {}
                    if rl:
                        resets = rl.get("resetsAt")
                        resets_str = (
                            datetime.fromtimestamp(resets).strftime("%H:%M")
                            if isinstance(resets, (int, float)) else "?"
                        )
                        overage = " [OVERAGE]" if rl.get("isUsingOverage") else ""
                        line += (
                            f" — {rl.get('status', '?')} ({rl.get('rateLimitType', '?')}, "
                            f"resets {resets_str}){overage}"
                        )
                    # Monthly account spend cap (2026-08-18) — distinct from
                    # rate_limit above: doesn't self-clear on a timer, needs
                    # Ian to raise it at claude.ai/settings/usage. See
                    # agent-server.py's CLI_SPEND_LIMIT_SIGNATURE.
                    if a.get("spend_limit_blocked"):
                        line += " **[SPEND LIMIT BLOCKED — raise at claude.ai/settings/usage]**"
                    lines.append(line)
                return "**/sys status**\n" + "\n".join(lines) if lines else "No agents found."

            if cmd == "usage":
                # Headroom is a whole-install question, not a per-agent
                # one — every agent shares the same account's rate
                # limit — so like status it runs ahead of target
                # resolution rather than demanding one. Ported from
                # mcarmody/karakos-package#128.
                async with self.http_session.get(
                    f"{AGENT_SERVER_URL}/usage", headers=headers
                ) as resp:
                    data = await resp.json()
                agents = data.get("agents") or {}
                if not agents:
                    return "**/sys usage**: no agents configured."
                # 2026-09-03 (task-1788454188): format_usage_report's
                # summary is now a multi-line dual-bar block (one bar per
                # rate-limit window, own "[SYS] Account usage — {agent}"
                # header already baked in) rather than a single line, so
                # this no longer prefixes `name` — a code fence keeps the
                # Unicode block bars aligned monospace, which Discord's
                # default proportional text won't do.
                blocks = [
                    f"```\n{(agents[name] or {}).get('summary', 'no reading')}\n```"
                    for name in sorted(agents)
                ]
                return "**/sys usage**\n" + "\n".join(blocks)

            if cmd == "restart-server":
                # Whole-process action like status/usage, no agent target.
                return await self._sys_restart_server(author, channel)

            if cmd == "context":
                # Whole-install board like status/usage — no real agent
                # target, but `agent`/`extra_args` may have caught a stray
                # "--all" from the raw command text since this command
                # doesn't take a target (handle_sys_command's positional
                # parsing has no way to know that ahead of time — the
                # text form defaults `agent` to some real agent name when
                # nothing follows "context", which is harmless here since
                # it's just one more thing "--all" isn't equal to).
                # Renders context_box.py's board directly — the "check
                # without entering #agent-chat" surface Ian asked for
                # 2026-08-27, reachable on demand rather than only when a
                # new message triggers a mirror.
                all_flag = "--all" in ({agent} | set(extra_args))
                return context_box.render_board(open_only=not all_flag)

            if not agent:
                return "**/sys**: no agent configured to target"

            if cmd == "clear":
                async with self.http_session.post(
                    f"{AGENT_SERVER_URL}/agents/{agent}/reset", headers=headers
                ) as resp:
                    ok = resp.status == 200
                return (f"**/sys clear** `{agent}`: "
                        f"{'done — fresh session' if ok else f'failed ({resp.status})'}")

            if cmd == "compact":
                # Manual trigger for the same finalize-then-fresh-session
                # action the automatic compaction triggers use (see
                # compact_session() in agent-server.py). 2026-08-10, Ian's
                # ask, prompted by seeing high context utilization and
                # wanting it down on demand.
                async with self.http_session.post(
                    f"{AGENT_SERVER_URL}/agents/{agent}/compact", headers=headers
                ) as resp:
                    ok = resp.status == 200
                return (f"**/sys compact** `{agent}`: "
                        f"{'done — summarized and restarted with a fresh session' if ok else f'failed ({resp.status})'}")

            if cmd == "reload":
                async with self.http_session.post(
                    f"{AGENT_SERVER_URL}/agents/{agent}/reload", headers=headers
                ) as resp:
                    ok = resp.status == 200
                return (f"**/sys reload** `{agent}`: "
                        f"{'done — session preserved' if ok else f'failed ({resp.status})'}")

            if cmd == "halt":
                # 2026-08-30, Ian's ask: a Discord-native equivalent of the
                # CLI's own interrupt, for "I need you to STOP on this
                # specific thing" — distinct from reload/clear, which bounce
                # the whole subprocess. Verified live against the real CLI:
                # sends a stream-json control_request over stdin, gets a
                # control_response ack, and the in-flight turn actually
                # stops without killing the process or losing the session.
                # See interrupt_agent() in agent-server.py for the capture.
                async with self.http_session.post(
                    f"{AGENT_SERVER_URL}/agents/{agent}/interrupt", headers=headers
                ) as resp:
                    ok = resp.status == 200

                # 2026-08-30, follow-up: "halt, and here's what to do
                # instead" in one command. Deliberately NOT a special-cased
                # send — the interrupted turn still holds agent_locks[agent]
                # until its own result event lands and the state flips back
                # to IDLE (interrupt_agent() skips that lock on purpose, see
                # its docstring), so a direct send_to_agent() here would race
                # the still-finishing turn. Queuing through POST /message —
                # the exact path a normal Discord message takes — sidesteps
                # that entirely: it lands in message_queue and gets drained
                # by process_agent_queue's own self-continuation pass the
                # moment the interrupted turn actually clears, same as any
                # message that arrived while the agent was busy. Reuses
                # _post_to_agent_server/_spool_deferred_poke so a follow-up
                # gets the same never-drop guarantee a live message does.
                follow_up = " ".join(extra_args).strip() if extra_args else ""
                queued_note = ""
                if ok and follow_up:
                    channel_id = str(channel.id) if channel is not None else "0"
                    channel_name = (
                        self.get_channel_name(channel_id)
                        or getattr(channel, "name", None)
                        or "unknown"
                    )
                    payload = {
                        "agent": agent,
                        "channel": channel_name,
                        "channel_id": channel_id,
                        "server": "discord",
                        "author": author,
                        "author_id": str(OWNER_DISCORD_ID),
                        "is_bot": False,
                        "content": follow_up,
                        "message_id": f"halt-followup-{uuid.uuid4()}",
                        "mentions_agent": True,
                    }
                    queued_ok, detail = await self._post_to_agent_server(payload)
                    if queued_ok:
                        queued_note = " — follow-up queued for the moment the interrupted turn clears"
                    else:
                        self._spool_deferred_poke(payload, detail)
                        queued_note = f" — follow-up spooled for retry (agent-server said: {detail})"

                return (f"**/sys halt** `{agent}`: "
                        f"{'sent — current turn interrupted, session intact' if ok else f'failed ({resp.status})'}"
                        f"{queued_note}")

            if cmd == "override":
                # /sys override <agent> <minutes> [reason...] — owner-set,
                # auto-expiring bypass of is_rate_limit_paused() for one
                # agent. 2026-08-10, Ian's ask: "bugfixes regardless of
                # session limits, at my discretion." Server-side caps the
                # duration regardless of what's requested here (see
                # RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC in agent-server.py)
                # so a typo (or a forgotten override) can't disable the
                # circuit breaker indefinitely.
                if not extra_args:
                    return "**/sys override** `<agent>` `<minutes>` `[reason...]` — minutes required"
                try:
                    minutes = float(extra_args[0])
                except ValueError:
                    return f"**/sys override**: `{extra_args[0]}` isn't a number of minutes"
                reason = " ".join(extra_args[1:])
                payload = {
                    "enabled_by": author,
                    "duration_sec": minutes * 60,
                    "reason": reason,
                }
                async with self.http_session.post(
                    f"{AGENT_SERVER_URL}/agents/{agent}/rate-limit-override",
                    headers=headers, json=payload,
                ) as resp:
                    data = await resp.json() if resp.status == 200 else {}
                    ok = resp.status == 200
                if not ok:
                    return f"**/sys override** `{agent}`: failed ({resp.status})"
                expires = data.get("expires_at")
                expires_str = (
                    datetime.fromtimestamp(expires).strftime("%H:%M UTC")
                    if isinstance(expires, (int, float)) else "?"
                )
                capped_note = " (capped — requested duration was longer)" if data.get("capped") else ""
                return (f"**/sys override** `{agent}`: rate-limit pause bypassed until "
                        f"{expires_str}{capped_note}. Anthropic can still reject calls "
                        f"for real — this only lifts our own queue hold.")

            if cmd == "override-clear":
                async with self.http_session.post(
                    f"{AGENT_SERVER_URL}/agents/{agent}/rate-limit-override/clear",
                    headers=headers,
                ) as resp:
                    data = await resp.json() if resp.status == 200 else {}
                    ok = resp.status == 200
                if not ok:
                    return f"**/sys override-clear** `{agent}`: failed ({resp.status})"
                had_one = data.get("status") == "override_cleared"
                return (f"**/sys override-clear** `{agent}`: "
                        f"{'cleared' if had_one else 'no active override'}")

            return (f"Unknown /sys command: `{cmd}`. Known: status, clear, "
                    f"reload, halt, compact, usage, override, override-clear, "
                    f"restart-server, context")
        except Exception as e:
            return f"**/sys {cmd}** failed: {e}"

    async def _sys_restart_server(
        self, author: str, channel: Optional["discord.abc.Messageable"],
    ) -> str:
        """/sys restart-server — bounce agent-server.py itself (not this
        process, not scheduler.py — see spec's "Scope" decision). Full
        design writeup: agents/Marvin/journal/sys-restart-server-spec-
        2026-08-16.md. Runs from relay because relay's process tree has no
        ancestry relationship to agent-server's — the same reasoning that
        already put reload/clear here rather than routing through
        agent-server's own HTTP API (which is served *by* the process
        being restarted, and would die along with the thing it's trying
        to report on).

        Flow: confirm (two invocations within a short window) -> wait for
        every agent to go IDLE with queue_depth 0 (bounded, no forcing —
        Ian, 2026-08-16 "Escalate and stop") -> snapshot session_ids ->
        SIGTERM via safe-pkill.sh (systemd's Restart=always relaunches it,
        ~2s per RestartSec) -> poll until it's back and confirm sessions
        survived.
        """
        now = time.time()

        if self._restart_server_last_run is not None:
            since = now - self._restart_server_last_run
            if since < RESTART_SERVER_COOLDOWN_SEC:
                remaining = RESTART_SERVER_COOLDOWN_SEC - since
                return (f"**/sys restart-server**: cooldown active, "
                        f"{remaining:.0f}s left since the last attempt.")

        pending = self._restart_server_pending
        if pending is None or (now - pending["armed_at"]) > RESTART_SERVER_CONFIRM_WINDOW_SEC:
            self._restart_server_pending = {"armed_at": now, "author": author}
            return (
                "**/sys restart-server**: this bounces `agent-server.py` for "
                "the whole install — every agent, every session (sessions are "
                "preserved across the restart, this doesn't clear them). Run "
                f"`/sys restart-server` again within "
                f"{RESTART_SERVER_CONFIRM_WINDOW_SEC}s to confirm."
            )
        self._restart_server_pending = None  # consumed, one shot

        headers = {"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}

        async def _fetch_health() -> dict:
            async with self.http_session.get(
                f"{AGENT_SERVER_URL}/health", headers=headers
            ) as resp:
                return await resp.json()

        # Pre-flight: wait for every agent to actually be idle. Restarting
        # mid-turn has caused real damage before (kill_agent_subprocess() /
        # ProcessLookupError incident, see
        # facts/hog-main-push-complete-2026-08-11.md) — this is the one
        # substantive addition Ian made to the original draft, and it's
        # non-optional.
        wait_start = time.time()
        last_status_push = wait_start
        snapshot: Optional[dict] = None
        pre_boot_id: Optional[str] = None
        while True:
            try:
                health = await _fetch_health()
            except Exception as e:
                return (f"**/sys restart-server**: couldn't reach agent-server "
                        f"to check idle state, nothing sent: {e}")

            agents = health.get("agents", {})
            not_idle = {
                name: a for name, a in agents.items()
                if a.get("state") != "IDLE" or a.get("queue_depth", 0) > 0
            }
            if not not_idle:
                snapshot = agents
                pre_boot_id = health.get("boot_id")
                break

            elapsed = time.time() - wait_start
            detail = ", ".join(
                f"`{name}` state={a.get('state')} queue_depth={a.get('queue_depth', 0)}"
                for name, a in not_idle.items()
            )

            if elapsed > RESTART_SERVER_IDLE_WAIT_TIMEOUT_SEC:
                # Ian, 2026-08-16 19:50 #general: "Escalate and stop" — no
                # force-past-ceiling. Post full context to #signals with a
                # direct ping (facts/signals-decisions-need-a-direct-ping-
                # 2026-08-16.md — a #signals post needing a decision back
                # is not sufficient on its own) and leave it to a human.
                add_pending(
                    "signals",
                    f"**/sys restart-server** timed out after {int(elapsed)}s "
                    f"waiting for idle ({detail}). Not forcing it — needs a "
                    f"human call on whether to force a restart anyway. "
                    f"<@{OWNER_DISCORD_ID}>",
                )
                return (
                    f"**/sys restart-server**: stopped — {detail} never went "
                    f"idle within {RESTART_SERVER_IDLE_WAIT_TIMEOUT_SEC}s. "
                    f"Posted to #signals with a direct ping, no restart sent."
                )

            if channel is not None and (time.time() - last_status_push) > RESTART_SERVER_STATUS_INTERVAL_SEC:
                try:
                    await channel.send(
                        f"**/sys restart-server**: still waiting on {detail} "
                        f"({int(elapsed)}s elapsed, won't force it)."
                    )
                except Exception as e:
                    log.warning(f"restart-server status push failed: {e}")
                last_status_push = time.time()

            await asyncio.sleep(2)

        pre_sessions = {name: a.get("session_id") for name, a in snapshot.items()}

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(SAFE_PKILL), "-TERM", "bin/agent-server.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except Exception as e:
            return f"**/sys restart-server**: failed to invoke safe-pkill.sh: {e}"

        self._restart_server_last_run = time.time()
        if proc.returncode != 0:
            output = (stderr or stdout or b"").decode(errors="ignore").strip()
            return f"**/sys restart-server**: safe-pkill.sh failed ({output})"

        # Poll until agent-server is back, then confirm sessions survived
        # (new PID, same session_ids — the same guarantee /sys reload
        # already relies on) rather than just "it responded".
        #
        # 2026-08-30: "responded" alone is a false positive — graceful_
        # shutdown() keeps /health answering all the way through its own
        # cleanup (idle-wait, per-agent session summaries, subprocess
        # kills) right up until sys.exit(0), so the very first poll almost
        # always lands on the still-dying OLD process, which trivially
        # reports matching session_ids against itself. That's what made
        # this always read "done in 1s" when real recovery was 1-2
        # minutes out — Ian flagged this live, 2026-08-30. Now require
        # boot_id (fresh per process, see SERVER_BOOT_ID in
        # agent-server.py) to actually change before accepting a health
        # response as evidence of a real restart, not just a response.
        poll_start = time.time()
        while time.time() - poll_start < RESTART_SERVER_POST_RESTART_TIMEOUT_SEC:
            await asyncio.sleep(1)
            try:
                health = await _fetch_health()
            except Exception:
                continue
            agents = health.get("agents", {})
            if not agents:
                continue
            if pre_boot_id is not None and health.get("boot_id") == pre_boot_id:
                # Still the old process — it hasn't actually died yet.
                continue
            post_sessions = {name: a.get("session_id") for name, a in agents.items()}
            preserved = all(
                post_sessions.get(name) == sid for name, sid in pre_sessions.items()
            )
            return (
                f"**/sys restart-server**: done in {int(time.time() - poll_start)}s — "
                f"agent-server back up, sessions "
                f"{'preserved' if preserved else 'CHANGED (unexpected — check manually, do not assume this is fine)'}."
            )

        # No response within the timeout: this now has the same external
        # signature as a crash-loop, not a clean bounce — say so plainly
        # rather than a soft "might still be starting" (spec's step 6).
        return (
            f"**/sys restart-server**: sent SIGTERM but agent-server hasn't "
            f"responded within {RESTART_SERVER_POST_RESTART_TIMEOUT_SEC}s — "
            f"this looks like a crash-loop, not a clean bounce. Check "
            f"`journalctl -u karakos-agent-server` directly."
        )

    async def on_message(self, message: discord.Message):
        """Thin in-flight-tracking wrapper — see _on_message_impl for the
        actual routing logic. Kept separate so a SIGTERM mid-handling
        (reload-on-commit.py bouncing this process for a relay.py/
        reply_gate.py change) can be detected and waited out by
        _graceful_shutdown() instead of silently dropping whatever this
        message was in the middle of doing."""
        global _INFLIGHT_COUNT
        _INFLIGHT_COUNT += 1
        try:
            await self._on_message_impl(message)
        finally:
            _INFLIGHT_COUNT -= 1

    async def _on_message_impl(self, message: discord.Message):
        """Route Discord message to agent"""
        # Ignore messages from servers we aren't configured for. Checked
        # before the self-author capture below too — Marvin never posts
        # outside a configured guild, but this keeps the capture log's
        # scope consistent with every other author's regardless.
        if message.guild and str(message.guild.id) not in self.server_ids:
            return

        # Capture message — including our own. Moved ahead of the
        # "ignore own messages" return below (2026-08-29, found via a
        # live "did that message actually send" check that looked like a
        # false negative): capture_message() used to run only after that
        # return, so Marvin's own sent messages never made it into
        # data/messages/messages-*.jsonl, the same gap
        # facts/outbound-messages-not-in-ingest-log-2026-08-13.md already
        # documented. mcp/tools-server.py's `discord history` action reads
        # that exact log, so it inherited the blind spot too — a message
        # Marvin posted looked indistinguishable from one that silently
        # failed. Capturing here, before the early return, fixes the log;
        # the return below still skips all routing/reply logic for
        # self-authored messages same as before, that part isn't a bug.
        await self.capture_message(message)

        # Ignore own messages (routing/reply logic only — already captured
        # above)
        if message.author == self.user:
            return

        # Speaking Banana (2026-08-28, specs/2026-08-28-speaking-banana.md):
        # record another bot's turn-claim. Unconditional on gate_mode (unlike
        # the context_box/handoff parsing below, which is tier2-only and
        # currently only covers #agent-chat) — the claim signal is a bare
        # emoji, not an envelope, and needs to work in #lounge too, which
        # has no gate_mode at all. Only bot authors trigger it (a human
        # typing a banana isn't claiming a turn), and only in a channel
        # that actually shares its floor with other bots (banana.in_scope
        # mirrors agent-server.py's quiet-mode guild check). This only
        # catches *other* bots' claims — Marvin's own outgoing replies
        # never reach on_message (self-authored messages return above this
        # point), so Marvin's side of the claim is recorded separately in
        # agent-server.py at compose time. Watch-first: this never blocks
        # or gates a reply, purely records for visibility.
        if (
            message.author.bot
            and banana.starts_with_claim(message.content or "")
            and banana.in_scope(str(message.channel.id), channels_config)
        ):
            channel_name_for_claim = self.get_channel_name(str(message.channel.id)) or str(message.channel.id)
            banana.claim(channel_name_for_claim, message.author.display_name)

        # /sys owner commands, intercepted before any normal routing so
        # they work even against a wedged agent. See handle_sys_command.
        if await self.handle_sys_command(message):
            return

        # (Removed 2026-08-06: a literal trailing "∎" used to be checked
        # here as an anti-loop termination token. Superseded by the handoff
        # protocol's `reply: none` field — Amos's own framing when he
        # pitched it was "it's your ∎, made checkable" — and neither side
        # has actually appended a bare ∎ since that landed. Real loop
        # prevention lives in reply_gate.py's Tier 1/2 wake logic plus the
        # handoff envelope's reply field, both of which stay in effect
        # below; this was dead code checking for a signal nobody produces
        # anymore. Ian approved the removal.)

        # Reply gate for channels that opt into graduated wake logic instead
        # of the blunt always-on / mention-only config split. Tier 1
        # (@mention, reply-to-self) is free and always wakes. Everything
        # else is scored by a cheap model, cooldown-gated, biased to
        # silence — see reply_gate.py.
        channel_name = self.get_channel_name(str(message.channel.id))
        channel_config = (
            channels_config.get("channels", {}).get(channel_name, {})
            if channel_name else {}
        )
        if channel_config.get("gate_mode") == "tier2" and self.gate:
            if not message.author.bot:
                self.gate.note_human_message(str(message.channel.id))

            # Handoff envelope (handoff.py): a sender-declared `reply` field
            # that short-circuits the gate entirely when present and valid.
            # required -> forced wake, free, same tier as an @mention.
            # none -> forced quiet, skips even the Tier 2 scorer call, which
            # a plain gate decline doesn't save. optional, missing, or
            # malformed -> no envelope, fall through to the normal gate
            # unchanged. Proposed by Amos 2026-08-05; see handoff.py for the
            # measured reasoning (a full turn costs ~1000x a compressed
            # message, so the only real lever is whether a turn happens).
            envelope = parse_handoff(message.content or "")

            # context_box (handoff.py, added 2026-08-27): record it on the
            # board regardless of reply value — a blocker declared with
            # reply:none still needs to be visible — and, for state in
            # {blocked, waiting-human} only, auto-mirror one line to
            # #general via outbox. This replaces a per-turn judgment call
            # ("is this worth mirroring?") that recurred as a miss three
            # times (see facts/agent-chat-replies-also-outbox-to-general.md)
            # with an unconditional trigger keyed off the field, same
            # move `reply` already made for the wake/quiet decision.
            # Inbound-only for now — see context_box.py's module docstring
            # for the outbound (Marvin's own replies) gap that's still open.
            # mirror_to (below, task-1788124679) generalizes the egress half
            # of this: destination-parameterized instead of hardcoded
            # #general, and triggerable without a context_box at all.
            state_triggered_mirror = False
            if envelope and envelope.context_box:
                cb = envelope.context_box
                row = context_box.record(
                    subject=envelope.subject,
                    state=cb.state,
                    blocked_on=cb.blocked_on,
                    waiting_on=cb.waiting_on,
                    sender=message.author.display_name,
                    channel=channel_name or str(message.channel.id),
                )
                if context_box.should_mirror(cb.state):
                    state_triggered_mirror = True
                    # mirror_to (2026-08-30) overrides the destination when
                    # present; absent, this keeps the original #general
                    # default -- every envelope written before mirror_to
                    # existed still lands exactly where it always has.
                    destination = envelope.mirror_to or "general"
                    add_pending(
                        destination,
                        context_box.render_mirror_line(envelope.subject or "(no subject)", row),
                    )
                    log.info(
                        f"[context_box] {channel_name} subject={envelope.subject!r} "
                        f"state={cb.state} -> mirrored to #{destination}"
                    )

            # Generalized envelope egress (2026-08-30, task-1788124679):
            # mirror_to alone -- no triggering context_box required --
            # lets a sender request a mirror for ANY kind of message, to
            # any configured channel. Deliberately a separate mechanism
            # from context_box's state-triggered board above, not a
            # replacement (see handoff.py's mirror_to docstring for why).
            # Skipped when the state-triggered path above already fired
            # for this same envelope, so a context_box message with
            # mirror_to set gets exactly one mirror, not two.
            if envelope and envelope.mirror_to and not state_triggered_mirror:
                add_pending(
                    envelope.mirror_to,
                    context_box.render_envelope_mirror_line(
                        envelope, message.author.display_name, channel_name or str(message.channel.id),
                    ),
                )
                log.info(
                    f"[egress] {channel_name} kind={envelope.kind} "
                    f"subject={envelope.subject!r} -> mirrored to #{envelope.mirror_to}"
                )

            if envelope and envelope.reply == "none":
                # Sender's declared intent still wins — silence stays free,
                # no scorer call either way — but a '?' in the prose next to
                # reply:none is a plausible mis-declaration. Free to catch,
                # so it's caught. Amos's addition, 2026-08-05.
                if "?" in (message.content or ""):
                    log.warning(
                        f"[gate] {channel_name} handoff: reply=none but "
                        f"content has '?' - possible sender mis-declare, "
                        f"staying quiet anyway"
                    )
                else:
                    log.info(
                        f"[gate] {channel_name} handoff: reply=none -> "
                        f"quiet (scorer skipped)"
                    )
                return
            # reply_from (handoff.py, added 2026-08-30): a `reply:required`
            # names who it's actually aimed at, in a channel with more than
            # one addressable agent. Ported from Amos's design after
            # #agent-chat grew a third bot (Zero) and an unconditional
            # force-wake on ANY `required` envelope stopped being a
            # two-party assumption. required_but_misdirected() is the pure
            # decision (see handoff.py); a True result declines the free
            # pass and falls through to the normal gate below, same scored
            # path an unaddressed message gets anyway, not a special
            # escalation or a guaranteed drop.
            misdirected = required_but_misdirected(envelope, self.user.name)
            if misdirected:
                log.info(
                    f"[gate] {channel_name} handoff: reply=required but "
                    f"reply_from={envelope.reply_from!r} names someone else -- "
                    f"declining the free pass, falling through to normal gate"
                )

            if envelope and envelope.reply == "required" and not misdirected:
                decision = Decision(
                    True, "handoff", "reply: required",
                    channel_id=str(message.channel.id),
                )
            else:
                robots_role_id = channels_config.get("robots_role_id")
                mentions_self = self.user in message.mentions
                mentions_role = bool(
                    robots_role_id
                    and any(str(r.id) == str(robots_role_id) for r in message.role_mentions)
                )
                mentions_other = (
                    any(m.bot and m.id != self.user.id for m in message.mentions)
                    and not mentions_self
                )

                gate_msg = GateMessage(
                    channel_id=str(message.channel.id),
                    author_id=str(message.author.id),
                    content=message.content or "",
                    mentions_self=mentions_self,
                    mentions_role=mentions_role,
                    mentions_other=mentions_other,
                    is_reply_to_self=await self._is_reply_to_self(message),
                    author_is_bot=message.author.bot,
                )
                decision = self.gate.evaluate(gate_msg)
                if decision.needs_score:
                    context = await self._recent_context(message.channel)
                    score = await self.score_with_cheap_model(
                        context, message.author.display_name
                    )
                    if score is None:
                        # Scorer died (timeout/bad output/missing binary)
                        # even after its internal retry. Do NOT let this
                        # collapse into a 0.0 score — that's the bug Amos
                        # hit 2026-08-09: a dead classifier and a real
                        # confident-no both logged as score=0.00, so a
                        # message sat unread for 13 minutes before Mike
                        # caught it by hand. Fall back to envelope +
                        # substance floor instead, and log it as a
                        # failure, not a decline.
                        content = message.content or ""
                        fallback = envelope is not None or self._substance_floor(content)
                        log.warning(
                            f"[gate] {channel_name} tier2 scorer failed twice, "
                            f"falling back to envelope+substance floor -> "
                            f"{'WAKE' if fallback else 'quiet'}"
                        )
                        decision = self.gate.resolve(decision, None, fallback=fallback)
                    else:
                        decision = self.gate.resolve(decision, score)

            log.info(
                f"[gate] {channel_name} {decision.tier}: {decision.reason} "
                f"-> {'WAKE' if decision.wake else 'quiet'}"
            )
            if not decision.wake:
                return

        # Determine target agent. Per #82's own stated invariant ("a
        # channel in a newly-permitted server routes nothing until it's
        # listed [in channels.json]"), no routing should happen for a
        # channel we haven't explicitly configured -- regardless of guild.
        # Before 2026-08-14 that invariant only held for the channel-
        # default-agent fallback below; the bot-mention branch skipped it
        # entirely, so a direct @mention of my own bot ID from any
        # server_ids-permitted guild (including Amos's Crab Cavern, added
        # by #82 for the shared-server case) routed a full turn -- and,
        # upstream in agent-server.py, streamed tool/interim text -- into
        # a channel that was never actually listed. Found comparing notes
        # with Amos on cross-server posting boundaries; paired with the
        # quiet-channel allowlist flip in agent-server.py, which covers
        # the same gap for any turn that still gets through some other way.
        channel_name = self.get_channel_name(str(message.channel.id))
        if not channel_name:
            return  # Not a listed channel; no routing regardless of mentions.

        target_agent = None

        # Check for bot mention
        for mention in message.mentions:
            if mention.bot and mention.id in discord_id_to_agent:
                target_agent = discord_id_to_agent[mention.id]
                break

        # Shared-role mention (e.g. @robots): route to *this* bot specifically,
        # even in channels with no blanket default_agent. A role ping is an
        # explicit address to "any listening bot" -- a stronger signal than
        # plain unaddressed chatter -- and answering it shouldn't require
        # making the whole channel always-on (that was the 2026-09-05 mistake:
        # flipping #lounge's default_agent to fix this fixed it by making
        # every message route, not just role-mention ones). This only changes
        # outcomes for channels that were previously silently dropping the
        # role-mention case; already-always-on channels (default_agent set)
        # resolve the same target via the fallback below regardless.
        if not target_agent:
            robots_role_id = channels_config.get("robots_role_id")
            if robots_role_id and any(
                str(r.id) == str(robots_role_id) for r in message.role_mentions
            ):
                target_agent = discord_id_to_agent.get(self.user.id)

        # Fall back to channel default agent
        if not target_agent:
            channel_config = channels_config.get("channels", {}).get(channel_name, {})
            target_agent = channel_config.get("default_agent")

        if not target_agent:
            return  # No routing

        # Send to agent server
        await self.send_to_agent_server(message, target_agent)

    def get_channel_name(self, channel_id: str) -> Optional[str]:
        """Get channel name from ID"""
        for name, config in channels_config.get("channels", {}).items():
            if config.get("id") == channel_id:
                return name
        return None

    async def _is_reply_to_self(self, message: discord.Message) -> bool:
        """True if message is a Discord reply to one of this bot's messages.
        A fetch failure fails toward False (not a reply) rather than raising,
        so a transient API hiccup drops one gate signal, not the message."""
        if message.reference is None:
            return False
        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved.author.id == self.user.id
        if message.reference.message_id is None:
            return False
        try:
            fetched = await message.channel.fetch_message(message.reference.message_id)
            return fetched.author.id == self.user.id
        except Exception:
            return False

    async def _recent_context(self, channel, limit: int = 12) -> str:
        """Recent channel history, oldest-first, for the Tier 2 scorer prompt.
        Pre-resolves Discord snowflakes (<@id>, <@&id>, <#id>) to usernames/display
        names so the cheap model doesn't hallucinate that a message addressed
        to a peer bot snowflake or role is meant for Marvin."""
        history = [m async for m in channel.history(limit=limit)]
        lines = []
        for m in reversed(history):
            content = m.content or ""
            if "<@" in content and hasattr(m, "mentions"):
                for mention in m.mentions:
                    content = content.replace(f"<@{mention.id}>", f"@{mention.display_name}")
                    content = content.replace(f"<@!{mention.id}>", f"@{mention.display_name}")
            if "<@&" in content and hasattr(m, "role_mentions"):
                for role in m.role_mentions:
                    content = content.replace(f"<@&{role.id}>", f"@{role.name}")
            if "<#" in content and hasattr(m, "channel_mentions"):
                for ch in m.channel_mentions:
                    content = content.replace(f"<#{ch.id}>", f"#{ch.name}")
            lines.append(f"{m.author.display_name}: {content}")
        return "\n".join(lines)

    async def score_with_cheap_model(self, context: str, author: str) -> Optional[float]:
        """Tier 2 scorer: one-shot Haiku call, no session state.

        Fixed 2026-08-09: this used to catch any failure (timeout, bad
        output, missing binary) and return 0.0, on the theory that a
        broken classifier should fail toward silence. That reasoning was
        wrong in a way Amos discovered on his side first: 0.0 is also
        exactly what a real, confident "ignore it" scores, so the two
        cases were indistinguishable in the log — `score=0.00` was true
        either way and told nobody which one happened. A message sat
        unread for 13 minutes on his side before a human caught it by
        hand.

        Now: one retry on failure, then return None (not a number) if
        both attempts fail. None is a distinct, loggable signal that the
        classifier died rather than declined. The caller (see the
        needs_score branch above) is responsible for a fallback decision
        — handoff envelope presence plus a substance floor — instead of
        silently trusting a fabricated 0.0.
        """
        prompt = SCORER_PROMPT.format(agent="Marvin", context=context, author=author)
        for attempt in range(2):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "claude", "-p",
                    "--model", "haiku",
                    "--max-turns", "1",
                    "--dangerously-skip-permissions",
                    prompt,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                raw = stdout.decode(errors="ignore")
                match = re.search(r"[01]?\.\d+|\b[01]\b", raw)
                if match:
                    return float(match.group(0))
                log.warning(
                    f"Gate scorer produced unparseable output "
                    f"(attempt {attempt + 1}/2): {raw[:200]!r}"
                )
            except Exception as e:
                log.warning(f"Gate scorer failed (attempt {attempt + 1}/2): {e}")
        log.error(
            "Gate scorer failed twice in a row — returning None, not 0.0. "
            "Caller must apply its own fallback."
        )
        return None

    @staticmethod
    def _substance_floor(content: str) -> bool:
        """Crude fallback heuristic used only when the tier-2 scorer itself
        is broken (see score_with_cheap_model). Not a replacement for the
        real classifier — just the least-bad default while it's
        unavailable. Biased slightly toward waking on longer or
        question-shaped messages, because the failure mode this guards
        against (a message silently dropped because the classifier died)
        is invisible to the sender, per Amos's 2026-08-09 report."""
        content = content or ""
        return len(content.split()) >= 8 or "?" in content

    async def _post_to_agent_server(self, payload: dict) -> tuple[bool, str]:
        """POST one message payload to the agent server. Returns (success,
        detail) where detail is a short status/error string for logging.
        Shared by the live send path and the deferred-poke flush loop so
        both retry the exact same request shape."""
        try:
            async with self.http_session.post(
                f"{AGENT_SERVER_URL}/message",
                json=payload,
                headers={"Authorization": f"Bearer {AGENT_SERVER_TOKEN}"}
            ) as resp:
                if resp.status == 202:
                    return True, "202"
                text = await resp.text()
                return False, f"{resp.status}: {text[:200]}"
        except Exception as e:
            return False, str(e)

    def _spool_deferred_poke(self, payload: dict, reason: str):
        """Write a failed payload to disk for later retry instead of
        dropping it. Filename embeds a timestamp so the flush loop can
        enforce DEFERRED_POKE_MAX_AGE_SEC without needing to open every
        file just to sort them."""
        try:
            DEFERRED_POKE_DIR.mkdir(parents=True, exist_ok=True)
            now = time.time()
            fname = f"{now:.6f}-{payload.get('message_id', 'unknown')}.json"
            record = {"spooled_at": now, "reason": reason, "payload": payload}
            (DEFERRED_POKE_DIR / fname).write_text(json.dumps(record))
            log.warning(
                f"Spooled message {payload.get('message_id')} for "
                f"{payload.get('agent')} after failure: {reason}"
            )
        except Exception as e:
            log.error(f"Failed to spool deferred poke — message lost: {e}")

    async def download_attachments(self, message: discord.Message) -> List[Dict]:
        """Save a message's attachments locally and describe them for the agent.

        Called from `send_to_agent_server` rather than `on_message` so that
        files on a message the gates are about to drop are never written to
        disk at all.

        An attachment that is too large, or that fails to download, still
        comes back in the list with `path: None` and a `skipped` reason. The
        agent needs to be able to say "you sent me a 40 MB video and I could
        not open it" — going quiet about the file is the bug this fixes, and
        a failed download reproduces it exactly.
        """
        attachments = getattr(message, "attachments", None) or []
        if not attachments:
            return []

        described: List[Dict] = []
        dest_dir = ATTACHMENTS_DIR / str(message.id)

        for index, attachment in enumerate(attachments[:MAX_ATTACHMENTS_PER_MESSAGE]):
            entry = {
                "filename": attachment.filename,
                "content_type": getattr(attachment, "content_type", None),
                "size": getattr(attachment, "size", None),
                "path": None,
                "skipped": None,
            }

            size = entry["size"] or 0
            if size > MAX_ATTACHMENT_BYTES:
                entry["skipped"] = f"exceeds the {MAX_ATTACHMENT_BYTES} byte download limit"
                log.warning(
                    "Attachment %s on message %s skipped: %d bytes",
                    attachment.filename, message.id, size
                )
                described.append(entry)
                continue

            path = dest_dir / safe_attachment_name(attachment.filename, index)
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                await attachment.save(path)
                entry["path"] = str(path)
            except Exception as e:
                # One bad download must not cost the message. The text
                # still goes through, and the envelope says what failed.
                entry["skipped"] = f"download failed: {e}"
                log.error(
                    "Failed to download attachment %s on message %s: %s",
                    attachment.filename, message.id, e
                )

            described.append(entry)

        dropped = len(attachments) - len(described)
        if dropped > 0:
            described.append({
                "filename": f"<{dropped} more attachment(s)>",
                "content_type": None,
                "size": None,
                "path": None,
                "skipped": f"over the {MAX_ATTACHMENTS_PER_MESSAGE} attachment per message limit",
            })

        return described

    async def send_to_agent_server(self, message: discord.Message, agent: str):
        """Send message to agent server. On failure (non-202 response or a
        connection error — agent server down, rate-limited, etc.), spool
        it to DEFERRED_POKE_DIR instead of dropping it silently. Task #9,
        2026-08-06 — matches upstream karakos-package issue #88, which
        names a real dropped message on this install (2026-08-05, a 429
        with no retry) as the reproduction case."""
        channel_name = self.get_channel_name(str(message.channel.id))
        if not channel_name:
            channel_name = "unknown"

        attachments = await self.download_attachments(message)

        payload = {
            "agent": agent,
            "channel": channel_name,
            "channel_id": str(message.channel.id),
            "server": "discord",
            "author": message.author.display_name,
            "author_id": str(message.author.id),
            "is_bot": message.author.bot,
            "content": message.content,
            "message_id": str(message.id),
            "mentions_agent": any(m.id in discord_id_to_agent for m in message.mentions),
            "attachments": attachments,
        }

        ok, detail = await self._post_to_agent_server(payload)
        if ok:
            log.info(f"Queued message for {agent} from {message.author.display_name}")
        else:
            log.error(f"Agent server error, spooling for retry: {detail}")
            self._spool_deferred_poke(payload, detail)

    async def capture_message(self, message: discord.Message):
        """Capture message to JSONL"""
        channel_name = self.get_channel_name(str(message.channel.id))

        entry = {
            "v": 1,
            "ts": datetime.now().isoformat(),
            "channel": "discord",
            "channel_id": str(message.channel.id),
            "channel_name": channel_name or "unknown",
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "is_bot": message.author.bot,
            "content": message.content,
            "message_id": str(message.id),
            # Names only — the capture log is a record of what was said,
            # and a message whose whole payload was a file reads as blank
            # without this.
            "attachments": [a.filename for a in getattr(message, "attachments", None) or []],
        }

        # Write to daily JSONL
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_file = MESSAGES_DIR / f"messages-{date_str}.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def write_health_heartbeat(self):
        """Write health heartbeat"""
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_FILE, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "status": "healthy"
            }, f)

    def write_presence_snapshot(self):
        """Full rewrite of data/presence.json from current member cache.

        2026-08-18: called on_ready (initial population) and on every
        presence_update (member counts here are small — a handful of
        real users/bots across two guilds — so a full rewrite per event
        is simpler and safer than patching one entry in place, no risk of
        the file drifting from actual gateway state.
        """
        members = {}
        for guild in self.guilds:
            for member in guild.members:
                activity = None
                if member.activity is not None:
                    activity = {
                        "type": type(member.activity).__name__,
                        "name": getattr(member.activity, "name", None),
                    }
                key = f"{guild.name}:{member.name}"
                members[key] = {
                    "user_id": str(member.id),
                    "name": member.name,
                    "bot": member.bot,
                    "status": str(member.status),
                    "activity": activity,
                }
        PRESENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRESENCE_FILE, "w") as f:
            json.dump({
                "updated_at": datetime.now().isoformat(),
                "members": members,
            }, f, indent=2)

    async def on_presence_update(self, before, after):
        """Live presence changes — keep data/presence.json current."""
        self.write_presence_snapshot()

    async def _status_poll_loop(self):
        """Poll STATUS_FILE and apply it to the live gateway connection.

        Polling rather than an inotify-style watch because the writer
        (set_status.py) runs in agent-server, a separate process — a
        plain poll is the simplest thing that can't miss an update
        regardless of which process wrote it or when. 10s default is
        fast enough that a 'going dark' post and the presence change
        land close enough together to read as the same event, cheap
        enough not to matter at this frequency.
        """
        try:
            # Apply immediately on connect too, not just after the first
            # sleep — a relay restart mid-dark-session (e.g. the
            # sanctioned admin restart path) should re-show the saved
            # state right away rather than default back to Online and
            # silently misrepresent an in-progress dark stretch for up
            # to STATUS_POLL_INTERVAL_SEC.
            await self._apply_status_file()
            while not self.is_closed():
                await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)
                if not self.is_closed():
                    await self._apply_status_file()
        except asyncio.CancelledError:
            pass

    async def _apply_status_file(self):
        """Read STATUS_FILE and call change_presence() iff it changed
        since the last poll. No file yet == leave Discord's own default
        (Online, no activity) alone, which is already the right resting
        state for quick-think/short-task work."""
        if not STATUS_FILE.exists():
            return
        try:
            with open(STATUS_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            log.exception("Failed to read %s", STATUS_FILE)
            return

        state = data.get("state")
        activity_text = data.get("activity")
        discord_status = _STATUS_TO_DISCORD.get(state)
        if discord_status is None:
            log.warning("Unknown status state %r in %s, ignoring", state, STATUS_FILE)
            return

        key = (state, activity_text)
        if key == self._last_applied_status:
            return  # unchanged since last poll, don't spam change_presence

        activity = discord.CustomActivity(name=activity_text) if activity_text else None
        try:
            await self.change_presence(status=discord_status, activity=activity)
            self._last_applied_status = key
            log.info("Applied presence: status=%s activity=%r", state, activity_text)
        except Exception:
            log.exception("Failed to apply presence from %s", STATUS_FILE)

    async def close(self):
        """Cleanup on shutdown"""
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        if self._status_task and not self._status_task.done():
            self._status_task.cancel()
        if self.http_session:
            await self.http_session.close()
        await super().close()

# =============================================================================
# Dispatch Adapter
# =============================================================================

class DispatchAdapter:
    """Watch inbox directories and invoke builder/reviewer scripts"""

    def __init__(self):
        self.running = False
        self.task = None

        # Initialize semaphores
        dispatch_semaphores["builder"] = asyncio.Semaphore(MAX_CONCURRENT_BUILDERS)
        dispatch_semaphores["reviewer"] = asyncio.Semaphore(MAX_CONCURRENT_REVIEWERS)

    async def start(self):
        """Start dispatch polling loop"""
        self.running = True
        self.task = asyncio.create_task(self.poll_loop())
        log.info("Dispatch adapter started")

    async def stop(self):
        """Stop dispatch adapter"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        # Wait for active dispatches
        if active_dispatches:
            log.info(f"Waiting for {len(active_dispatches)} active dispatches to complete")
            await asyncio.gather(*active_dispatches.values(), return_exceptions=True)

    async def poll_loop(self):
        """Poll inbox directories for new briefs"""
        while self.running:
            try:
                await self.check_inboxes()
                await asyncio.sleep(DISPATCH_POLL_INTERVAL)
            except Exception as e:
                log.error(f"Dispatch poll error: {e}")
                await asyncio.sleep(DISPATCH_POLL_INTERVAL)

    async def check_inboxes(self):
        """Check inbox directories for new briefs"""
        for agent_type in ["builder", "reviewer"]:
            inbox_dir = DISPATCH_INBOX_DIR / agent_type
            if not inbox_dir.exists():
                continue

            # Find brief files
            briefs = sorted(inbox_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)

            for brief_file in briefs:
                # Check if already dispatched
                if brief_file.stem in active_dispatches:
                    continue

                # Try to acquire semaphore (non-blocking)
                semaphore = dispatch_semaphores.get(agent_type)
                if semaphore and semaphore._value > 0:
                    # Dispatch
                    task = asyncio.create_task(self.dispatch(agent_type, brief_file))
                    active_dispatches[brief_file.stem] = task
                    log.info(f"Dispatched {agent_type}: {brief_file.name}")

    async def dispatch(self, agent_type: str, brief_file: Path):
        """Dispatch brief to agent"""
        semaphore = dispatch_semaphores.get(agent_type)
        if not semaphore:
            return

        async with semaphore:
            try:
                # Read brief
                with open(brief_file) as f:
                    brief_content = f.read()

                # Parse frontmatter
                metadata = self.parse_frontmatter(brief_content)
                requester = metadata.get("requester", "unknown")
                callback_channel = metadata.get("callback_channel", "general")

                # Determine invoke script
                invoke_script = WORKSPACE_ROOT / "bin" / f"invoke-{agent_type}.sh"
                if not invoke_script.exists():
                    log.error(f"Invoke script not found: {invoke_script}")
                    return

                # Invoke script
                timeout = DISPATCH_TIMEOUTS.get(agent_type, 21600)
                log.info(f"Invoking {agent_type} for {brief_file.name} (timeout: {timeout}s)")

                proc = await asyncio.create_subprocess_exec(
                    str(invoke_script),
                    str(brief_file),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                    returncode = proc.returncode

                    if returncode == 0:
                        log.info(f"{agent_type} completed: {brief_file.name}")
                    else:
                        log.error(f"{agent_type} failed with code {returncode}: {brief_file.name}")
                        log.error(f"stderr: {stderr.decode()}")

                except asyncio.TimeoutError:
                    log.error(f"{agent_type} timed out: {brief_file.name}")
                    proc.kill()
                    await proc.wait()

                # Archive brief
                archive_dir = brief_file.parent / "archive"
                archive_dir.mkdir(exist_ok=True)
                brief_file.rename(archive_dir / brief_file.name)

            finally:
                # Remove from active dispatches
                active_dispatches.pop(brief_file.stem, None)

    def parse_frontmatter(self, content: str) -> Dict:
        """Parse YAML frontmatter from brief"""
        if not content.startswith("---"):
            return {}

        lines = content.split("\n")
        frontmatter_lines = []
        in_frontmatter = False

        for i, line in enumerate(lines):
            if i == 0 and line.strip() == "---":
                in_frontmatter = True
                continue
            if in_frontmatter:
                if line.strip() == "---":
                    break
                frontmatter_lines.append(line)

        # Simple key: value parser (not full YAML)
        metadata = {}
        for line in frontmatter_lines:
            if ":" in line:
                key, _, value = line.partition(":")
                metadata[key.strip()] = value.strip()

        return metadata

# =============================================================================
# Main
# =============================================================================

async def main():
    """Main relay service"""
    _acquire_singleton_lock("relay")
    log.info("Karakos relay starting")

    # Load config
    load_config()

    # Start dispatch adapter
    dispatch = DispatchAdapter()
    await dispatch.start()

    # Get primary agent's Discord token
    primary_agent = None
    for agent_name, config in agent_config.items():
        token_env = config.get("discord_bot_token_env")
        if token_env and os.environ.get(token_env):
            primary_agent = agent_name
            break

    if not primary_agent:
        log.warning("No Discord tokens configured, Discord adapter disabled")
        # Run dispatch-only mode
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            pass
        finally:
            await dispatch.stop()
        return

    # Start Discord bot
    token = os.environ.get(agent_config[primary_agent]["discord_bot_token_env"])
    discord_client = DiscordAdapter()

    # Graceful SIGTERM: reload-on-commit.py's safe-pkill.sh sends this when
    # relay.py/reply_gate.py changes land, so it needs an actual handler
    # rather than the default (immediate termination) — see
    # _graceful_shutdown for the drain logic. discord_client.start() is
    # called directly here rather than via Client.run(), which is the
    # sync wrapper that would normally install this signal handling for
    # us; doing it explicitly since we bypass that wrapper.
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(
            signal.SIGTERM,
            lambda: asyncio.ensure_future(_graceful_shutdown(discord_client)),
        )
    except NotImplementedError:
        # add_signal_handler is Unix-only; every deployment target so far
        # (container, native systemd) is Unix, so this is a defensive
        # fallback, not an expected path — logged so it's visible if that
        # ever changes rather than silently losing the whole fix.
        log.warning(
            "relay: loop.add_signal_handler unavailable on this platform — "
            "SIGTERM will fall back to immediate termination, no graceful drain"
        )

    try:
        # Run Discord bot (blocks until closed)
        await discord_client.start(token)
    except KeyboardInterrupt:
        log.info("Shutdown signal received")
    finally:
        await discord_client.close()
        await dispatch.stop()
        log.info("Relay shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
