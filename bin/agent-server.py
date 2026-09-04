#!/usr/bin/env python3
"""
Karakos Agent Server — Persistent Subprocess Architecture

Accepts messages via HTTP, queues to SQLite, sends to persistent claude
subprocess via stdin (stream-json), posts responses to Discord.

Port: 18791 (configurable via AGENT_SERVER_PORT env var)
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
from aiohttp import web

import speaking_banana as banana
import context_box
import voice_presence
from handoff import parse_handoff
from outbox import add_pending

# =============================================================================
# Configuration
# =============================================================================

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
PORT = int(os.environ.get("AGENT_SERVER_PORT", "18791"))
DB_PATH = WORKSPACE_ROOT / "data" / "memory" / "agent-server.db"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.json"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
STREAM_LOG_DIR = Path(os.environ.get("KARAKOS_LOG_DIR", str(WORKSPACE_ROOT / "logs"))) / "agent-streams"
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
OWNER_DISCORD_ID = os.environ.get("OWNER_DISCORD_ID", "0")

# Cost tracking — informational only as of 2026-08-06. The dollar-based
# daily cap used to hard-reject inbound messages at COST_DAILY_LIMIT,
# which silently dropped agent-to-agent messages with no retry (found via
# a real incident: $53.97 actual spend vs a $25 cap, one of Amos's
# messages 429-rejected and lost). Ian's decision: adopt Amos's model
# instead — no dollar cap, protection comes from QUEUE_DEPTH_LIMIT below
# (already matched his number exactly) plus automatic context compaction
# (CONTEXT_WINDOW_TOKENS / COMPACTION_TARGET_TOKENS below). These limits
# are still recorded and still drive the #cost channel post, just no
# longer used to reject anything.
COST_DAILY_LIMIT = float(os.environ.get("COST_DAILY_LIMIT", "25.00"))
COST_MONTHLY_LIMIT = float(os.environ.get("COST_MONTHLY_LIMIT", "500.00"))
COST_WARNING_THRESHOLD = float(os.environ.get("COST_WARNING_THRESHOLD", "0.75"))

# Queue limits
QUEUE_DEPTH_LIMIT = 50
TYPING_INTERVAL = 8  # seconds

# Queued-ack sweep (Task #13, 2026-08-06 — deterministic "still here"
# notice for a channel waiting behind a busy turn on a different
# channel, since the per-channel typing indicator alone doesn't cover a
# multi-minute wait). Design worked out with Amos, corrected once by him
# before landing here:
#   - 45s wait gate does the anti-spam work by itself — a channel only
#     qualifies once it has genuinely been ignored that long, so this
#     doesn't need a conservative cooldown stacked on top of it.
#   - 10min cooldown, not the 30min originally proposed: at 30min, a
#     second long turn inside the same half hour goes silent again —
#     exactly the case the ack exists for (someone waiting through
#     back-to-back turns). 10min caps it at 6/hour worst case, and every
#     one of those is a real 45s+ wait, not noise.
QUEUED_ACK_WAIT_THRESHOLD_SEC = 45
QUEUED_ACK_COOLDOWN_SEC = 600
QUEUED_ACK_SWEEP_INTERVAL_SEC = 15

# Automatic context compaction — replaces the dollar cap as the real
# protection against runaway sessions. Triggered per-agent after a turn
# completes (never mid-turn), via finalize (summarize-session.py) then a
# full session reset so the next turn starts fresh with the summary
# injected by load_last_session().
#
# CONTEXT_WINDOW_TOKENS corrected 2026-08-07: was hardcoded to 200_000
# ("200k is Sonnet's context window") — stale. Live current-model Sonnet
# (both 4.6 and 5) is a 1M-token window; 200k was true for older Sonnet
# generations this comment was presumably written against. Caught live
# when a real session legitimately reached 452,145 estimated tokens and
# reported as "226% of window" — not a measurement bug this time (that
# one was fixed 2026-08-06), just the wrong denominator.
CONTEXT_WINDOW_TOKENS = 1_000_000

# COMPACTION_TARGET_TOKENS re-based 2026-08-07 (same day as the window
# fix above, second pass, per Ian): compact at a flat 200k rather than a
# fraction of the 1M window. Not a capability limit — it's "keep it
# where it already felt comfortable" carried over from his Sonnet 4.6
# usage, back when 200k *was* the real ceiling. Explicitly a soft
# target: a failed attempt (see maybe_compact_session) isn't an
# emergency, it just retries on the next turn same as before. Was 0.5 *
# CONTEXT_WINDOW_TOKENS (500k) for about half a day — that number is now
# CONTEXT_CONCERN_TOKENS below, i.e. "the soft target fired and failed
# enough times to be worth a second look," not the trigger itself.
COMPACTION_TARGET_TOKENS = 200_000

# Tiered context warnings, added 2026-08-07 per Ian, re-tiered same day
# once COMPACTION_TARGET_TOKENS moved down to 200k:
#   - SOFT (= COMPACTION_TARGET_TOKENS, 200k): the compaction trigger
#     itself now lives here — see maybe_compact_session. Tier label kept
#     for the heartbeat/status surface; it's no longer "visibility only."
#   - CONCERN (500k): the soft target fired at least one turn ago and
#     hasn't cleared — usually a repeated finalize failure (timeout /
#     no data). Informational: logged, surfaced in /agents and the
#     heartbeat, not paged.
#   - CRITICAL (800k): should never actually happen — the soft target at
#     200k, backstopped by the concern tier at 500k, should have reset
#     the session well before this. Reaching it means compaction has
#     been silently failing for a while, so this posts directly to
#     #signals rather than waiting for the next heartbeat.
CONTEXT_CONCERN_TOKENS = 500_000
CONTEXT_CRITICAL_WARNING_TOKENS = 800_000

# Topic-change compaction — second, independent trigger added 2026-08-07
# per Ian, alongside the token-target one above. Rationale: a continuous
# multi-channel dialogue changes topics often enough that a lot of
# context is "useless" well before 200k tokens, but checking every turn
# would be wasteful (a classifier call for zero signal on back-to-back
# messages seconds apart) and wrong (two messages seconds apart are
# essentially never a real topic change). Gated on a real gap instead —
# see maybe_topic_change_compact().
TOPIC_CHECK_GAP_SEC = 30 * 60
# Skip the check on small sessions — nothing "useless" has accumulated
# yet, and compacting a small session just burns a finalize call and a
# restart for no real benefit.
TOPIC_CHECK_MIN_TOKENS = 20_000

# Rate-limit circuit breaker (2026-08-07) — added after a real incident:
# both agents sat at 99% utilization / overageInUse=true from ~08:19 to
# 12:39, and the only "fix" was Ian manually telling Marvin to go quiet
# until the five-hour window reset, because nothing here actually gated
# on the rate_limit_event data already being recorded. Originally keyed
# solely off Anthropic's own "allowed_warning" status (their
# surpassedThreshold, typically 90%) rather than a locally-chosen
# utilization cutoff. 2026-08-08 per Ian: added an explicit utilization
# threshold too (RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD below) — status is
# still checked first in is_rate_limit_paused() since it's the cheaper
# check and covers the common case. Since the cost-model migration
# removed the $/day cap, there's no other backstop against burning
# overage dollars.
RATE_LIMIT_PAUSE_STATUS = "allowed_warning"
# Anthropic's status enum is "allowed" | "allowed_warning" | "rejected"
# (confirmed against the CLI's own zod schema, TOm, 2026-08-08) — "rejected"
# means a request was already denied, a strictly stronger signal than the
# warning. Found live tonight (Marvin 20:11:42, relay 20:22:56) right at the
# tail of a five_hour window, with utilization absent from the payload both
# times — utilization is genuinely optional in Anthropic's schema, not
# reliably present, so it can't be the only hard-stop signal.
RATE_LIMIT_REJECTED_STATUS = "rejected"
# 2026-08-08 (second revision, per Ian): status == RATE_LIMIT_PAUSE_STATUS
# used to hard-pause the queue by itself. Anthropic sets that status around
# 90% utilization ("surpassedThreshold, typically 90%" — see below), which
# meant a real incident (agent jammed with utilization still well under
# the 97% cutoff and plenty of window left) triggered a full stop far
# earlier than intended. Anthropic's status is now a WARNING signal only —
# see is_rate_limit_warning() — logged and notified but not blocking.
# is_rate_limit_paused() below is the only hard stop, and it's keyed
# purely on utilization crossing RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD.
RATE_LIMIT_WARNING_UTILIZATION_THRESHOLD = 0.90
# The hard stop. Utilization-only (not status) so it can't fire early off
# Anthropic's own ~90% warning flag — see comment above. Also the trigger
# point for proactive wind-down/compaction (see maybe_rate_limit_compact())
# so a session summarizes itself before the pause actually holds the
# queue, rather than freezing mid-thought.
#
# 0.97 -> 0.95, 2026-08-29 (Ian, after the 04:05 UTC crash): a manual
# override resumed Marvin mid-warning-zone, utilization re-crossed 97%
# three minutes later, and the second compact-ahead-of-pause attempt
# only had ~5s of runway before Anthropic's hard `rejected` landed — the
# summarizer subprocess itself failed (real nonzero exit in ~3s, not the
# 45s timeout — consistent with that call getting caught by the same
# limit it was racing). 95% buys roughly two more points of headroom,
# enough to give one plausible manual override room to still land a
# clean compaction instead of racing the cutoff. See
# facts/usage-report-percentage-fix-2026-08-29.md for the fuller
# incident writeup (that fact covers the /sys usage display fix from the
# same incident, not this threshold).
RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD = 0.95
# Heartbeats (poke.sh --source heartbeat, see heartbeat.sh) are exempted
# from the pause below (2026-08-07, per Ian/Moon Problem): the pause trips
# at Anthropic's own warning threshold, well short of 100%, so there's
# always enough headroom left in the window to cover a heartbeat's small
# cost — even with overages disabled. Must match heartbeat.sh's --source
# value exactly; that value becomes message_queue.author via handle_message.
RATE_LIMIT_HEARTBEAT_AUTHOR = "heartbeat"
# Backlog-gap callout threshold (2026-08-29) — see the comment at the
# callout's insertion point in process_agent_queue for why this exists.
# 5 minutes: loose enough that a normal fast multi-message exchange never
# trips it, tight enough to catch a real pause/restart/outage gap.
BACKLOG_GAP_THRESHOLD_SEC = 300
# Entry-side mid-turn steering threshold (2026-08-30, Ian's design —
# see the injection point in process_agent_queue). Short on purpose:
# this is "is the message I'm about to answer already old by the time
# I've picked it up," not the backlog callout's "was there a real gap" —
# ordinary queue/processing lag should almost never trip it, so 30s
# catches "busy elsewhere / just resumed" without firing on routine
# turnaround time.
ENTRY_STALE_THRESHOLD_SEC = 30
# How often rate_limit_gate_sweep_loop retries a paused agent's queue.
# process_agent_queue() is otherwise only triggered reactively (a new
# message arrives, or another channel is left queued after a drain) —
# nothing re-triggers it just because a five-hour window happened to
# reset, so a paused agent needs its own clock, same reasoning as
# QUEUED_ACK_SWEEP_INTERVAL_SEC above.
RATE_LIMIT_GATE_SWEEP_INTERVAL_SEC = 60

# Claude CLI's own hard-stop when the Anthropic *account's* monthly spend
# cap is hit — a different failure mode than everything else in this
# block, which is all about the five-hour/seven-day rate-limit window.
# This one doesn't self-resolve on a timer; it needs Ian to raise the cap
# at claude.ai/settings/usage. Real incident 2026-08-18: the CLI subprocess
# returned this as a bare `result`/`error` string with no assistant
# content at all, and before this fix it went straight through the normal
# reply path and got posted to #signals verbatim, once per 30-minute
# heartbeat, for ~2.5h before anyone caught it (05:56-08:27 UTC). Matched
# on this URL fragment from Anthropic's own raise-limit link rather than
# the surrounding prose, which could plausibly appear in ordinary
# conversation about spend/limits (this codebase's own comments included).
CLI_SPEND_LIMIT_SIGNATURE = "cc_cli_limit_message"

# Headroom tracking (2026-08-09, ported/adapted from
# mcarmody/karakos-package#128) — `cost_events`/`/cost` track dollars;
# dollars are not what stops an agent mid-sentence, the rate limit is,
# and until now the only visibility into it was status/utilization
# (above), which Amos's instance confirmed can be entirely absent from
# the CLI's rate_limit_event (no utilization field at all, ever — see
# the #agent-chat exchange 2026-08-08 23:38). Window *time* progress is
# the one thing resetsAt always gives us even then, so it's tracked as
# an independent, complementary signal — NOT a replacement for the
# status/utilization checks above, and deliberately not its own table:
# reuses the existing `rate_limits` row per agent (see
# _record_rate_limit_event) rather than the parallel `rate_limit_state`
# table upstream added, since every field it needs is already there.
RATE_LIMIT_WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 86400,
}
# Fraction of the window elapsed at which is_rate_limit_warning() also
# fires (OR'd with the status/utilization checks it already does) — an
# agent sitting near the end of its window with no utilization reading
# at all (Amos's case) still gets a signal instead of none.
RATE_LIMIT_WINDOW_PROGRESS_WARNING_FRACTION = 0.80

# Processing states
STATUS_QUEUED = 0
STATUS_IN_PROGRESS = 1
STATUS_COMPLETE = 2
STATUS_CRASHED = 3
STATUS_SKIPPED = 4

# Session persistence
SUMMARY_DIR = WORKSPACE_ROOT / "logs" / "session-summaries"
LAST_SUMMARY_TEMPLATE = WORKSPACE_ROOT / "data" / "last-session-summary-{agent}.md"

# Logging
STREAM_LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("agent-server")
log.setLevel(logging.INFO)
# Guard against duplicate handlers (2026-08-29). logging.getLogger() by
# name returns the same process-global logger object every time — but
# this module is loaded by tests/conftest.py's import_script() via
# importlib.util.spec_from_file_location, deliberately *outside*
# sys.modules caching ("allows reload with different env"), so this
# module-level block reruns on every test file that imports it. Without
# the guard, each rerun appended another handler pair to the same
# logger, so one log call fired once per prior import. Real incident:
# a single pytest run (8 test files import this module) produced dozens
# of duplicate lines in the real production log, misread as an active
# Discord outage. KARAKOS_LOG_DIR lets tests (see conftest.py) point the
# file handler at a throwaway directory instead of the real one.
if not log.handlers:
    log_dir = Path(os.environ.get("KARAKOS_LOG_DIR", str(WORKSPACE_ROOT / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "agent-server.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(handler)

    # Also log to console
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(console)

# Regex patterns
THINKING_BLOCK_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)

# Silence-announcement suppression (2026-09-02, Amos flagged this from
# outside: 12 of Marvin's last 13 #agent-chat messages were real posts
# explaining a decision NOT to reply — "Not addressed to me — that ping is
# Amos's ID. No reply.", "(no output — #agent-chat remains under Ian's
# standing pause)", "Sitting this one out...". reply_gate.py's own docstring
# already states the intended rule in prose: "Silence should be the normal
# state between two agents — it means nothing is needed." The gap is that
# nothing in the CODE enforces it. `pending_final and channel_id != "0"` at
# the post_to_discord call site posts anything non-empty, with no concept of
# "this turn's answer IS silence, not a message about silence."
#
# Two layers, cheapest first:
#   1. PASS_SENTINEL_RE — an explicit, forward-looking convention (matches
#      Amos's own side's identical fix, same day): a reply that opens or
#      closes with a bare, all-caps PASS is suppressed outright. Requires
#      the model to actually say PASS, which is a prompt-level change this
#      PR does not make — the check is here so that once it does, it works.
#   2. SILENCE_ANNOUNCEMENT_RE — a heuristic net for the CURRENT phrasing,
#      built directly from the 12 real examples above, not a general
#      sentiment classifier. Three shapes only, each deliberately narrow
#      to avoid catching a real answer that happens to share an opening
#      phrase (e.g. "That mention resolves to a real question worth
#      answering" must NOT be suppressed — it's an actual reply):
#        (a) the entire message is one parenthetical aside (`(no
#            reply...)`, optionally wrapped in `*...*` for italics — 6 of
#            12 real examples were exactly this shape);
#        (b) the message opens with "Not addressed to me" / "Not my
#            mention" / "Not mine to take" / "Sitting this one out" —
#            phrases that, in this exact leading position, essentially
#            only ever mean "declining to answer";
#        (c) the message opens with a mention-resolution phrase ("That
#            mention resolves to...", "Same reason as...") AND contains an
#            explicit "not me"/"not mine"/"holding"/"staying quiet"
#            nearby (within ~100 chars) — these two openers are NOT
#            silence-specific on their own (a real answer could start
#            "That mention resolves to a real question..."), so they only
#            count combined with an actual negation, not on the opener
#            alone.
#      This is a stopgap net for existing behavior, not a substitute for
#      #1 — expect to delete this once the model reliably emits PASS
#      instead.
PASS_SENTINEL_RE = re.compile(r"(^PASS\b|\bPASS[.!?]*$)")
SILENCE_ANNOUNCEMENT_RE = re.compile(
    r"^\*?\(.*\)\*?$"                                              # (a) whole message is a parenthetical aside
    r"|^(Not (my mention|mine to take|addressed to me)"           # (b) unambiguous leading phrases
    r"|Sitting this one out)"
    r"|^(That mention resolves to|Same reason as)"                # (c) opener ...
    r".{0,100}?\b(not (me|mine)|holding|hold off|standing down|staying quiet|sitting? out)\b",  # ... + nearby negation, combined only
    re.IGNORECASE | re.DOTALL,
)


def is_silence_announcement(text: str) -> bool:
    """True if `text` is a turn explaining that it decided not to reply,
    rather than an actual reply. See the comment above PASS_SENTINEL_RE."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(PASS_SENTINEL_RE.search(stripped) or SILENCE_ANNOUNCEMENT_RE.match(stripped))

# Singleton-instance guard (2026-08-07) — see the matching guard in
# relay.py / scheduler.py for the full incident writeup
# (agent-server-duplicate-process-incident.md). agent-server.py already
# failed loudly when duplicated during that incident (port 18791 already
# bound), so this isn't covering a silent-duplication gap the way the
# relay/scheduler guards are — it's here so that failure is an explicit,
# clear log line instead of an aiohttp bind traceback, and as a backstop
# against any future config change (different port per env, SO_REUSEPORT,
# etc.) that could make a port conflict stop being a reliable guard.
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

# =============================================================================
# Global State
# =============================================================================

db: Optional[aiosqlite.Connection] = None
http_session: Optional[aiohttp.ClientSession] = None
agent_config: Dict[str, Dict[str, Any]] = {}
channels_config: Dict[str, Any] = {}
agent_processes: Dict[str, asyncio.subprocess.Process] = {}
agent_locks: Dict[str, asyncio.Lock] = {}
agent_states: Dict[str, str] = {}
# Fire-and-forget tasks (notifications, queue kicks, sweep loops) need a
# strong reference held *somewhere* or the event loop's weak-ref-only
# bookkeeping is free to garbage-collect them mid-execution, before
# whatever they were awaiting (e.g. the Discord POST inside
# _notify_rate_limit_pause / _notify_spend_limit) ever completes — no
# exception, no log line, the notification just never happens. Real
# incident: the 2026-08-28 22:02 UTC rate-limit pause never posted to
# #signals; root-caused to exactly this pattern (bare `asyncio.
# create_task()` calls whose return value was discarded). asyncio's own
# docs warn about this explicitly. Every task nothing else references
# should be spawned via _spawn() below instead of calling
# asyncio.create_task() directly.
_background_tasks: "set[asyncio.Task]" = set()

def _spawn(coro, name: Optional[str] = None) -> asyncio.Task:
    """asyncio.create_task() wrapper that keeps a strong reference until
    the task finishes (so it can't be collected early) and logs — instead
    of silently swallowing — any exception it raised. Use for every
    fire-and-forget task; see the note above."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            # graceful_shutdown() (SIGTERM/SIGINT handler) intentionally
            # ends with sys.exit(0) — its normal, successful exit path, not
            # a crash. asyncio.Task.exception() surfaces SystemExit like
            # any other exception though, so every clean restart/reload of
            # the whole agent-server process was logging as
            # "Background task ... failed: SystemExit(0)" — found
            # 2026-08-29 during a routine log sweep, at least 3 occurrences
            # that day alone, all correlating with intentional restarts,
            # none an actual failure. A non-zero exit code is still worth
            # a real log line (something asked to exit unhappily), just
            # not at ERROR-with-traceback severity that a log scan would
            # mistake for a crash.
            if isinstance(exc, SystemExit):
                if exc.code not in (0, None):
                    log.warning(f"Background task {t.get_name()} exited with code {exc.code}")
                return
            log.error(f"Background task {t.get_name()} failed: {exc!r}", exc_info=exc)

    task.add_done_callback(_on_done)
    return task

async def _voice_presence_log(agent: str, channel: str, text: str) -> None:
    """Thread-offload wrapper around voice_presence.log_score() — embedding
    inference (and the one-time model load) is synchronous/CPU-bound, and
    this runs via _spawn() as a background task sharing the same event
    loop as every other agent's turn processing, so it goes through
    asyncio.to_thread() rather than blocking that loop directly."""
    await asyncio.to_thread(voice_presence.log_score, agent, channel, text)

response_buffers: Dict[str, str] = {}
agent_last_cost: Dict[str, float] = {}
agent_sessions: Dict[str, str] = {}
typing_tasks: Dict[str, asyncio.Task] = {}
agent_todo_lists: Dict[str, List[Dict]] = {}
# Anthropic's own live rate-limit signal, straight off the stream-json
# `rate_limit_event` (2026-08-06 — Ian's ask, per Amos: "if you only ship
# one, ship this one, it's Anthropic answering the question directly,
# where everything else we're both doing is inferring it"). Kept
# in-memory for cheap access alongside the persisted copy in the
# rate_limits DB table (see init_db / _record_rate_limit_event).
#
# 2026-09-03: restructured from Dict[agent, info] to
# Dict[agent, Dict[rate_limit_type, info]] (task-1788454188) — a flat
# per-agent dict meant a seven_day event and a five_hour event fought
# over the same slot, so only whichever arrived most recently was ever
# visible. Code that wants the single "how urgent is this right now"
# signal (the pause/warning gates, the notices, /agents' backward-compat
# `rate_limit` field) should call _rate_limit_primary(agent) rather than
# reaching into this dict directly. Code that wants the full breakdown
# (format_usage_report, /usage's `windows` field) reads this directly.
agent_rate_limits: Dict[str, Dict[str, Dict[str, Any]]] = {}
# Tracks whether we've already posted the #signals pause/resume notice
# for the agent's *current* pause episode, so rate_limit_gate_sweep_loop
# retrying every 60s doesn't spam a notice on every retry while still
# paused. See is_rate_limit_paused() / process_agent_queue().
agent_rate_limit_pause_notified: Dict[str, bool] = {}
# Same idea for the (non-blocking) warning zone — added 2026-08-08 so
# crossing ~90% posts one #signals notice instead of one per sweep tick,
# and posts again on clearing. See is_rate_limit_warning().
agent_rate_limit_warning_notified: Dict[str, bool] = {}
# Context-window fill estimate, same inputs as maybe_compact_session() but
# purely observational — 2026-08-07, Ian's ask, tracked separately from the
# (still deliberately disabled) auto-compaction trigger so visibility
# doesn't require flipping that behavior on. In-memory only, one entry per
# agent, always overwritten — "how full is the session right now."
agent_context_usage: Dict[str, Dict[str, Any]] = {}
# Whether the agent's most recent turn hit the monthly spend cap (see
# CLI_SPEND_LIMIT_SIGNATURE) — cleared the moment a subsequent turn
# produces a real response. agent_spend_limit_notified dedupes the
# #signals alert the same way agent_rate_limit_pause_notified does, so a
# blocked agent gets one alert per block episode, not one per heartbeat.
agent_spend_limit_blocked: Dict[str, bool] = {}
agent_spend_limit_notified: Dict[str, bool] = {}
# Rate-limit override (2026-08-10, Ian's ask: "bugfixes regardless of
# session limits, at my discretion"). An owner-set, auto-expiring bypass
# of is_rate_limit_paused() for exactly one agent at a time — for the
# rare case where Ian wants to push a fix through during a pause instead
# of waiting for the window to reset. Deliberately NOT a permanent
# setting: every override has a hard expiry (capped at
# RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC even if a longer duration is
# requested) so a forgotten override can't quietly disable the circuit
# breaker forever. In-memory cache backed by the rate_limit_overrides
# DB table (see init_db / _load_rate_limit_overrides_from_db) so it
# survives a restart instead of silently clearing one mid-use.
agent_rate_limit_overrides: Dict[str, Dict[str, Any]] = {}
RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC = 3600  # 1 hour hard cap, non-negotiable
# Per-(agent, channel_id) last-turn bookkeeping for the topic-change
# compaction trigger (maybe_topic_change_compact, 2026-08-07) — "at" is
# epoch seconds, "text" is that turn's formatted message content, used
# as the "before" side of the classifier comparison the next time this
# same channel comes back after a gap. Deliberately scoped per channel,
# not per agent — see maybe_topic_change_compact()'s docstring. In-memory
# only; a restart just means the next turn in each channel establishes a
# fresh baseline instead of comparing against pre-restart content, which
# is fine (a restart already means compaction happened).
agent_channel_last_turn: Dict[tuple, Dict[str, Any]] = {}
active_todo_messages: Dict[str, Dict] = {}
# Per-channel cooldown tracking for the queued-ack sweep (Task #13). Not
# persisted — a restart clearing this is fine, worst case one channel
# gets an extra ack sooner than it strictly needed to.
channel_last_ack: Dict[str, float] = {}
# Which message_id each channel's last ack was actually *for* (2026-08-18).
# The 10min cooldown above was designed for back-to-back busy turns, where
# the oldest queued message changes between acks — it was never meant to
# let the SAME stuck message get re-acked every 10 minutes indefinitely.
# During a multi-hour rate-limit pause the oldest message doesn't change,
# so the cooldown alone let one queued message rack up ten near-identical
# "-# ⏳ queued — paused..." posts in #agent-chat over 05:56-08:27 UTC.
# Keying on message_id caps it at one ack per distinct queued message —
# a new message still gets its own ack (subject to the existing cooldown),
# but a message that's still the same one 10 minutes later doesn't.
channel_last_acked_message_id: Dict[str, str] = {}

# Discord token mapping
AGENT_TOKENS: Dict[str, str] = {}
DISCORD_ID_TO_AGENT: Dict[int, str] = {}

# Graceful shutdown flag
shutting_down = False

# Per-process identity, generated fresh on import (i.e. once per actual OS
# process, including every restart) — 2026-08-30, fixes /sys restart-server
# reporting "done in 1s" when real recovery takes 1-2 minutes. graceful_
# shutdown() keeps the aiohttp server (and /health) answering throughout its
# own cleanup — waiting for idle, generating session summaries, killing
# subprocesses — right up until sys.exit(0) at the very end, so a poll loop
# that just checks "/health responds with matching session_ids" gets a false
# positive almost immediately: it's still talking to the dying OLD process,
# which trivially matches itself. See _sys_restart_server() in relay.py,
# which now requires this to actually change before declaring victory.
SERVER_BOOT_ID = str(uuid.uuid4())

# =============================================================================
# Database Schema
# =============================================================================

async def init_db():
    """Initialize database schema"""
    global db
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row

    # Message queue table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            channel TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            server TEXT DEFAULT 'discord',
            author TEXT NOT NULL,
            author_id TEXT DEFAULT '0',
            is_bot INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            message_id TEXT UNIQUE NOT NULL,
            mentions_agent INTEGER DEFAULT 0,
            attachments TEXT,
            processed INTEGER DEFAULT 0,
            response TEXT,
            discord_response_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processing_started_at TIMESTAMP,
            processed_at TIMESTAMP
        )
    """)
    # 2026-08-09: attachments added alongside relay.py's attachment-download
    # support (ported from mcarmody/karakos-package#127). CREATE TABLE IF
    # NOT EXISTS is a no-op on an already-live table, so an upgraded
    # install only gets this column through the migration below; without
    # it, every INSERT on an existing install (see handle_message) would
    # 500 the moment relay.py started sending the new field. Same
    # try/except pattern as the rate_limits.utilization migration above —
    # ALTER TABLE has no "IF NOT EXISTS" in sqlite.
    try:
        await db.execute("ALTER TABLE message_queue ADD COLUMN attachments TEXT")
        await db.commit()
    except Exception:
        pass  # column already exists

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_agent
        ON message_queue(agent, processed, created_at)
    """)

    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_pending
        ON message_queue(processed) WHERE processed = 0
    """)

    # Sessions table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            agent TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            compaction_count INTEGER DEFAULT 0,
            last_compacted TIMESTAMP
        )
    """)

    # Cost events table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            cost_delta REAL,
            session_total REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            duration_ms REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Rate-limit state table (2026-08-06) — Anthropic's own live signal
    # for account usage against the five_hour/weekly windows, straight off
    # the CLI's stream-json `rate_limit_event`. History isn't the point
    # here, "are we close to the edge right now" is. See
    # _record_rate_limit_event() in read_agent_response().
    #
    # 2026-09-03 (task-1788454188): this used to be `agent TEXT PRIMARY
    # KEY` — one row per agent, period, regardless of which window the
    # event was even about. A seven_day rate_limit_event would silently
    # clobber a five_hour reading and vice versa, so the two windows could
    # never both be known at once — the actual blocker on showing a
    # dual-bar usage report like Amos's. Composite key on (agent,
    # rate_limit_type) lets both windows live side by side. SQLite can't
    # ALTER a table's PRIMARY KEY in place, so detect the old shape and
    # rebuild rather than relying on CREATE TABLE IF NOT EXISTS, which
    # would just no-op against the already-live old table.
    async with db.execute("PRAGMA table_info(rate_limits)") as cursor:
        existing_cols = await cursor.fetchall()

    if not existing_cols:
        await db.execute("""
            CREATE TABLE rate_limits (
                agent TEXT NOT NULL,
                rate_limit_type TEXT NOT NULL,
                status TEXT,
                resets_at INTEGER,
                overage_status TEXT,
                overage_resets_at INTEGER,
                is_using_overage INTEGER DEFAULT 0,
                utilization REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (agent, rate_limit_type)
            )
        """)
        await db.commit()
    else:
        pk_cols = {c["name"] for c in existing_cols if c["pk"]}
        if pk_cols == {"agent"}:
            # Old single-row-per-agent shape. First make sure `utilization`
            # exists on it (2026-08-08 migration, same try/except-ALTER
            # pattern as everywhere else in this file, since ALTER TABLE
            # has no "IF NOT EXISTS" in sqlite) so the copy below always
            # has a column to select, even on an install that predates it.
            try:
                await db.execute("ALTER TABLE rate_limits ADD COLUMN utilization REAL")
                await db.commit()
            except Exception:
                pass  # column already exists
            log.warning(
                "Migrating rate_limits table from single-row-per-agent to "
                "composite (agent, rate_limit_type) primary key (task-1788454188)"
            )
            await db.execute("ALTER TABLE rate_limits RENAME TO rate_limits_old_pk")
            await db.execute("""
                CREATE TABLE rate_limits (
                    agent TEXT NOT NULL,
                    rate_limit_type TEXT NOT NULL,
                    status TEXT,
                    resets_at INTEGER,
                    overage_status TEXT,
                    overage_resets_at INTEGER,
                    is_using_overage INTEGER DEFAULT 0,
                    utilization REAL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (agent, rate_limit_type)
                )
            """)
            # A pre-migration row's rate_limit_type may be NULL (an install
            # that never saw a typed event yet) — land those under the
            # literal string "unknown" rather than NULL, since NULL in a
            # composite PK column would let duplicate (agent, NULL) rows
            # pile up instead of upserting cleanly next time.
            await db.execute("""
                INSERT INTO rate_limits
                    (agent, rate_limit_type, status, resets_at, overage_status,
                     overage_resets_at, is_using_overage, utilization, updated_at)
                SELECT agent, COALESCE(NULLIF(rate_limit_type, ''), 'unknown'),
                       status, resets_at, overage_status, overage_resets_at,
                       is_using_overage, utilization, updated_at
                FROM rate_limits_old_pk
            """)
            await db.execute("DROP TABLE rate_limits_old_pk")
            await db.commit()
        # else: already on the composite-key shape (a prior run of this
        # same migration) — nothing to do.

    # Rate-limit override table (2026-08-10) — see agent_rate_limit_overrides
    # comment above and is_rate_limit_override_active(). One row per agent,
    # always overwritten on a fresh /sys override (INSERT OR REPLACE), same
    # single-row-per-agent shape as rate_limits above.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_overrides (
            agent TEXT PRIMARY KEY,
            enabled_by TEXT NOT NULL,
            reason TEXT,
            enabled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at REAL NOT NULL
        )
    """)

    await db.commit()
    log.info("Database initialized")

# =============================================================================
# Configuration Loading
# =============================================================================

def _validate_agents_config(data: Any) -> Optional[str]:
    """Return None if `data` (parsed agents.json) looks structurally sound,
    else a short human-readable reason it doesn't."""
    if not isinstance(data, dict):
        return "not a JSON object"
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return "'agents' key missing or not an object"
    for name, cfg in agents.items():
        if not isinstance(cfg, dict):
            return f"agents.{name} is not an object"
    return None


def _validate_channels_config(data: Any) -> Optional[str]:
    """Return None if `data` (parsed channels.json) looks structurally
    sound, else a short human-readable reason it doesn't."""
    if not isinstance(data, dict):
        return "not a JSON object"
    channels = data.get("channels")
    if not isinstance(channels, dict):
        return "'channels' key missing or not an object"
    for name, cfg in channels.items():
        if not isinstance(cfg, dict) or "id" not in cfg:
            return f"channels.{name} missing required 'id' field"
    return None


async def _load_validated_config(path: Path, label: str, validator) -> Optional[dict]:
    """Load+parse+validate one config JSON file. Returns the parsed dict
    on success, or None on any failure (missing file, bad JSON, failed
    schema check) — callers keep whatever they already had in memory in
    that case instead of overwriting a working config with garbage, or
    letting a JSONDecodeError propagate out of load_config() and take
    down startup() (cold boot) or the /agents/{name}/register hot-reload
    path over a single bad edit.

    Failures alert #signals rather than just logging: a routing/agent
    roster change that silently didn't take effect is exactly the kind
    of thing that needs a human, not a line in a log nobody's tailing.

    Modeled on Aerial's git_sync last-known-good-config fallback
    (azylman/aerial, surfaced 2026-08-30 via task-1788075644) — this is
    the cheap, no-repo-surgery slice of that pattern: config validation
    with an LKGC fallback, independent of whether the full generic-
    engine/instance-config repo split ever happens.
    """
    if not path.exists():
        log.warning(f"{label} not found: {path}")
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"{label} failed to load ({e}) — keeping last-known-good config")
        add_pending(
            "signals",
            f"⚠️ `{label}` failed to parse ({e}) — agent-server kept the "
            f"last-known-good config in memory instead of applying this "
            f"edit. Fix the file; it'll pick up on the next reload. "
            f"<@{OWNER_DISCORD_ID}>",
        )
        return None
    reason = validator(data)
    if reason:
        log.error(f"{label} failed validation ({reason}) — keeping last-known-good config")
        add_pending(
            "signals",
            f"⚠️ `{label}` failed validation ({reason}) — agent-server kept "
            f"the last-known-good config in memory instead of applying this "
            f"edit. Fix the file; it'll pick up on the next reload. "
            f"<@{OWNER_DISCORD_ID}>",
        )
        return None
    return data


async def load_config():
    """Load agent and channel configuration from JSON files, validating
    each before applying it. See _load_validated_config for the LKGC
    fallback behavior on a missing/malformed/invalid file."""
    global agent_config, channels_config, AGENT_TOKENS, DISCORD_ID_TO_AGENT

    # Load agents config
    new_agents = await _load_validated_config(
        AGENTS_CONFIG_PATH, "config/agents.json", _validate_agents_config
    )
    if new_agents is not None:
        agent_config = new_agents.get("agents", {})
        log.info(f"Loaded configuration for {len(agent_config)} agents")
    elif not agent_config:
        log.error(f"No usable agents config yet (no prior good config to fall back to): {AGENTS_CONFIG_PATH}")

    # Load channels config
    new_channels = await _load_validated_config(
        CHANNELS_CONFIG_PATH, "config/channels.json", _validate_channels_config
    )
    if new_channels is not None:
        channels_config = new_channels
        log.info(f"Loaded {len(channels_config.get('channels', {}))} channel mappings")
    elif not channels_config:
        log.warning(f"No usable channels config yet (no prior good config to fall back to): {CHANNELS_CONFIG_PATH}")

    # Build Discord token map
    for agent_name, config in agent_config.items():
        token_env_var = config.get("discord_bot_token_env")
        if token_env_var:
            token = os.environ.get(token_env_var, "")
            if token:
                AGENT_TOKENS[agent_name] = token
                bot_id_env = config.get("discord_bot_id_env")
                if bot_id_env:
                    bot_id = os.environ.get(bot_id_env)
                    if bot_id:
                        DISCORD_ID_TO_AGENT[int(bot_id)] = agent_name

# =============================================================================
# Session Management
# =============================================================================

async def get_or_create_session(agent: str) -> str:
    """Get existing session ID or create new one"""
    async with db.execute(
        "SELECT session_id FROM sessions WHERE agent = ?", (agent,)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            return row["session_id"]

    # Create new session
    session_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO sessions (agent, session_id) VALUES (?, ?)",
        (agent, session_id)
    )
    await db.commit()
    log.info(f"Created new session for {agent}: {session_id}")
    return session_id

def session_transcript_exists(session_id: str) -> bool:
    """Check whether the Claude CLI already has a transcript for this
    session ID. `--session-id` only works for brand-new sessions; once a
    transcript file exists the CLI refuses with "Session ID ... is already
    in use", so callers must switch to `--resume` for that ID.
    """
    claude_projects_dir = Path.home() / ".claude" / "projects"
    return any(claude_projects_dir.glob(f"*/{session_id}.jsonl"))

async def clear_session(agent: str):
    """Clear agent session and create new ID"""
    session_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO sessions (agent, session_id, input_tokens, compaction_count)
        VALUES (?, ?, 0, 0)
        ON CONFLICT(agent) DO UPDATE SET
            session_id = ?,
            input_tokens = 0,
            compaction_count = 0,
            last_compacted = CURRENT_TIMESTAMP
        """,
        (agent, session_id, session_id)
    )
    await db.commit()
    agent_last_cost.pop(agent, None)
    log.info(f"Cleared session for {agent}, new ID: {session_id}")

async def update_session_tokens(agent: str, input_tokens: int):
    """Update session token count"""
    await db.execute(
        "UPDATE sessions SET input_tokens = ? WHERE agent = ?",
        (input_tokens, agent)
    )
    await db.commit()

def estimate_context_tokens(metadata: Dict[str, Any]) -> int:
    """Estimate the total context an agent is currently carrying. input_tokens
    alone is only the fresh (non-cached) portion of the last turn —
    cache_read_input_tokens is what actually reflects how much prior
    conversation got reused, which is the real signal for "how full is
    this session." cache_creation_input_tokens covers content newly
    written into the cache this turn. Summing all three is a
    same-order-of-magnitude estimate of total context, not an exact
    figure — good enough to trigger compaction before a session gets
    dangerously large, not precise enough to bill against.

    FIXED 2026-08-06: this used to receive cache_read_input_tokens summed
    off the top-level `result` stream-json event, which produced a
    physically impossible 9,682,204 on one real turn against a 200k
    window. Root cause (confirmed against Amos's equivalent
    last_assistant_usage(), same fix independently on his end): the
    `result` event's usage likely aggregates across every internal
    tool-call iteration within a turn, not just the last one — a turn
    with N iterations against an already-large cached context sums to
    roughly N times the real figure. read_agent_response() now populates
    metadata's cache_* fields from the LAST individual `assistant` stream
    event instead (see the comment there), so this function's inputs are
    correct without any change to the summing logic itself."""
    return (
        metadata.get("input_tokens", 0)
        + metadata.get("cache_read_input_tokens", 0)
        + metadata.get("cache_creation_input_tokens", 0)
    )

async def _record_rate_limit_event(agent: str, info: Dict[str, Any]) -> None:
    """Persist the CLI's own live rate-limit signal (2026-08-06). One row
    per (agent, rate_limit_type) — see the 2026-09-03 migration comment on
    the rate_limits table in init_db() — always overwritten within that
    window, this is "where do we stand right now," not a history. Logs a
    warning when we're not simply "allowed", or when overage is actually
    being spent, so it shows up in agent-server.log without needing
    anyone to go looking for it."""
    rate_limit_type = info.get("rateLimitType") or "unknown"
    agent_rate_limits.setdefault(agent, {})[rate_limit_type] = info
    status = info.get("status")
    is_using_overage = bool(info.get("isUsingOverage"))
    if status != "allowed" or is_using_overage:
        log.warning(
            f"{agent} rate limit: status={status} type={rate_limit_type} "
            f"resetsAt={info.get('resetsAt')} overage={info.get('overageStatus')} "
            f"isUsingOverage={is_using_overage}"
        )
    if db is None:
        return
    try:
        await db.execute(
            """
            INSERT INTO rate_limits
                (agent, rate_limit_type, status, resets_at, overage_status,
                 overage_resets_at, is_using_overage, utilization, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(agent, rate_limit_type) DO UPDATE SET
                status=excluded.status,
                resets_at=excluded.resets_at,
                overage_status=excluded.overage_status,
                overage_resets_at=excluded.overage_resets_at,
                is_using_overage=excluded.is_using_overage,
                utilization=excluded.utilization,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                agent,
                rate_limit_type,
                status,
                info.get("resetsAt"),
                info.get("overageStatus"),
                info.get("overageResetsAt"),
                1 if is_using_overage else 0,
                info.get("utilization"),
            ),
        )
        await db.commit()
    except Exception as e:
        log.warning(f"Failed to persist rate_limit_event for {agent}: {e}")

def _rate_limit_primary(agent: str) -> Optional[Dict[str, Any]]:
    """Pick which window's reading governs the single-value gates
    (is_rate_limit_paused, is_rate_limit_warning, maybe_rate_limit_compact,
    the pause/warning #signals notices, and the backward-compat flat
    fields on /agents and /usage) now that agent_rate_limits can hold more
    than one window per agent at once (task-1788454188). A `rejected`
    status wins outright regardless of window — that's Anthropic already
    denying a request, stronger than any utilization number. Otherwise
    the window with the highest utilization governs, since that's the one
    actually closest to tripping the pause threshold. Returns None only
    when nothing has been recorded for this agent at all."""
    windows = agent_rate_limits.get(agent)
    if not windows:
        return None
    for info in windows.values():
        if info.get("status") == RATE_LIMIT_REJECTED_STATUS:
            return info
    return max(windows.values(), key=lambda i: i.get("utilization") or 0)

async def _load_rate_limits_from_db() -> None:
    """Preload the last-known rate-limit status per agent at startup, so
    a restart mid-warning doesn't silently resume message processing —
    agent_rate_limits is otherwise empty until the next turn completes
    and a fresh rate_limit_event arrives, which is exactly the gap a
    compaction-triggered restart (or a /sys reload) would fall into
    while still over the threshold. Skips rows whose window has already
    expired (resets_at in the past) rather than trusting stale data from
    a prior window — a live rate_limit_event replaces this on the very
    next turn either way, this is only a bridge for the gap between
    restart and then."""
    if db is None:
        return
    try:
        now = time.time()
        async with db.execute(
            "SELECT agent, rate_limit_type, status, resets_at, utilization FROM rate_limits"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            resets_at = row["resets_at"]
            if resets_at and resets_at <= now:
                continue  # stale — window already over, let a live event set this
            rate_limit_type = row["rate_limit_type"] or "unknown"
            agent_rate_limits.setdefault(row["agent"], {})[rate_limit_type] = {
                "status": row["status"],
                "rateLimitType": row["rate_limit_type"],
                "resetsAt": resets_at,
                "utilization": row["utilization"],
            }
        # Hard-pause criteria is status=="rejected" OR utilization crossing
        # the threshold (see is_rate_limit_paused()) — restoring both (not
        # just status) is what lets a restart mid-pause stay paused instead
        # of silently resuming until the next live event. Logged once per
        # agent off the same _rate_limit_primary() selection the actual
        # gates use, rather than per-row, now that an agent can have more
        # than one live window restored at once.
        for agent in agent_rate_limits:
            info = _rate_limit_primary(agent)
            if not info:
                continue
            utilization = info.get("utilization")
            rate_limit_type = info.get("rateLimitType")
            if info.get("status") == RATE_LIMIT_REJECTED_STATUS:
                log.warning(
                    f"{agent} restored rate-limit pause state from DB "
                    f"on startup (status=rejected, type={rate_limit_type}) — "
                    "staying paused until it clears"
                )
            elif utilization and utilization >= RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD:
                log.warning(
                    f"{agent} restored rate-limit pause state from DB "
                    f"on startup ({utilization:.0%} utilization, type={rate_limit_type}) — "
                    "staying paused until it clears"
                )
            elif info.get("status") == RATE_LIMIT_PAUSE_STATUS:
                log.warning(
                    f"{agent} restored rate-limit warning state from DB "
                    f"on startup (type={rate_limit_type}) — not paused, "
                    "still under the hard-stop threshold"
                )
    except Exception as e:
        log.warning(f"Failed to preload rate_limits from DB (non-fatal): {e}")

async def _load_rate_limit_overrides_from_db() -> None:
    """Preload any still-active rate-limit override at startup, same
    reasoning as _load_rate_limits_from_db() above — a restart mid-override
    (e.g. a /sys reload while Ian's mid-fix) must not silently drop it and
    re-impose a pause he already explicitly bypassed. Skips rows that have
    already expired rather than trusting stale data; an expired override
    is just deleted, not resurrected."""
    if db is None:
        return
    try:
        now = time.time()
        async with db.execute(
            "SELECT agent, enabled_by, reason, expires_at FROM rate_limit_overrides"
        ) as cursor:
            rows = await cursor.fetchall()
        expired = []
        for row in rows:
            if row["expires_at"] <= now:
                expired.append(row["agent"])
                continue
            agent_rate_limit_overrides[row["agent"]] = {
                "enabled_by": row["enabled_by"],
                "reason": row["reason"],
                "expires_at": row["expires_at"],
            }
            log.warning(
                f"{row['agent']} restored active rate-limit override from DB "
                f"on startup (by {row['enabled_by']}, expires "
                f"{datetime.fromtimestamp(row['expires_at']).strftime('%H:%M UTC')})"
            )
        for agent in expired:
            await db.execute("DELETE FROM rate_limit_overrides WHERE agent = ?", (agent,))
        if expired:
            await db.commit()
    except Exception as e:
        log.warning(f"Failed to preload rate_limit_overrides from DB (non-fatal): {e}")

def is_rate_limit_override_active(agent: str) -> bool:
    """True if a non-expired owner override exists for this agent.
    Auto-expires lazily on read — no background sweep needed, evicted
    from the in-memory cache the moment anything checks it past its
    expiry, same shape as the rest of the rate-limit gate."""
    info = agent_rate_limit_overrides.get(agent)
    if not info:
        return False
    if time.time() >= info["expires_at"]:
        agent_rate_limit_overrides.pop(agent, None)
        return False
    return True

async def set_rate_limit_override(agent: str, enabled_by: str, duration_sec: float, reason: str = "") -> float:
    """Owner-set bypass of is_rate_limit_paused() for one agent, capped at
    RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC no matter what duration is
    requested. Returns the actual expires_at (epoch seconds) so the
    caller can report it back accurately even when the cap kicked in."""
    duration_sec = max(0.0, min(duration_sec, RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC))
    expires_at = time.time() + duration_sec
    if db is not None:
        await db.execute(
            "INSERT INTO rate_limit_overrides (agent, enabled_by, reason, enabled_at, expires_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?) "
            "ON CONFLICT(agent) DO UPDATE SET "
            "enabled_by=excluded.enabled_by, reason=excluded.reason, "
            "enabled_at=CURRENT_TIMESTAMP, expires_at=excluded.expires_at",
            (agent, enabled_by, reason, expires_at),
        )
        await db.commit()
    agent_rate_limit_overrides[agent] = {
        "enabled_by": enabled_by,
        "reason": reason,
        "expires_at": expires_at,
    }
    log.warning(
        f"Rate-limit override SET for {agent} by {enabled_by} "
        f"(reason: {reason or 'none given'}), expires "
        f"{datetime.fromtimestamp(expires_at).strftime('%H:%M UTC')} — "
        f"is_rate_limit_paused() will return False for this agent until then"
    )
    return expires_at

async def clear_rate_limit_override(agent: str) -> bool:
    """Cancel an active override early. Returns whether one existed."""
    existed = agent in agent_rate_limit_overrides
    if db is not None:
        await db.execute("DELETE FROM rate_limit_overrides WHERE agent = ?", (agent,))
        await db.commit()
    agent_rate_limit_overrides.pop(agent, None)
    if existed:
        log.warning(f"Rate-limit override CLEARED for {agent}")
    return existed

def rate_limit_window_progress(info: Optional[Dict[str, Any]], now: Optional[float] = None) -> Optional[float]:
    """Fraction (0.0-1.0) of the current rate-limit window that has elapsed.

    Ported from mcarmody/karakos-package#128. Returns None when it cannot
    be computed, which callers must render as "unknown" rather than as
    0%. A missing resetsAt, an unrecognised rateLimitType, or a reset
    time already in the past all land here — reporting any of them as
    "0% used" would be the exact failure this feature exists to prevent
    (a reassuring number standing in for no data), just dressed up as
    output instead of silence.
    """
    if not isinstance(info, dict):
        return None
    resets_at = info.get("resetsAt")
    window = RATE_LIMIT_WINDOW_SECONDS.get(info.get("rateLimitType"))
    if not isinstance(resets_at, (int, float)) or not window:
        return None

    now = time.time() if now is None else now
    remaining = resets_at - now
    if remaining <= 0:
        # The window is over; the next event will describe the new one.
        return None
    if remaining >= window:
        return 0.0
    return (window - remaining) / window


_USAGE_BAR_WIDTH = 20
_RATE_LIMIT_TYPE_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "7-day (all models)",
}

def _usage_bar(fraction: Optional[float]) -> str:
    """Render a Unicode-block progress bar, e.g. '████████░░░░░░░░░░░░'.
    None renders as a run of '?' rather than an empty (0%-looking) bar —
    same "unknown must never read as zero" rule as
    rate_limit_window_progress(), for the same reason: a reassuring blank
    bar standing in for no data is worse than visibly not knowing."""
    if fraction is None:
        return "?" * _USAGE_BAR_WIDTH
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * _USAGE_BAR_WIDTH)
    return "█" * filled + "░" * (_USAGE_BAR_WIDTH - filled)

def format_usage_report(agent: str, now: Optional[float] = None) -> str:
    """Render an agent's current rate-limit headroom for a human — the
    counterpart to cost-report.sh: that answers "what has this spent",
    this answers "how close is it to being cut off", which is the number
    that actually stops a turn mid-sentence. Never raises on a
    partial/missing reading.

    2026-09-03 (task-1788454188): rewritten from a single-line summary to
    a bracketed [SYS]-style block with one progress bar per known
    rate-limit window (five_hour, seven_day), matching the format Ian
    pointed at from Amos's instance (IMG_6549.png). This was blocked
    until agent_rate_limits could hold more than one window per agent at
    once — see the rate_limits table migration in init_db() and
    _rate_limit_primary() — since previously a seven_day reading and a
    five_hour reading fought over the same slot and only one could ever
    be shown.

    Each bar's fill is utilization, never rate_limit_window_progress()
    (wall-clock time elapsed in the window) — those are different numbers
    that both look like "the percentage" at a glance, and conflating them
    was a real incident (2026-08-29: window ~65% through its wall-clock
    life while utilization, the number that actually trips the pause
    threshold, was already 98-99%). rate_limit_window_progress() is
    unchanged and still backs the 80%-of-window warning backstop in
    is_rate_limit_warning() and the raw `percent_of_window_used` field on
    /usage — it just doesn't drive what these bars fill to."""
    windows = agent_rate_limits.get(agent)
    if not windows:
        return (
            "No rate-limit reading yet — the CLI reports headroom in-band, "
            "so this fills in the first time the agent takes a turn."
        )

    now = time.time() if now is None else now
    lines = [f"[SYS] Account usage — {agent}"]

    # Fixed display order (five_hour, then seven_day) rather than dict/
    # insertion order, so the report reads the same every time regardless
    # of which event happened to arrive first — anything else Anthropic
    # ever adds just gets tacked on the end instead of dropped.
    ordered_types = [t for t in ("five_hour", "seven_day") if t in windows]
    ordered_types += [t for t in windows if t not in ordered_types]

    any_overage = False
    for rate_limit_type in ordered_types:
        info = windows[rate_limit_type]
        utilization = info.get("utilization")
        label = _RATE_LIMIT_TYPE_LABELS.get(rate_limit_type, rate_limit_type)

        resets_at = info.get("resetsAt")
        if resets_at:
            reset_str = f"resets {format_reset_time(resets_at)}"
        else:
            reset_str = "reset time unknown"

        status = info.get("status")
        flag = ""
        if status == RATE_LIMIT_REJECTED_STATUS:
            flag = " [REJECTED]"
        elif status == RATE_LIMIT_PAUSE_STATUS:
            flag = " [WARNING]"

        if utilization is not None:
            bar, pct = _usage_bar(utilization), f"{utilization * 100:.0f}%"
        else:
            # utilization has never once come through on this account's
            # rate_limit_event (task-1788468492) — confirmed against the DB
            # and agent-server.log, not a one-off gap. Rather than show a
            # bare "?" forever, fall back to window time-progress — but
            # visibly labeled as a different, weaker signal, not silently
            # substituted for it. Conflating the two (unlabeled) is exactly
            # what caused the 2026-08-29 incident (bar read ~65% "used" off
            # window progress while true utilization was already 98%); this
            # keeps that incident's fix (utilization drives the pause logic,
            # never window progress) and only changes what's displayed when
            # there is no utilization reading to show at all.
            progress = rate_limit_window_progress(info, now)
            if progress is not None:
                bar = _usage_bar(progress)
                pct = f"~{progress * 100:.0f}% window elapsed (no usage reading)"
            else:
                bar, pct = _usage_bar(None), "? %"

        lines.append(f"{bar} {pct}  {label} — {reset_str}{flag}")
        if info.get("isUsingOverage"):
            any_overage = True

    lines.append(f"Extra usage: {'active (spending past plan limits)' if any_overage else 'none'}")
    return "\n".join(lines)


def is_rate_limit_warning(agent: str) -> bool:
    """True once this agent is in the warning zone — Anthropic's own
    'allowed_warning' status (whatever window it's for; unlike the hard
    stop below, the warning doesn't care about rateLimitType, since it's
    not blocking anything), OR utilization has crossed
    RATE_LIMIT_WARNING_UTILIZATION_THRESHOLD (0.90) on its own, OR the
    window is RATE_LIMIT_WINDOW_PROGRESS_WARNING_FRACTION (80%) through
    its wall-clock life (2026-08-09 — a backstop for the case where
    utilization is never present at all, confirmed live on Amos's
    instance). Logged and notified (see _notify_rate_limit_warning) but
    never holds the queue — see is_rate_limit_paused() for the actual
    hard stop.

    2026-09-03: reads _rate_limit_primary(agent) rather than indexing
    agent_rate_limits directly, now that an agent can have more than one
    window tracked at once (task-1788454188) — see that function's
    docstring for the selection rule."""
    info = _rate_limit_primary(agent) or {}
    if info.get("status") in (RATE_LIMIT_PAUSE_STATUS, RATE_LIMIT_REJECTED_STATUS):
        return True
    if (info.get("utilization") or 0) >= RATE_LIMIT_WARNING_UTILIZATION_THRESHOLD:
        return True
    progress = rate_limit_window_progress(info)
    return progress is not None and progress >= RATE_LIMIT_WINDOW_PROGRESS_WARNING_FRACTION

def is_rate_limit_paused(agent: str) -> bool:
    """True if status=="rejected" (Anthropic already denied a request —
    stronger than a warning, hard-pause immediately, don't wait on
    utilization) OR utilization has crossed
    RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD (0.95, lowered from 0.97
    2026-08-29). Deliberately NOT keyed
    off 'allowed_warning' alone (see is_rate_limit_warning() for that
    signal) — Anthropic sets that status around 90% utilization, which
    jammed a real session with plenty of window left (2026-08-08, Ian:
    'we shouldn't stop work just at 90%'). Empty/missing info (e.g. right
    after a startup with nothing yet loaded from the DB and no turn
    completed) is NOT paused by default.

    2026-08-10: checked before either of the above — an active, owner-set
    override (see is_rate_limit_override_active / set_rate_limit_override)
    makes this return False regardless of status or utilization. This does
    NOT change what Anthropic will actually accept; a genuinely rejected
    request still gets rejected at the API layer either way. All this
    bypasses is our own app-level queue hold, for the rare case Ian wants
    a fix pushed through immediately instead of waiting for the window.

    2026-09-03: reads _rate_limit_primary(agent) rather than indexing
    agent_rate_limits directly, now that an agent can have more than one
    window tracked at once (task-1788454188) — see that function's
    docstring for the selection rule."""
    if is_rate_limit_override_active(agent):
        return False
    info = _rate_limit_primary(agent) or {}
    if info.get("status") == RATE_LIMIT_REJECTED_STATUS:
        return True
    return (info.get("utilization") or 0) >= RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD

_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

def format_reset_time(epoch: float) -> str:
    """Render a rate-limit reset timestamp as UTC with LA local time
    alongside it, e.g. '00:00 UTC / 17:00 PT (Aug 31)' — added 2026-09-01
    per Ian: the bare UTC-only stamp in the pause/queued-ack notices
    forces a manual conversion every time. %Z gives PST/PDT correctly
    across the DST boundary rather than hardcoding PT. Only date-suffixes
    the local side when it falls on a different calendar day than UTC,
    since that's the actual ambiguity a same-day stamp doesn't have."""
    utc_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    local_dt = utc_dt.astimezone(_PACIFIC_TZ)
    local_str = local_dt.strftime("%H:%M %Z")
    if local_dt.date() != utc_dt.date():
        local_str += local_dt.strftime(" (%b %d)")
    return f"{utc_dt.strftime('%H:%M')} UTC / {local_str}"

async def _notify_rate_limit_pause(agent: str, paused: bool) -> None:
    """Best-effort #signals notice on entering/leaving a rate-limit
    pause. Fire-and-forget from process_agent_queue (never awaited
    while holding agent_locks[agent]) and must never raise — same
    pattern as the startup notice / critical-context notice."""
    try:
        signals_channel = (channels_config.get("channels", {}).get("signals", {}) or {}).get("id")
        if not signals_channel:
            return
        if paused:
            # 2026-09-03: primary window (task-1788454188) — see
            # _rate_limit_primary()'s docstring for the selection rule,
            # now that more than one window can be tracked at once.
            info = _rate_limit_primary(agent) or {}
            resets = info.get("resetsAt")
            resets_str = (
                format_reset_time(resets)
                if isinstance(resets, (int, float)) else "an unknown time"
            )
            if info.get("status") == RATE_LIMIT_REJECTED_STATUS:
                reason = "a request was already rejected by Anthropic"
            else:
                utilization = info.get("utilization") or 0
                reason = f"utilization hit {utilization:.0%} (threshold {RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD:.0%})"
            msg = (
                f"-# ⏸️ {agent} paused — rate limit {reason}, "
                f"holding new messages to avoid overage spend. "
                f"Resumes automatically around {resets_str}, or sooner if the status clears."
            )
        else:
            msg = f"-# ▶️ {agent} resumed — rate limit back to normal, processing queued messages again."
        await post_to_discord(agent, signals_channel, msg)
    except Exception as e:
        log.warning(f"Rate-limit pause/resume notice failed (non-fatal): {e}")

async def _notify_rate_limit_warning(agent: str, warning: bool) -> None:
    """Best-effort #signals notice on entering/leaving the (non-blocking)
    rate-limit warning zone — added 2026-08-08 alongside splitting warning
    from hard-pause (see is_rate_limit_warning() / is_rate_limit_paused()).
    Mirrors _notify_rate_limit_pause's fire-and-forget pattern but never
    implies the queue is held, since this zone doesn't hold it."""
    try:
        signals_channel = (channels_config.get("channels", {}).get("signals", {}) or {}).get("id")
        if not signals_channel:
            return
        if warning:
            # 2026-09-03: primary window (task-1788454188) — see
            # _rate_limit_primary()'s docstring for the selection rule,
            # now that more than one window can be tracked at once.
            info = _rate_limit_primary(agent) or {}
            utilization = info.get("utilization") or 0
            status = info.get("status")
            if utilization:
                reason = f"{utilization:.0%} utilization"
            elif status in (RATE_LIMIT_PAUSE_STATUS, RATE_LIMIT_REJECTED_STATUS):
                reason = f"status={status}"
            else:
                # Neither utilization nor status crossed — must be the
                # window-progress backstop (2026-08-09, see
                # rate_limit_window_progress) firing on its own, the case
                # Amos's instance hits with no utilization field at all.
                progress = rate_limit_window_progress(info)
                reason = (
                    f"{progress:.0%} through the window" if progress is not None
                    else f"status={status}"
                )
            msg = (
                f"-# ⚠️ {agent} rate limit warning — {reason}, "
                f"still processing messages normally. Hard pause at "
                f"{RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD:.0%} utilization."
            )
        else:
            msg = f"-# {agent} rate limit warning cleared — back under {RATE_LIMIT_WARNING_UTILIZATION_THRESHOLD:.0%}."
        await post_to_discord(agent, signals_channel, msg)
    except Exception as e:
        log.warning(f"Rate-limit warning notice failed (non-fatal): {e}")

async def _notify_spend_limit(agent: str, blocked: bool) -> None:
    """#signals alert when the Claude CLI subprocess reports the
    Anthropic account's monthly spend cap hit or cleared (see
    CLI_SPEND_LIMIT_SIGNATURE). Unlike the five-hour rate-limit window's
    _notify_rate_limit_pause, this doesn't self-resolve on a timer — pings
    the owner directly (same convention as relay.py's /sys restart-server:
    a #signals post needing action, not just an FYI, gets a direct ping)
    since clearing it requires Ian to actually raise the limit. Fires once
    per block episode via agent_spend_limit_notified, same dedup pattern
    as the rate-limit notifiers above."""
    try:
        signals_channel = (channels_config.get("channels", {}).get("signals", {}) or {}).get("id")
        if not signals_channel:
            return
        if blocked:
            msg = (
                f"-# 🚫 {agent} hit the Anthropic account's monthly spend limit — "
                f"blocked, not just paused. Raise it at "
                f"claude.ai/settings/usage to clear (won't resolve on its own). "
                f"Heartbeats will keep showing up here but real replies are held "
                f"until then. <@{OWNER_DISCORD_ID}>"
            )
        else:
            msg = f"-# {agent} spend-limit block cleared — processing normally again."
        await post_to_discord(agent, signals_channel, msg)
    except Exception as e:
        log.warning(f"Spend-limit notice failed (non-fatal): {e}")

async def compact_session(agent: str, reason: str) -> bool:
    """Shared compaction action — finalize (summarize-session.py writes a
    summary file) then a full session reset (restart_agent — new
    session_id, no --resume) so the next turn starts fresh and
    load_last_session() picks the summary back up as injected context.
    Split out 2026-08-07 so both compaction triggers (the token-target
    check in maybe_compact_session() and the gap-and-channel topic-change
    check in maybe_topic_change_compact()) share one implementation
    instead of two copies drifting apart. `reason` is just for the log
    line. Returns True on success so callers can tell whether compaction
    actually happened (and so a later trigger in the same turn knows not
    to re-fire)."""
    try:
        # timeout raised 60 -> 75s, 2026-08-07, paired with the summarizer's
        # own inner timeout going 20 -> 45s (see summarize-session.py) —
        # real failure observed same day at the 20s mark
        # ("Failed to generate summary: timeout") on a session sized right
        # at the (then-500k) trigger; 20s was already tight for
        # summarizing real sessions and only gets tighter as sessions
        # grow, so the fix is headroom on both timeouts, not just a
        # retry. This outer one just needs to clear the inner one plus
        # process-spawn overhead.
        result = subprocess.run(
            ["python3", str(Path(__file__).parent / "summarize-session.py"), agent],
            capture_output=True, text=True, timeout=75, cwd=str(WORKSPACE_ROOT)
        )
        if result.returncode != 0:
            log.error(f"{agent} finalize failed, skipping compaction this turn ({reason}): {result.stderr}")
            return False
    except Exception as e:
        log.error(f"{agent} finalize raised, skipping compaction this turn ({reason}): {e}")
        return False

    await db.execute(
        "UPDATE sessions SET compaction_count = compaction_count + 1 WHERE agent = ?",
        (agent,)
    )
    await db.commit()
    await restart_agent(agent)
    log.info(f"{agent} compacted and restarted with a fresh session ({reason})")
    return True

async def maybe_compact_session(agent: str, metadata: Dict[str, Any]) -> bool:
    """Automatic context compaction — Amos's model (Mike's Karakos
    instance), adopted 2026-08-06 in place of the dollar-based daily cap
    that used to hard-reject messages. Runs after a turn fully completes
    and its response has already been posted, never mid-turn. Token-target
    trigger — see maybe_topic_change_compact() below for the second,
    gap-and-channel-based trigger added 2026-08-07."""
    estimate = estimate_context_tokens(metadata)
    if estimate < COMPACTION_TARGET_TOKENS:
        return False

    log.warning(
        f"{agent} context estimate {estimate:,} tokens crossed the "
        f"{COMPACTION_TARGET_TOKENS:,}-token soft target — compacting"
    )
    return await compact_session(agent, reason="token target")

async def maybe_rate_limit_compact(agent: str, already_compacted: bool) -> bool:
    """Third compaction trigger, added 2026-08-08 per Ian — proactive
    wind-down/summarize once utilization crosses
    RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD, the same mark
    is_rate_limit_paused() uses to hold the queue. Point is to summarize
    ahead of the pause so a session doesn't get frozen mid-thought once
    the queue actually stops draining. Skipped if an earlier trigger this
    turn (maybe_compact_session) already compacted — no point paying for
    a second finalize+restart back to back."""
    if already_compacted:
        return False
    # 2026-09-03: primary window (task-1788454188) — see
    # _rate_limit_primary()'s docstring for the selection rule, now that
    # more than one window can be tracked at once.
    utilization = (_rate_limit_primary(agent) or {}).get("utilization") or 0
    if utilization < RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD:
        return False

    log.warning(
        f"{agent} rate-limit utilization {utilization:.0%} crossed the "
        f"{RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD:.0%} mark — compacting ahead of the pause"
    )
    return await compact_session(agent, reason="rate-limit utilization")

async def classify_topic_change(previous_text: str, new_text: str) -> Optional[bool]:
    """Cheap same-topic/different-topic classifier for
    maybe_topic_change_compact() below. Haiku, single turn, small prompt
    — deliberately not the full summarizer; this only ever needs a
    yes/no. Returns True if the topic changed, False if it's a
    continuation, None if the call itself failed or gave an unparseable
    answer (caller treats None like False — skip rather than guess)."""
    prompt = (
        "Two Discord messages from the same ongoing agent conversation, "
        "separated by a gap of at least 30 minutes. Based on their "
        "content, is the SECOND message continuing the same topic or "
        "task as the FIRST, or does it start something new or unrelated?"
        "\n\nFIRST (before the gap):\n" + previous_text[:1500] +
        "\n\nSECOND (after the gap):\n" + new_text[:1500] +
        "\n\nAnswer with exactly one word: SAME or CHANGED."
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--model", "haiku",
            "--max-turns", "1",
            "--output-format", "stream-json",
            "--verbose",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except Exception as e:
        log.warning(f"Topic-change classifier call failed: {e}")
        return None

    answer = ""
    for line in stdout.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            answer = (event.get("result", "") or "").strip().upper()
            break

    if "CHANGED" in answer:
        return True
    if "SAME" in answer:
        return False
    log.warning(f"Topic-change classifier gave an unparseable answer: {answer!r}")
    return None

async def maybe_topic_change_compact(agent: str, channel_id: str, new_text: str, metadata: Dict[str, Any], already_compacted: bool = False) -> None:
    """Second, independent compaction trigger, added 2026-08-07 per Ian:
    a continuous multi-channel dialogue changes topics often enough that
    a lot of context is 'useless' well before the token-target trigger
    fires, but checking on every turn would be both wasteful (a
    classifier call for zero signal on back-to-back messages seconds
    apart) and wrong (two messages seconds apart are essentially never a
    real topic change) — so this is gated on a real gap
    (TOPIC_CHECK_GAP_SEC, ~30min) instead of firing per turn.

    Scoped per (agent, channel_id), not per agent, on purpose — this
    agent runs one shared Claude session across every Discord channel it
    watches, so 'gap' and 'topic' both need to mean something
    channel-local: a busy #general shouldn't suppress the check for
    #signals coming back after real silence, and the classifier should
    never be asked to compare content from two different channels
    against each other (a channel switch is already a different
    conversation by construction — no call needed to know that; what's
    genuinely ambiguous is the *same* channel resuming after a gap,
    which is the only case this actually spends a classifier call on).

    Bookkeeping (agent_channel_last_turn) updates unconditionally, every
    turn, regardless of whether the token-target trigger already compacted
    this turn — otherwise a token-triggered compaction leaves this
    channel's baseline stale (comparing future topic checks against
    pre-compaction text with an inflated gap). The gating/classification
    below is skipped via already_compacted when that trigger did fire —
    no point spending a classifier call to decide whether to do something
    that already happened."""
    key = (agent, channel_id)
    now = time.time()
    prior = agent_channel_last_turn.get(key)
    agent_channel_last_turn[key] = {"at": now, "text": new_text}

    if already_compacted:
        return  # bookkeeping is refreshed above; nothing left to decide this turn
    if not prior:
        return  # first turn seen in this channel — nothing to compare against yet
    gap_sec = now - prior["at"]
    if gap_sec < TOPIC_CHECK_GAP_SEC:
        return  # too recent — this is the "not every turn" gate
    if estimate_context_tokens(metadata) < TOPIC_CHECK_MIN_TOKENS:
        return  # session too small for compaction to be worth it yet

    changed = await classify_topic_change(prior["text"], new_text)
    if changed is not True:
        return  # False (same topic) or None (classifier failed/unparseable) — leave it

    log.warning(
        f"{agent}/{channel_id} topic change detected after a "
        f"{gap_sec / 60:.0f}-minute gap in this channel — compacting"
    )
    await compact_session(agent, reason="topic change")

# =============================================================================
# Attachments (2026-08-09, ported from mcarmody/karakos-package#127)
# =============================================================================

def _human_size(size) -> str:
    """Bytes as something an agent can reason about at a glance."""
    if not isinstance(size, (int, float)) or size < 0:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _batch_span_seconds(messages: List[Dict]) -> Optional[float]:
    """Elapsed time between a batch's oldest and newest message, or None
    if timestamps are missing/unparseable — fails closed to "don't flag
    a gap" rather than guessing. message_queue.created_at is SQLite's
    CURRENT_TIMESTAMP, 'YYYY-MM-DD HH:MM:SS' in UTC, no explicit offset."""
    timestamps = []
    for msg in messages:
        raw = msg["created_at"]
        try:
            timestamps.append(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
        except (TypeError, ValueError):
            continue
    if len(timestamps) < 2:
        return None
    return (max(timestamps) - min(timestamps)).total_seconds()


def _format_span(seconds: float) -> str:
    """Human-readable elapsed time for the backlog callout — '2h39m',
    '45m', or '90s', whichever is coarsest without losing the point."""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def format_attachments(raw) -> str:
    """Render a queued message's attachments as lines for the agent envelope.

    Returns "" when there are none, so callers can append unconditionally.

    Every attachment gets a line whether or not the relay managed to save
    it. The failure line is the point of the feature as much as the
    success line: before this, a message carrying a file reached the
    agent as bare text and the user got an answer that never acknowledged
    the file existed.
    """
    if not raw:
        return ""

    if isinstance(raw, str):
        try:
            attachments = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("Unparseable attachments column: %r", raw[:200])
            return ""
    else:
        attachments = raw

    if not isinstance(attachments, list) or not attachments:
        return ""

    lines = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        name = item.get("filename") or "unnamed"
        path = item.get("path")
        if path:
            descriptor = ", ".join(
                p for p in (item.get("content_type"), _human_size(item.get("size"))) if p
            )
            lines.append(f"  - {name} ({descriptor}) saved at: {path}")
        else:
            reason = item.get("skipped") or "not available"
            lines.append(f"  - {name} — NOT saved: {reason}")

    if not lines:
        return ""

    header = (
        f"  [{len(lines)} attachment(s) on this message. "
        "Open a saved one with the Read tool at the path given.]"
    )
    return "\n".join([header, *lines])

# =============================================================================
# Session Persistence (Summary and Restore)
# =============================================================================

async def load_last_session(agent: str) -> Dict[str, Any]:
    """Load last session summary if available and recent"""
    summary_path = Path(str(LAST_SUMMARY_TEMPLATE).format(agent=agent))

    if not summary_path.exists():
        return {"status": "not_found"}

    # Check age
    mtime = summary_path.stat().st_mtime
    age_hours = (time.time() - mtime) / 3600

    if age_hours > 24:
        return {"status": "stale", "age_hours": age_hours}

    with open(summary_path) as f:
        summary = f.read()

    return {"status": "success", "summary": summary, "age_hours": age_hours}

# =============================================================================
# Agent Subprocess Management
# =============================================================================

def load_persona_files(agent: str) -> str:
    """Load and concatenate persona files for agent"""
    persona_dir = WORKSPACE_ROOT / "agents" / agent / "persona"
    if not persona_dir.exists():
        return ""

    persona_parts = []
    for file in sorted(persona_dir.glob("*.md")):
        with open(file) as f:
            content = f.read().strip()
            if content:
                persona_parts.append(content)

    return "\n\n".join(persona_parts)


def load_memory_index(agent: str) -> str:
    """Load the routing-table-only memory index (MEMORY.md), never the fact
    bodies it points at. Schema and this constraint ported from Amos (Mike's
    Karakos instance) 2026-08-05: the index earns its place by staying a
    list of links and one-line hooks. If it starts holding paragraphs of
    actual fact content, it has become the exact bloated-context problem
    the two-layer design (index + individual fact files under
    agents/{agent}/memory/facts/) exists to avoid. This function loads only
    agents/{agent}/memory/MEMORY.md; fact files are read on demand, not
    here."""
    memory_index = WORKSPACE_ROOT / "agents" / agent / "memory" / "MEMORY.md"
    if not memory_index.exists():
        return ""
    try:
        return memory_index.read_text().strip()
    except Exception as e:
        log.error(f"Failed to read memory index for {agent}: {e}")
        return ""


def load_onboarding_prompt(agent: str) -> str:
    """Return the onboarding prompt iff persona is empty.

    Gated on persona content (not session-resume state) so wiping the DB
    doesn't retrigger onboarding once the user has given the agent its
    identity. Substitutes a small set of placeholders so the file can be
    shared across agent renames.
    """
    persona_dir = WORKSPACE_ROOT / "agents" / agent / "persona"
    if persona_dir.exists() and any(
        f.read_text().strip() for f in persona_dir.glob("*.md") if f.is_file()
    ):
        return ""

    onboarding_path = WORKSPACE_ROOT / "agents" / agent / "onboarding.md"
    if not onboarding_path.exists():
        return ""

    text = onboarding_path.read_text()
    substitutions = {
        "{{AGENT_NAME}}": agent,
        "{{OWNER_NAME}}": os.environ.get("OWNER_NAME", "User"),
        "{{SYSTEM_NAME}}": os.environ.get("SYSTEM_NAME", "karakos"),
    }
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    return text.strip()


async def start_agent_subprocess(agent: str):
    """Start persistent Claude subprocess for agent"""
    config = agent_config.get(agent, {})
    if not config:
        log.error(f"No config found for agent: {agent}")
        return

    session_id = await get_or_create_session(agent)
    system_prompt_path = WORKSPACE_ROOT / config.get("system_prompt", "")

    if not system_prompt_path.exists():
        log.error(f"System prompt not found for {agent}: {system_prompt_path}")
        return

    # The CLI's --system-prompt flag takes the prompt string, not a file
    # path. Read the file contents here.
    try:
        system_prompt_text = system_prompt_path.read_text()
    except Exception as e:
        log.error(f"Failed to read system prompt for {agent}: {e}")
        return

    # Load persona
    persona_content = load_persona_files(agent)

    # Load memory index (routing table only — see load_memory_index)
    memory_index = load_memory_index(agent)
    if memory_index:
        persona_content = (
            memory_index + ("\n\n" + persona_content if persona_content else "")
        )

    # First-boot gate: if no persona has been written yet, prepend the
    # onboarding prompt so the agent asks the user for guidance instead
    # of arriving fully-formed.
    onboarding = load_onboarding_prompt(agent)
    if onboarding:
        log.info(f"Injecting onboarding prompt for {agent} (persona is empty)")
        persona_content = onboarding + ("\n\n" + persona_content if persona_content else "")

    # Load last session summary if available
    last_session = await load_last_session(agent)
    if last_session["status"] == "success":
        log.info(f"Injecting session summary for {agent} (age: {last_session['age_hours']:.1f}h)")
        persona_content = f"[SESSION RESET]\n\n{last_session['summary']}\n\n{persona_content}"

    # --session-id only works for a brand-new session; once the CLI has
    # written a transcript for this ID (i.e. on every restart after the
    # first), it must be resumed instead or the CLI exits with
    # "Session ID ... is already in use".
    resuming = session_transcript_exists(session_id)

    # Build command
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--model", config.get("model", "sonnet"),
        "--max-turns", str(config.get("max_turns", 200)),
        "--verbose",
        "--dangerously-skip-permissions",
        "--resume" if resuming else "--session-id", session_id,
        "--system-prompt", system_prompt_text,
    ]

    if persona_content:
        cmd.extend(["--append-system-prompt", persona_content])

    # Add disallowed tools
    disallowed = config.get("disallowed_tools", [])
    for pattern in disallowed:
        cmd.extend(["--disallowedTools", pattern])

    # Add allowed tools if specified
    allowed = config.get("allowed_tools")
    if allowed:
        cmd.extend(["--allowedTools", ",".join(allowed)])

    log.info(
        f"Starting {agent} subprocess (model={config.get('model')}, "
        f"session={session_id[:8]}, {'resuming' if resuming else 'new session'})"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Found live 2026-08-07: asyncio's default StreamReader limit is
            # 64KiB per line, and stream-json emits one JSON object per
            # line — a single large tool result or Skill-file dump easily
            # exceeds that, and read_agent_response()'s proc.stdout.readline()
            # (line ~1058) then raises LimitOverrunError ("Separator is not
            # found, and chunk exceed the limit"), silently caught by the
            # broad `except Exception` around the whole read loop. Net
            # effect: that turn's response reading aborts wherever it was,
            # the turn still gets marked processed, and whatever hadn't
            # posted yet (interim streaming aside) is lost. Hit repeatedly
            # this session, including right after a large Skill invocation.
            # 16 MiB is generous headroom — cost is just a larger allowed
            # buffer, not a preallocation.
            limit=16 * 1024 * 1024,
        )
        agent_processes[agent] = proc
        agent_states[agent] = "IDLE"
        agent_sessions[agent] = session_id

        # Start stderr reader
        _spawn(stderr_reader(agent, proc))

        log.info(f"{agent} subprocess started (PID {proc.pid})")
    except Exception as e:
        log.error(f"Failed to start {agent}: {e}")
        agent_states[agent] = "ERROR_RECOVERY"

async def stderr_reader(agent: str, proc: asyncio.subprocess.Process):
    """Read and log stderr from agent subprocess"""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode().strip()
            if msg:
                log.warning(f"{agent} stderr: {msg}")
    except Exception as e:
        log.error(f"stderr reader error for {agent}: {e}")

async def kill_agent_subprocess(agent: str):
    """Terminate agent subprocess"""
    proc = agent_processes.get(agent)
    if not proc:
        return

    log.info(f"Killing {agent} subprocess (PID {proc.pid})")
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.warning(f"{agent} didn't terminate, sending SIGKILL")
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        log.warning(f"{agent} subprocess (PID {proc.pid}) was already dead")

    agent_processes.pop(agent, None)
    log.info(f"{agent} subprocess terminated")

async def restart_agent(agent: str):
    """Restart agent subprocess.

    Gated on agent_locks[agent] (2026-08-11) — process_agent_queue() holds
    that lock for the full duration of an in-flight turn, including the
    blocking proc.stdout.readline() loop in read_agent_response(). Killing
    the subprocess out from under that readline() closes stdout, which
    returns EOF immediately: the turn's response comes back empty, gets
    marked STATUS_COMPLETE anyway, and nothing posts to Discord — silent,
    no exception, no log line calling it an error. Real incident: a manual
    /compact landed in the same instant as a real message, ate it, and the
    sender (reasonably) thought Marvin just didn't respond. The automatic
    compaction triggers (maybe_compact_session etc.) were never affected —
    they already only fire after process_agent_queue's lock is released —
    but the manual /compact and /restart /reload HTTP endpoints call this
    directly, with no relationship to whatever turn might be mid-flight.
    Acquiring the lock here just makes restart wait for the current turn
    to actually finish first, same as it always implicitly did for the
    automatic paths.
    """
    log.info(f"Restarting {agent}")
    lock = agent_locks.get(agent)
    async with (lock if lock else contextlib.nullcontext()):
        await kill_agent_subprocess(agent)
        await clear_session(agent)
        agent_last_cost.pop(agent, None)
        response_buffers[agent] = ""
        await start_agent_subprocess(agent)


async def interrupt_agent(agent: str) -> Dict[str, Any]:
    """Send a stream-json `control_request`/"interrupt" to the agent's live
    subprocess over stdin (2026-08-30, Ian's ask: a Discord-native "HALT"
    command mirroring the CLI's own interrupt). Verified live against the
    real Claude CLI, not from docs alone: it acks with a `control_response`
    (subtype "success", echoing request_id, `response.still_queued` listing
    any queued messages it dropped), actually stops the in-flight turn,
    injects a synthetic user turn ("[Request interrupted by user]"), and
    closes with a `result` event (`is_error: true`, `stop_reason: null`,
    subtype `error_during_execution`) — all without killing the process or
    losing the session. read_agent_response()'s main loop already ignores
    unrecognized event types and handles an `is_error` result without
    special-casing it, so no changes were needed there.

    Deliberately does NOT take agent_locks[agent], unlike restart_agent()/
    reload_agent() above. Those two are fine waiting for a lock, because
    they're bouncing the subprocess between turns. This is the opposite
    case — the whole point is to interrupt a turn that is, right now,
    holding that lock — so waiting for it first would defeat the command
    entirely, the same "must work even if the agent is wedged" requirement
    the /sys status/clear/reload commands are already built to satisfy.
    Writing unlocked is safe because proc.stdin.write() is a plain
    synchronous call, not a coroutine — on a single-threaded event loop it
    can't interleave mid-call with whatever the turn's own send_to_agent()
    is writing, only ever land fully before or fully after it.
    """
    proc = agent_processes.get(agent)
    if not proc or not proc.stdin:
        return {"ok": False, "error": "no subprocess"}
    request_id = str(uuid.uuid4())
    msg = json.dumps({
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "interrupt"},
    }) + "\n"
    try:
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        log.info(f"Sent interrupt control_request to {agent} (request_id={request_id})")
        return {"ok": True, "request_id": request_id}
    except Exception as e:
        log.error(f"Error sending interrupt to {agent}: {e}")
        return {"ok": False, "error": str(e)}


async def reload_agent(agent: str):
    """Bounce the subprocess but keep the session — used to pick up new
    SYSTEM_PROMPT / persona / MCP config without dropping conversation
    context. The respawn calls --resume on the existing session_id.

    Same lock-gating as restart_agent() above and for the same reason —
    this hits kill_agent_subprocess() too, so it's exposed to the identical
    mid-turn race.
    """
    log.info(f"Reloading {agent} (preserving session)")
    lock = agent_locks.get(agent)
    async with (lock if lock else contextlib.nullcontext()):
        await kill_agent_subprocess(agent)
        agent_last_cost.pop(agent, None)
        response_buffers[agent] = ""
        await start_agent_subprocess(agent)

# =============================================================================
# Cost Tracking
# =============================================================================

async def post_cost_update(agent: str, metadata: Dict):
    """Post cost update to Discord and database"""
    session_total = metadata.get("total_cost_usd", 0.0)
    input_tokens = metadata.get("input_tokens", 0)
    output_tokens = metadata.get("output_tokens", 0)
    duration_ms = metadata.get("duration_ms", 0)

    # Calculate delta
    last_cost = agent_last_cost.get(agent, 0.0)
    cost_delta = session_total - last_cost
    agent_last_cost[agent] = session_total

    # Store in database
    await db.execute(
        """
        INSERT INTO cost_events (agent, cost_delta, session_total, input_tokens, output_tokens, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (agent, cost_delta, session_total, input_tokens, output_tokens, duration_ms)
    )
    await db.commit()

    # Post to Discord cost channel (if configured)
    cost_channel_id = channels_config.get("channels", {}).get("cost", {}).get("id")
    if cost_channel_id and cost_delta > 0.001:
        duration_s = duration_ms / 1000.0
        message = f"`{agent}` +${cost_delta:.2f} (session: ${session_total:.2f}) • {input_tokens:,}in/{output_tokens:,}out • {duration_s:.1f}s"
        await post_to_discord(agent, cost_channel_id, message)

# check_cost_limits() removed 2026-08-06 — it had exactly one caller
# (the handle_message rejection below, also removed the same day) and is
# fully dead now that rejection is gone. COST_DAILY_LIMIT /
# COST_MONTHLY_LIMIT constants stay (still read from .env, still shown by
# `cost-report.sh` / handle_cost_get, which does its own independent SUM
# query rather than calling this), just nothing enforces them anymore.

# =============================================================================
# Discord Integration
# =============================================================================

MAX_DISCORD_MSG_LEN = 2000

def split_discord_message(text: str, max_length: int = MAX_DISCORD_MSG_LEN) -> List[str]:
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

async def post_to_discord(agent: str, channel_id: str, content: str, reply_to: Optional[str] = None) -> Optional[str]:
    """Post message to Discord as agent, splitting if over 2000 chars"""
    global http_session

    # Skip posting if channel_id is "0" (silent mode)
    if channel_id == "0":
        return None

    # Silence discipline, enforced structurally (task-1788365086): a turn
    # that decides not to speak must emit zero bytes, not a visible blank
    # message. Four prior fixes lived only in persona/voice.md and all four
    # relapsed, because the actual gap was never in the model's judgment —
    # it was that nothing downstream of that judgment ever verified the
    # result before calling the Discord API. split_discord_message() already
    # drops a true empty string (`[text] if text else []` -> `[]`), so a
    # literal "" never reaches this point; what got through and posted as a
    # visible blank message twice in #general (2026-09-04 00:03) was
    # whitespace-only content, which is truthy and survives that check.
    # Stripped here, at the one function every posting path funnels through,
    # so it can't be bypassed by finding yet another caller that skips it.
    if not content or not content.strip():
        log.info(
            f"post_to_discord: suppressing whitespace/empty content for "
            f"{agent} in {channel_id} (silence discipline, task-1788365086)"
        )
        return None

    # Get agent's Discord token, fallback to primary agent
    token = AGENT_TOKENS.get(agent)
    if not token:
        # Use first available token as fallback
        if AGENT_TOKENS:
            token = list(AGENT_TOKENS.values())[0]
            content = f"[{agent}] {content}"
        else:
            log.warning(f"No Discord tokens configured, cannot post for {agent}")
            return None

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json"
    }

    chunks = split_discord_message(content)
    last_msg_id = None
    failed = 0

    for idx, chunk in enumerate(chunks):
        payload = {"content": chunk}
        # Only reply-reference the first chunk
        if reply_to and last_msg_id is None:
            payload["message_reference"] = {"message_id": reply_to}

        posted = False
        try:
            async with http_session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    last_msg_id = data.get("id")
                    posted = True
                elif resp.status == 429:
                    retry_after = (await resp.json()).get("retry_after", 1)
                    log.warning(f"Rate limited posting to {channel_id}, retry after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    # Retry this chunk
                    async with http_session.post(url, headers=headers, json=payload) as retry_resp:
                        if retry_resp.status == 200:
                            data = await retry_resp.json()
                            last_msg_id = data.get("id")
                            posted = True
                        else:
                            log.error(
                                f"Discord API error {retry_resp.status} on chunk "
                                f"{idx + 1}/{len(chunks)} ({len(chunk)} chars) after "
                                f"rate-limit retry: {await retry_resp.text()}"
                            )
                else:
                    log.error(
                        f"Discord API error {resp.status} on chunk "
                        f"{idx + 1}/{len(chunks)} ({len(chunk)} chars): "
                        f"{await resp.text()}"
                    )
        except Exception as e:
            log.error(f"Error posting chunk {idx + 1}/{len(chunks)} to Discord: {e}")

        if not posted:
            failed += 1

    # A chunk that never landed is a piece of the reply the user will never
    # see. Returning the id of a sibling chunk reports the whole message as
    # delivered and the loss goes unnoticed — which is how two replies
    # vanished silently before this was caught.
    if failed:
        log.error(
            f"post_to_discord: {failed} of {len(chunks)} chunk(s) failed for "
            f"{agent} in {channel_id}; message is incomplete"
        )
        return None

    return last_msg_id

async def get_latest_channel_message_id(agent: str, channel_id: str) -> Optional[str]:
    """Fetch the most recent message ID in a channel via the Discord REST
    API — read-only, best-effort. Used for mid-turn steering, Phase 1
    (2026-08-30, task-1788046725 follow-on): checks whether *anything*
    landed in the channel while a turn was generating, not just messages
    that made it into this agent's own message_queue (which reply-gating
    can skip entirely — see the Amos-adoption-commit race in #agent-chat
    2026-08-29 that started this thread). Returns None on any failure
    rather than raising; this is instrumentation, never something that
    should affect whether a turn completes or posts.
    """
    global http_session
    if channel_id == "0":
        return None
    token = AGENT_TOKENS.get(agent)
    if not token and AGENT_TOKENS:
        token = list(AGENT_TOKENS.values())[0]
    if not token:
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with http_session.get(url, headers=headers, params={"limit": 1}) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    return data[0].get("id")
            else:
                log.debug(
                    f"[stale-gate] snapshot fetch failed ({resp.status}) "
                    f"for channel {channel_id}"
                )
    except Exception as e:
        log.debug(f"[stale-gate] snapshot fetch error for channel {channel_id}: {e}")
    return None

async def get_recent_channel_messages(
    agent: str, channel_id: str, after_id: Optional[str] = None, limit: int = 10
) -> List[Dict]:
    """Fetch real messages from a channel via the Discord REST API,
    oldest-first — read-only, best-effort, empty list on any failure.

    Entry-side half of mid-turn steering (2026-08-30, Ian's design,
    #agent-chat/#general 2026-08-29/30): when the message a turn is about
    to answer is already old by the time we pick it up (busy elsewhere,
    a pause, a restart), this pulls whatever's landed in the channel
    since — from any author, not just what made it into this agent's own
    message_queue — so the draft starts from current reality instead of
    a stale snapshot. `after_id` is the newest message_id already in this
    turn's own batch; Discord's `after` param excludes it and everything
    older, so the result is exactly the gap. Discord returns newest-first
    regardless of before/after/around; reversed here so callers can just
    iterate in reading order.
    """
    global http_session
    if channel_id == "0":
        return []
    token = AGENT_TOKENS.get(agent)
    if not token and AGENT_TOKENS:
        token = list(AGENT_TOKENS.values())[0]
    if not token:
        return []
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    params: Dict[str, Any] = {"limit": limit}
    if after_id:
        params["after"] = after_id
    try:
        async with http_session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return list(reversed(data))
            log.debug(
                f"[entry-stale-check] recent-messages fetch failed "
                f"({resp.status}) for channel {channel_id}"
            )
    except Exception as e:
        log.debug(f"[entry-stale-check] recent-messages fetch error for channel {channel_id}: {e}")
    return []

async def start_typing(agent: str, channel_id: str):
    """Start typing indicator in Discord channel"""
    if channel_id == "0" or channel_id in typing_tasks:
        return

    async def typing_loop():
        token = AGENT_TOKENS.get(agent)
        if not token and AGENT_TOKENS:
            token = list(AGENT_TOKENS.values())[0]
        if not token:
            return

        url = f"https://discord.com/api/v10/channels/{channel_id}/typing"
        headers = {"Authorization": f"Bot {token}"}

        while True:
            try:
                async with http_session.post(url, headers=headers) as resp:
                    if resp.status != 204:
                        break
                await asyncio.sleep(TYPING_INTERVAL)
            except Exception:
                break

    task = _spawn(typing_loop())
    typing_tasks[channel_id] = task

async def stop_typing(channel_id: str):
    """Stop typing indicator"""
    task = typing_tasks.pop(channel_id, None)
    if task:
        task.cancel()

# =============================================================================
# Message Processing
# =============================================================================

# Mechanical presence layer (2026-08-18) — stolen from Amos per #agent-chat:
# "our relay already writes presence automatically, it flips me idle/busy
# per turn state and tags batch work in the activity text." We didn't have
# that half — presence only ever moved when the agent explicitly called
# set_status (skills/set-status/), the intentional "going dark" layer. This
# is the mechanical half: on every turn we write an activity string saying
# what's in flight (channel, batch size, other channels queued behind it),
# and clear it back to the resting state when the turn ends. Deliberately
# does NOT flip the status dot itself (state always stays "online" here) —
# idle/dnd are already claimed by the intentional four-tier taxonomy
# (SKILL.md), and reusing them mechanically would make "idle" mean two
# different things depending on who set it.
#
# Same file relay.py already polls (STATUS_FILE == data/status/marvin.json
# there) — no new IPC needed, just another writer. Only wired for Marvin:
# that file and relay's poll loop are single-agent today (agents.json has
# no equivalent for "relay"), so this is a no-op for any other agent key.
#
# A manual set_status call always wins over this. Distinguished by a
# "source" field written by both sides: set_status.py stamps "manual",
# this stamps "auto". While a manual entry is an *active declaration*
# (state idle or dnd — "I'm mid checkpoint" / "I'm dark"), mechanical
# writes are skipped entirely rather than clobbering it. Once the agent
# calls set_status(state="online") to clear (SKILL.md's documented "Done"
# step), that's a relinquish signal — state back to online hands control
# back to the mechanical layer, same as if no manual entry existed.
STATUS_FILE = WORKSPACE_ROOT / "data" / "status" / "marvin.json"


def _write_mechanical_status(agent: str, activity: Optional[str]) -> None:
    if agent != "Marvin":
        return

    try:
        if STATUS_FILE.exists():
            with open(STATUS_FILE) as f:
                current = json.load(f)
            if current.get("source") == "manual" and current.get("state") in ("idle", "dnd"):
                return  # active intentional declaration in effect — hands off
    except (json.JSONDecodeError, OSError):
        pass  # unreadable/corrupt — fine to overwrite with a clean auto write

    if activity and len(activity) > 128:
        activity = activity[:128]

    payload = {
        "state": "online",
        "activity": activity or None,
        "source": "auto",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = STATUS_FILE.with_suffix(".json.tmp")
        with open(tmp_file, "w") as f:
            json.dump(payload, f, indent=2)
        tmp_file.replace(STATUS_FILE)
    except OSError:
        log.exception("Failed to write mechanical status to %s", STATUS_FILE)


async def send_to_agent(agent: str, content: str, message_ids: List[str], activity: Optional[str] = None):
    """Send message to agent subprocess"""
    proc = agent_processes.get(agent)
    if not proc or not proc.stdin:
        log.error(f"No subprocess for {agent}")
        return

    agent_states[agent] = "PROCESSING"
    _write_mechanical_status(agent, activity)
    response_buffers[agent] = ""

    # Send message — Claude Code stream-json input envelope.
    # Format: {"type": "user", "message": {"role": "user", "content": <str>}}
    # The bare {"type":"user","content":...} form is rejected by the SDK.
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    }) + "\n"
    try:
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        log.info(f"Sent message to {agent} ({len(message_ids)} queued messages)")
    except Exception as e:
        log.error(f"Error sending to {agent}: {e}")
        agent_states[agent] = "ERROR_RECOVERY"

def format_tool_summary(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Build the text after '-# ⚙️ ' for a tool-streaming line. Returns None
    for tools that should be suppressed entirely (no gear line at all).

    Format ported from Amos (Mike's Karakos instance), shared 2026-08-06:
    per-tool shapes rather than one generic "Tool — description" line,
    em-dash separator, fixed gear icon for everything (per-tool icons
    "never worth the lookup table" in his words).

    Known gaps versus his implementation, deliberately not ported yet:
      - Ordering: his gear line waits for the sibling text pump before
        posting, so it never lands ahead of the assistant text it belongs
        to. Marvin's current loop posts tool lines inline as they're seen,
        which can interleave oddly if a text block and a tool_use block
        share one stream event with the text first. Noted, not fixed —
        would need restructuring read_agent_response's event loop.
      - Merge mode (collapsing consecutive same-tool calls to "Tool ×N")
        exists on his side but is off by default. Not ported.
    """
    if tool_name in ("TaskList", "TaskGet"):
        return None

    def trunc(s: str, n: int) -> str:
        # Collapse ALL whitespace runs (including newlines) to single
        # spaces, not just strip the edges. A multi-line command (heredoc,
        # embedded python3 -<<EOF, etc.) left its internal "\n"s intact
        # here before 2026-08-06 — Discord's "-# " subtext prefix only
        # applies to the first line, so every line after it broke out of
        # the quiet formatting into a plain-size dump, and an inline
        # single-backtick span doesn't render across multiple lines
        # either, so the backticks themselves showed up broken. Ian
        # flagged this directly ("work on the syntax for some of these
        # bash commands") after watching it happen live.
        s = " ".join((s or "").split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if tool_name == "Read":
        path = tool_input.get("file_path", "") or tool_input.get("path", "")
        parts = path.rstrip("/").split("/")
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f"Read — {short}"

    if tool_name == "Write":
        path = tool_input.get("file_path", "") or tool_input.get("path", "")
        content = tool_input.get("content", "")
        return f"Write — {path} ({len(content)} chars)"

    if tool_name == "Edit":
        path = tool_input.get("file_path", "") or tool_input.get("path", "")
        return f"Edit — {path}"

    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        cmd = tool_input.get("command", "")
        # Show the actual command syntax whenever there is one — a
        # description alone ("Check system health") tells Ian what I
        # claim I'm doing, not what actually ran. trunc() now collapses
        # newlines, so this backtick span is always safe as one line.
        cmd_code = f"`{trunc(cmd, 65)}`" if cmd else ""
        if desc and cmd_code:
            return f"Bash — {trunc(desc, 40)}: {cmd_code}"
        if cmd_code:
            return f"Bash — {cmd_code}"
        if desc:
            return f"Bash — {trunc(desc, 80)}"
        return "Bash"

    if tool_name == "Grep":
        pattern = trunc(tool_input.get("pattern", ""), 40)
        path = tool_input.get("path", "")
        return f"Grep — /{pattern}/ in {path}" if path else f"Grep — /{pattern}/"

    if tool_name == "WebFetch":
        return f"WebFetch — {trunc(tool_input.get('url', ''), 60)}"

    if tool_name == "WebSearch":
        return f"WebSearch — {trunc(tool_input.get('query', ''), 60)}"

    if tool_name == "Task":
        agent_type = tool_input.get("subagent_type", "general-purpose")
        desc = tool_input.get("description", "")
        return f"Task — {agent_type}: {trunc(desc, 60)}" if desc else f"Task — {agent_type}"

    # Fallback for anything without a dedicated shape (Glob, NotebookEdit,
    # MCP tools, etc.) — bare tool name, no crash on the unexpected case.
    return tool_name


async def write_streaming_response(message_ids: List[str], text: str) -> None:
    """Push partial response text into message_queue so SSE polling sees it.

    The /api/chat/stream SSE route reads message_queue.response and forwards
    deltas to the dashboard. Without these incremental writes, the dashboard
    only sees text on the post-loop UPDATE — i.e., never until the turn ends.
    """
    if not message_ids or db is None:
        return
    placeholders = ",".join("?" * len(message_ids))
    try:
        await db.execute(
            f"UPDATE message_queue SET response = ? WHERE message_id IN ({placeholders})",
            (text, *message_ids),
        )
        await db.commit()
    except Exception as e:
        log.warning(f"streaming response write failed: {e}")


async def read_agent_response(
    agent: str, channel_id: str, message_ids: Optional[List[str]] = None
) -> tuple[str, Dict]:
    """Read and process agent response stream"""
    proc = agent_processes.get(agent)
    if not proc or not proc.stdout:
        return "", {}

    config = agent_config.get(agent, {})
    tool_streaming = config.get("tool_streaming", False)
    stream_to_channel = config.get("stream_to_channel", False)

    # Per-guild quiet mode (2026-08-11, Ian: keep #agent-chat clean of
    # tool-call/interim "thinking" noise; extended 2026-08-28 to a general
    # rule — full personality everywhere, but tool/gear-line streaming
    # only in the home guild). Originally a hand-maintained channel *name*
    # list (`agents.json`'s `quiet_channels`), which meant a new channel
    # in a foreign guild stayed noisy until someone remembered to add its
    # name. Replaced with an actual guild check: only the primary guild
    # (`channels_config["server_ids"][0]` — Heart of Gold) gets tool
    # lines and interim italics; every other guild a configured channel
    # lives in is quiet by construction, no list maintenance needed. Only
    # silences tool lines and interim italics for this turn — the real
    # final answer (pending_final in process_agent_queue) is never gated
    # by either flag and always posts regardless, so a quiet channel
    # still gets its answer, just without the play-by-play.
    #
    # 2026-08-14: flipped from a pure blocklist to allowlist-then-blocklist.
    # A channel_id not found in our own channels_config (e.g. one that
    # lives in a foreign guild, reached via a direct @mention) used to
    # fail OPEN here — full tool/interim streaming into a channel we don't
    # even recognize as ours. Found comparing notes with Amos, whose
    # equivalent path defaults to posting nowhere unless told otherwise.
    # Paired with relay.py's guild gate, which should stop that traffic
    # even earlier now — this is the second layer, not the only one.
    # Preserved under the guild-check rewrite: an unrecognized channel
    # has no known guild_id either, so it still fails closed below.
    channel_cfg = next(
        (cfg for cfg in channels_config.get("channels", {}).values()
         if cfg.get("id") == channel_id),
        None,
    )
    primary_guild_ids = channels_config.get("server_ids", [])
    primary_guild_id = primary_guild_ids[0] if primary_guild_ids else None
    if channel_cfg is None or channel_cfg.get("guild_id") != primary_guild_id:
        tool_streaming = False
        stream_to_channel = False

    msg_ids = message_ids or []

    final_text = ""
    metadata = {}
    last_posted_chunk = ""
    last_assistant_usage: Dict[str, Any] = {}

    # Chunked/interim Discord streaming (added 2026-08-06, per Ian: "make
    # sure the interstitial thinking phrases get spoken while cogitating
    # instead of dumping them all at the end"). Each text block is held as
    # "pending" until we know what follows it in the stream: if another
    # text block or a tool call comes next, the pending one was
    # interstitial commentary and posts in italics; if the turn ends with
    # it still pending, it's the real final answer and posts plain, no
    # italics. Matches the distinction Mike drew, relayed via Amos
    # 2026-08-06 — subtext ("-# ") stays reserved for tool lines only.
    pending_interim_text = None
    text_streamed_this_turn = False
    last_discord_msg_id = None

    async def flush_pending_text():
        nonlocal pending_interim_text, text_streamed_this_turn, last_discord_msg_id
        if pending_interim_text and stream_to_channel and channel_id != "0":
            # is_silence_announcement() (2026-09-02) was only ever wired in at
            # the pending_final post site below — this interim path posted
            # straight to Discord with no content gate at all. Confirmed live
            # (agent-server.log, 2026-09-02 17:09/21:26, #agent-chat): an
            # interstitial "Not mine to take" / "no message sent" segment gets
            # flushed as italic interim text *before* the model goes on to say
            # more, so it never reaches pending_final and never hit the guard.
            # Guild-based quiet mode (2026-08-28) already stops this class of
            # noise for crab-cavern channels (stream_to_channel is forced off
            # there), but this check is content-based and guild-agnostic on
            # purpose: the same interim segment can just as easily precede a
            # real final answer in a home-guild channel (#general etc.), where
            # streaming is intentionally still on.
            if is_silence_announcement(pending_interim_text):
                log.info(
                    "suppressing silence-announcement interim post (channel=%s): %r",
                    channel_id, pending_interim_text[:200],
                )
            else:
                msg_id = await post_to_discord(agent, channel_id, f"*{pending_interim_text}*")
                if msg_id:
                    last_discord_msg_id = msg_id
            text_streamed_this_turn = True
        pending_interim_text = None

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            try:
                decoded_line = line.decode()
            except UnicodeDecodeError as e:
                # Distinct from the JSONDecodeError case below: this is the
                # raw bytes off the pipe failing to decode at all, not
                # malformed JSON. Would otherwise propagate to the broad
                # `except Exception` around the whole loop and silently
                # abort reading the rest of this turn's stream (whatever
                # had accumulated in final_text so far still gets
                # returned, but everything after this line is lost with no
                # trace). Logged here instead of relying on that outer
                # catch, which never named what actually broke.
                log.warning(
                    f"{agent}: undecodable stdout line ({len(line)} bytes, "
                    f"decode error: {e}) -- skipping"
                )
                continue

            try:
                event = json.loads(decoded_line)
            except json.JSONDecodeError as e:
                # task-1788135135 (2026-08-31): found completely silent --
                # no log, no counter -- while chasing long single-turn
                # replies losing whole interior sections before ever
                # reaching Discord, with no delivery error anywhere
                # (confirmed via #lounge history: content cut off mid-
                # sentence and resumed at a non-adjacent section, not a
                # simple end-truncation). Leading theory: stream-json is
                # one JSON object per line, so if a single large `text`
                # content block's write gets split across an unescaped
                # newline somewhere upstream, this line and the
                # continuation line that follows it both fail to parse --
                # and previously both were dropped with zero trace while
                # the loop kept going, silently erasing exactly one
                # interior chunk from final_text. Not yet proven; this
                # logs enough (length + head/tail snippet, never the full
                # body, to avoid flooding logs on a bad line) to confirm
                # or rule the theory out next time it fires, instead of
                # guessing again.
                snippet_len = 120
                head = decoded_line[:snippet_len]
                tail = decoded_line[-snippet_len:] if len(decoded_line) > snippet_len else ""
                log.warning(
                    f"{agent}: unparseable stream-json line "
                    f"({len(decoded_line)} chars, error: {e}) -- skipping. "
                    f"head={head!r} tail={tail!r}"
                )
                continue

            event_type = event.get("type")

            # Claude Code stream-json output: each turn emits one or more
            # `assistant` events with content blocks (thinking/text/tool_use),
            # then a single `result` event closes the turn.
            if event_type == "assistant":
                message = event.get("message", {}) or {}
                got_text = False

                # Real per-turn context size (2026-08-06 fix). The old
                # code summed cache_read_input_tokens etc. off the
                # top-level `result` event, which produced a physically
                # impossible 9.68M-token reading on one real turn —
                # confirmed with Amos (his last_assistant_usage() does the
                # same thing we're doing here) that `result`-level usage
                # likely aggregates across every internal tool-call
                # iteration in the turn, since each iteration re-reads the
                # full cached context. This assistant event's own
                # `message.usage` is the real, individual snapshot.
                # Unconditionally overwritten on every assistant event, so
                # by the time the loop ends this holds the LAST one — no
                # separate timestamp-anchoring needed the way Amos's
                # recovery-from-file path requires it, because this loop
                # only ever reads its own subprocess's live stdout in
                # causal order, never replays historical events.
                msg_usage = message.get("usage") or {}
                if msg_usage:
                    last_assistant_usage = msg_usage

                # Merge mode: consecutive tool_use blocks calling the same
                # tool collapse into one "Tool ×N" line instead of N
                # separate gear lines. Matches Amos's (Mike's Karakos
                # instance) design — his version is a real debounced
                # collapse across stream events with a 1.0s window; this is
                # the simpler same-event version (merging only tool_use
                # blocks that land together in one assistant stream event,
                # not a timer-based window across separate events), which
                # covers the common case without needing async timers in
                # this request-scoped loop. Added 2026-08-06.
                pending_tool_name = None
                pending_tool_count = 0
                pending_tool_summary = None

                async def flush_pending_tool():
                    nonlocal pending_tool_name, pending_tool_count, pending_tool_summary, last_discord_msg_id
                    if pending_tool_name and tool_streaming and channel_id != "0":
                        line = (
                            pending_tool_summary if pending_tool_count == 1
                            else f"{pending_tool_name} ×{pending_tool_count}"
                        )
                        if line:
                            msg_id = await post_to_discord(agent, channel_id, f"-# ⚙️ {line}")
                            if msg_id:
                                last_discord_msg_id = msg_id
                    pending_tool_name = None
                    pending_tool_count = 0
                    pending_tool_summary = None

                for block in message.get("content", []) or []:
                    btype = block.get("type")
                    if btype == "text":
                        await flush_pending_tool()
                        text = block.get("text", "")
                        if text:
                            final_text += text
                            response_buffers[agent] = final_text
                            got_text = True
                            if stream_to_channel and channel_id != "0":
                                # A new text block arrived, so whatever was
                                # pending before it wasn't the final answer
                                # after all — flush it as interim, then this
                                # one becomes the new pending segment.
                                await flush_pending_text()
                                pending_interim_text = text
                    elif btype == "tool_use":
                        # A tool call follows, so any pending text segment
                        # was interstitial commentary, not the final answer
                        # — flush it as interim before the tool line posts.
                        await flush_pending_text()
                        tool_name = block.get("name", "unknown")
                        log.info(f"{agent} called tool: {tool_name}")
                        # "-# ⚙️ ..." — Discord subtext markdown so it reads
                        # as quiet metadata, not a normal message. Format
                        # matches Amos's per-tool shapes, shared
                        # 2026-08-06 — see format_tool_summary().
                        summary = format_tool_summary(
                            tool_name, block.get("input", {}) or {}
                        )
                        if summary is None:
                            # Suppressed tool (e.g. TaskList/TaskGet) — a
                            # silent call still breaks run adjacency, but
                            # doesn't start a new pending run of its own.
                            await flush_pending_tool()
                            continue
                        if tool_name == pending_tool_name:
                            pending_tool_count += 1
                            pending_tool_summary = summary
                        else:
                            await flush_pending_tool()
                            pending_tool_name = tool_name
                            pending_tool_count = 1
                            pending_tool_summary = summary
                    # `thinking` blocks are intentionally ignored here — they
                    # are stripped from the final text below as a belt-and-
                    # braces measure for any inline <thinking> tags.

                await flush_pending_tool()

                if got_text:
                    cleaned = THINKING_BLOCK_RE.sub("", final_text)
                    await write_streaming_response(msg_ids, cleaned)

            elif event_type == "rate_limit_event":
                # Anthropic's own live answer to "session usage within the
                # relevant time window" (2026-08-06, Ian's ask). Confirmed
                # with Amos that even his more mature system doesn't read
                # this in-band — his equivalent comes from polling the
                # OAuth usage endpoint out-of-band, which is separate
                # auth, separate cron, and stale between polls. This is
                # neither: it's already on the stream we're reading.
                info = event.get("rate_limit_info", {}) or {}
                if info:
                    await _record_rate_limit_event(agent, info)

            elif event_type == "result":
                # Extract metadata. Cost/duration are top-level and come
                # straight from the CLI's own accounting, which cost_events
                # history confirms stays sane turn over turn — trusted
                # as-is. Token counts used to also come from this event's
                # `usage` field, but that field is what produced the
                # impossible 9.68M-token context reading (see comment
                # above, in the `assistant` branch) — use
                # last_assistant_usage instead for anything context-size
                # related.
                usage = event.get("usage", {}) or {}
                metadata = {
                    "session_id": event.get("session_id"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    # Real per-turn context size — last individual
                    # assistant event's own usage, not this result event's
                    # (likely-aggregated) figures. Falls back to this
                    # event's numbers only if the assistant stream somehow
                    # produced no usage at all, so the field is never just
                    # missing.
                    "cache_read_input_tokens": last_assistant_usage.get(
                        "cache_read_input_tokens", usage.get("cache_read_input_tokens", 0)
                    ),
                    "cache_creation_input_tokens": last_assistant_usage.get(
                        "cache_creation_input_tokens", usage.get("cache_creation_input_tokens", 0)
                    ),
                    "total_cost_usd": event.get("total_cost_usd", 0.0),
                    "duration_ms": event.get("duration_ms", 0),
                    "is_error": event.get("is_error", False),
                }
                # If the assistant stream produced nothing, fall back to
                # the result's flat `result` string (success) or `error`.
                if not final_text:
                    final_text = event.get("result", "") or event.get("error", "")
                # Monthly-spend-cap hard-stop (2026-08-18) — arrives
                # exactly this way: no assistant content, just this flat
                # fallback string. Flag it for process_agent_queue and
                # clear final_text so it never gets treated as a real
                # reply and posted to Discord (see CLI_SPEND_LIMIT_SIGNATURE
                # above for why this incident mattered).
                if CLI_SPEND_LIMIT_SIGNATURE in final_text:
                    metadata["spend_limit_blocked"] = True
                    final_text = ""
                break

    except Exception as e:
        log.error(f"Error reading response from {agent}: {e}")

    # Strip any inline thinking blocks (defense in depth)
    final_text = THINKING_BLOCK_RE.sub("", final_text).strip()

    # Whatever's still pending is the true final answer — nothing followed
    # it in the stream, so it never got flushed as an interim italic post.
    # Posted plain by the caller. If everything that streamed got flushed
    # as interim already (turn ended right after a tool call, no trailing
    # text), there's nothing left to post. If streaming never engaged this
    # turn at all (config off, DM, or an empty assistant stream that fell
    # back to the result event's flat text above), hand back the whole
    # response, same as before this feature existed.
    if pending_interim_text is not None:
        pending_final = THINKING_BLOCK_RE.sub("", pending_interim_text).strip()
    elif text_streamed_this_turn:
        pending_final = ""
    else:
        pending_final = final_text

    agent_states[agent] = "IDLE"
    _write_mechanical_status(agent, None)
    return final_text, metadata, pending_final, last_discord_msg_id

async def queued_ack_sweep_loop():
    """Runs for the life of the process, independent of any single agent's
    turn (Task #13). The per-channel typing indicator added earlier only
    fires from inside process_agent_queue()'s own pass, which is fine for
    "is anyone even looking at this" but doesn't cover a channel that's
    been waiting several minutes through back-to-back busy turns — that
    needs its own clock, not one piggybacked on whichever channel happens
    to be draining right now."""
    while True:
        await asyncio.sleep(QUEUED_ACK_SWEEP_INTERVAL_SEC)
        try:
            await check_queued_acks()
        except Exception as e:
            log.warning(f"queued_ack_sweep_loop error (non-fatal): {e}")

async def rate_limit_gate_sweep_loop():
    """Runs for the life of the process. process_agent_queue() is only
    triggered reactively — a new message arrives, or another channel is
    left queued after a drain — so a paused agent (see
    is_rate_limit_paused()) has nothing to re-trigger it once the
    five-hour window actually resets; a new Discord message isn't
    guaranteed to show up right at that moment. This is what actually
    resumes a paused queue instead of leaving it stuck until unrelated
    traffic happens to arrive. process_agent_queue() is cheap to call
    when there's nothing to do (acquires the lock, checks IDLE / pause /
    pending messages, returns immediately), so calling it unconditionally
    for every agent on each tick is fine."""
    while True:
        await asyncio.sleep(RATE_LIMIT_GATE_SWEEP_INTERVAL_SEC)
        for agent in list(agent_config.keys()):
            try:
                _spawn(process_agent_queue(agent))
            except Exception as e:
                log.warning(f"rate_limit_gate_sweep_loop error for {agent} (non-fatal): {e}")

async def check_queued_acks():
    """Find the oldest STATUS_QUEUED message per (agent, channel), and for
    any that's been waiting long enough and isn't on cooldown, post a
    deterministic ack — no model call. Amos's race-condition catch: the
    sweep and the drain aren't ordered against each other, so a turn that
    finishes right as this fires could post the ack immediately followed
    by the real answer, which reads worse than silence. Guarded by
    re-checking the specific row is still STATUS_QUEUED right before
    sending, not just when this sweep started."""
    if db is None:
        return

    async with db.execute(
        "SELECT agent, channel_id, message_id, created_at FROM message_queue WHERE processed = ?",
        (STATUS_QUEUED,)
    ) as cursor:
        rows = await cursor.fetchall()

    oldest: Dict[tuple, Dict] = {}
    for row in rows:
        if row["channel_id"] == "0":
            continue
        key = (row["agent"], row["channel_id"])
        if key not in oldest or row["created_at"] < oldest[key]["created_at"]:
            oldest[key] = dict(row)

    now = time.time()
    for (agent, channel_id), row in oldest.items():
        try:
            created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            waited_sec = (datetime.utcnow() - created).total_seconds()
        except (ValueError, TypeError):
            continue

        if waited_sec < QUEUED_ACK_WAIT_THRESHOLD_SEC:
            continue
        if channel_last_acked_message_id.get(channel_id) == row["message_id"]:
            # Already sent an ack for this exact queued message — the
            # cooldown below is for spacing acks about *different*
            # messages, not for repeating one about the same message
            # that just hasn't moved yet (e.g. a rate-limit pause).
            continue
        last_ack = channel_last_ack.get(channel_id, 0)
        if now - last_ack < QUEUED_ACK_COOLDOWN_SEC:
            continue

        # Race guard, corrected 2026-08-06 per Amos: a re-check
        # immediately before sending still leaves a gap for exactly as
        # long as post_to_discord()'s own network call takes — the drain
        # can acquire the lock, complete a turn, and post the real answer
        # during that window, so the ack can still land after it despite
        # the recheck saying "still queued" moments earlier. Closing the
        # gap for real (not just narrowing it) means holding the SAME
        # lock process_agent_queue() uses across both the recheck and
        # the send — while this lock is held, the drain cannot be
        # running for this agent at all, so there is no window left for
        # it to interleave in. Costs a brief serialization (drain start
        # waits for the ack post to finish, typically sub-second) in
        # exchange for an actual guarantee instead of a narrowed race.
        agent_lock = agent_locks.get(agent)
        if agent_lock is None:
            continue
        async with agent_lock:
            async with db.execute(
                "SELECT processed FROM message_queue WHERE message_id = ?",
                (row["message_id"],)
            ) as cursor:
                current = await cursor.fetchone()
            if not current or current["processed"] != STATUS_QUEUED:
                continue

            # Per Ian (#general 2026-08-08 07:46:31): a generic ack reads
            # as evasive when the real reason is known — e.g. paused for
            # the rate-limit warning zone can sit for hours, which is a
            # very different wait than an ordinary busy turn. Surface the
            # actual cause when we have one; fall back to the old generic
            # copy only when it's just normal turn-processing.
            if is_rate_limit_paused(agent):
                # 2026-09-03: primary window (task-1788454188) — see
                # _rate_limit_primary()'s docstring for the selection rule.
                resets_at = (_rate_limit_primary(agent) or {}).get("resetsAt")
                if resets_at:
                    reset_str = format_reset_time(resets_at)
                    reason = f"paused — five-hour rate limit window in the warning zone, resumes ~{reset_str}"
                else:
                    reason = "paused — five-hour rate limit window in the warning zone"
            else:
                reason = "finishing up elsewhere, will get to this shortly"

            await post_to_discord(
                agent, channel_id,
                f"-# ⏳ queued — {reason}"
            )
            channel_last_ack[channel_id] = now
            channel_last_acked_message_id[channel_id] = row["message_id"]

async def process_agent_queue(agent: str):
    """Process pending messages for agent"""
    lock = agent_locks.get(agent)
    if not lock:
        return

    async with lock:
        if agent_states.get(agent) != "IDLE":
            return

        # Rate-limit circuit breaker (2026-08-07) — checked before
        # touching message_queue at all. Paused messages stay
        # STATUS_QUEUED untouched; rate_limit_gate_sweep_loop is what
        # retries this once the window clears, since a new Discord
        # message isn't guaranteed to arrive right when that happens.
        # See is_rate_limit_paused() / RATE_LIMIT_PAUSE_STATUS above —
        # built after both agents sat at 99% utilization for over four
        # hours tonight with only a manual, behavioral freeze holding
        # the line.
        if is_rate_limit_paused(agent):
            if not agent_rate_limit_pause_notified.get(agent):
                agent_rate_limit_pause_notified[agent] = True
                log.warning(f"{agent} paused — rate limit in warning zone, holding queued messages")
                _spawn(_notify_rate_limit_pause(agent, paused=True))

            # Heartbeats still get through — see RATE_LIMIT_HEARTBEAT_AUTHOR
            # above. Query is scoped to heartbeat messages only so a paused
            # agent never batches real conversation in alongside one.
            async with db.execute(
                """
                SELECT * FROM message_queue
                WHERE agent = ? AND processed = ? AND author = ?
                ORDER BY created_at ASC
                LIMIT 20
                """,
                (agent, STATUS_QUEUED, RATE_LIMIT_HEARTBEAT_AUTHOR)
            ) as cursor:
                all_pending = await cursor.fetchall()
            if not all_pending:
                return
            log.info(f"{agent} paused but letting heartbeat through — window headroom covers it")
        else:
            if agent_rate_limit_pause_notified.get(agent):
                agent_rate_limit_pause_notified[agent] = False
                log.info(f"{agent} resumed — rate limit back to normal")
                _spawn(_notify_rate_limit_pause(agent, paused=False))

            if is_rate_limit_warning(agent):
                if not agent_rate_limit_warning_notified.get(agent):
                    agent_rate_limit_warning_notified[agent] = True
                    log.warning(f"{agent} rate limit in warning zone (not paused, still under {RATE_LIMIT_UTILIZATION_PAUSE_THRESHOLD:.0%})")
                    _spawn(_notify_rate_limit_warning(agent, warning=True))
            elif agent_rate_limit_warning_notified.get(agent):
                agent_rate_limit_warning_notified[agent] = False
                log.info(f"{agent} rate limit warning cleared")
                _spawn(_notify_rate_limit_warning(agent, warning=False))

            # Get pending messages
            async with db.execute(
                """
                SELECT * FROM message_queue
                WHERE agent = ? AND processed = ?
                ORDER BY created_at ASC
                LIMIT 20
                """,
                (agent, STATUS_QUEUED)
            ) as cursor:
                all_pending = await cursor.fetchall()

        if not all_pending:
            return

        # Process only the oldest message's channel this pass. Messages
        # queued for OTHER channels while this agent was busy must never be
        # merged into the same response and posted to a single channel —
        # found 2026-08-05 when a #signals heartbeat and a live exchange in
        # a different Discord server's #agent-chat landed in the queue
        # together; the combined response (including internal heartbeat
        # status) posted only to #agent-chat, leaking it into a shared
        # external server. Leftover messages for other channels stay
        # STATUS_QUEUED and get drained in a follow-up pass (see the
        # self-continuation check after the lock releases below).
        target_channel_id = all_pending[0]["channel_id"]
        messages = [m for m in all_pending if m["channel_id"] == target_channel_id]

        # Typing indicator for every OTHER channel with a message waiting
        # behind this one, not just the one being drained (Task #13,
        # 2026-08-06 — the "simultaneous conversation" problem: a channel
        # queued behind a busy turn showed zero signal — no typing, no
        # ack — until its own turn finally started, so replies "read as a
        # clump then all at once"). Confirmed with Amos his system has the
        # identical structural limit (one lock, one subprocess, strictly
        # serial); this is the fix he'd adopt too — "strictly better than
        # what either of us runs," a Discord API call with no model
        # anywhere near it. Not stopped explicitly here: start_typing()
        # is idempotent per channel_id, and each channel's own real pass
        # through this function calls stop_typing() once its actual
        # response is ready, same as it always has.
        waiting_channel_ids = {m["channel_id"] for m in all_pending} - {target_channel_id}
        for waiting_channel_id in waiting_channel_ids:
            await start_typing(agent, waiting_channel_id)

        # Mark as in progress
        message_ids = [msg["message_id"] for msg in messages]
        await db.execute(
            f"""
            UPDATE message_queue
            SET processed = ?, processing_started_at = CURRENT_TIMESTAMP
            WHERE message_id IN ({','.join('?' * len(message_ids))})
            """,
            (STATUS_IN_PROGRESS, *message_ids)
        )
        await db.commit()

        # Format batch
        channel_id = target_channel_id
        # Explicit channel header (2026-08-06) — the actual root cause
        # behind four separate misrouting incidents tonight: nothing in
        # the prompt ever told the agent which channel a turn was
        # actually scoped to, so content addressed to a third party (most
        # often Amos, discussed but not present in the triggering
        # message) kept winning over the channel the reply would really
        # post to. Each incident got patched by hand after the fact via a
        # direct Discord API call — this is the fix for the cause instead
        # of the symptom. Every response posts to exactly one channel
        # regardless of who it's "about"; say so plainly, every turn.
        channel_name = next(
            (name for name, cfg in channels_config.get("channels", {}).items()
             if cfg.get("id") == channel_id),
            None,
        )
        if channel_name:
            channel_label = f"#{channel_name}"
        else:
            # Not a real Discord channel — e.g. the dashboard's silent
            # chat, which deliberately uses channel_id sentinel "0" and
            # has no Discord channel to cross-post to at all (see
            # dashboard/app/api/chat/route.ts). Falls back to the channel
            # name already resolved and stored at insert time
            # (messages[0]["channel"]) instead of the raw channel_id,
            # which for this case is literally the string "0" — found
            # live 2026-08-18 rendering as a nonsense "#0" header when
            # Ian tested via the dashboard mid-incident.
            fallback_name = messages[0]["channel"] or channel_id
            if messages[0]["server"] == "discord":
                # A real Discord channel_id that just isn't in our own
                # channels_config (foreign guild, etc.) — still phrase it
                # as a channel, best effort.
                channel_label = f"#{fallback_name}"
            else:
                channel_label = f"the {fallback_name} chat (not Discord — nothing to cross-post to)"
        formatted_parts = [
            f"[This turn posts ONLY to {channel_label} — regardless of who "
            f"the conversation is about, your response goes here and "
            f"nowhere else. If this content is meant for someone in a "
            f"different channel, say so explicitly rather than writing as "
            f"if they'll see it.]"
        ]

        # Backlog callout (2026-08-29, Ian: a paused agent resuming had no
        # visibility into how much real conversation happened while it was
        # down — a real 2.5h gap only got noticed because someone manually
        # checked logs and the DB after the fact). Each message already
        # carries its own timestamp below, but that's easy to skim past
        # across a batch — this makes the gap itself impossible to miss,
        # not just technically present. Threshold (5 min) is deliberately
        # loose: an ordinary quick multi-message batch (several messages
        # arriving within a normal conversational back-and-forth) shouldn't
        # get flagged as a "gap," only a batch that actually spans a real
        # absence — rate-limit pause, restart, or anything else that held
        # messages queued a while.
        if len(messages) > 1:
            span_seconds = _batch_span_seconds(messages)
            if span_seconds is not None and span_seconds > BACKLOG_GAP_THRESHOLD_SEC:
                formatted_parts.append(
                    f"[Backlog note: this batch of {len(messages)} messages "
                    f"spans {_format_span(span_seconds)} — real conversation "
                    f"may have happened here while you were paused, "
                    f"restarting, or otherwise unavailable. Worth reading "
                    f"the whole batch before assuming it's a normal "
                    f"back-and-forth.]"
                )

        for msg in messages:
            timestamp = msg["created_at"]
            author = msg["author"]
            content = msg["content"]
            part = f"[{timestamp}] {author}: {content}"
            attachment_lines = format_attachments(msg["attachments"])
            if attachment_lines:
                part = f"{part}\n{attachment_lines}"
            formatted_parts.append(part)

        # Mid-turn steering, entry-side check (2026-08-30, Ian's design —
        # #general 2026-08-29/30, 30s threshold agreed live). Companion to
        # the exit-side snapshot below: this one asks "is the message I'm
        # about to answer already stale by the time I've picked it up" —
        # busy in another channel, a rate-limit pause, a restart — rather
        # than "did the channel move while I was thinking." Only meaningful
        # for real Discord channels with real snowflake message_ids (the
        # dashboard's silent chat has neither); `after_id` on the fetch
        # means only messages genuinely newer than this batch's own newest
        # come back, so nothing already shown above gets duplicated.
        if messages[-1]["server"] == "discord" and channel_id != "0":
            try:
                newest_created = datetime.strptime(
                    messages[-1]["created_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                trigger_age = (datetime.now(timezone.utc) - newest_created).total_seconds()
            except (TypeError, ValueError):
                trigger_age = None

            if trigger_age is not None and trigger_age > ENTRY_STALE_THRESHOLD_SEC:
                newest_message_id = messages[-1]["message_id"]
                recent = await get_recent_channel_messages(
                    agent, channel_id, after_id=newest_message_id
                )
                if recent:
                    log.info(
                        f"[entry-stale-check] {agent}: trigger message in "
                        f"{channel_label} is {trigger_age:.0f}s old, found "
                        f"{len(recent)} newer message(s) live — injecting "
                        f"as context"
                    )
                    recent_lines = [
                        f"[{m.get('timestamp', '')}] "
                        f"{(m.get('author') or {}).get('username', 'unknown')}: "
                        f"{m.get('content', '')}"
                        for m in recent
                    ]
                    formatted_parts.append(
                        f"[Entry stale-check: the message above that triggered "
                        f"this turn is {trigger_age:.0f}s old — {len(recent)} "
                        f"more message(s) have landed live in {channel_label} "
                        f"since then, fetched fresh rather than relying on a "
                        f"stale snapshot:]\n" + "\n\n".join(recent_lines)
                    )

        formatted_content = "\n\n".join(formatted_parts)

        # Mid-turn steering, Phase 1 — instrumentation only (2026-08-30,
        # following Crab Cavern's Pre-Flight Stale Gate design from Zero;
        # see #agent-chat 2026-08-29/30). Snapshot the channel's actual
        # last message ID via the Discord REST API — not just this
        # agent's own message_queue, which reply-gating can skip
        # entirely — right before generation starts, so staleness can be
        # detected afterward regardless of who spoke or whether it was
        # ever queued for us. Deliberately NOT wired to abort, edit, or
        # re-steer the draft yet: this exact hot path has a real history
        # of subtle races (the 2000-char silent drop, the asyncio
        # fire-and-forget GC bug, the reload-hook self-kill), so before
        # anything auto-cancels a turn's output, this logs how often
        # staleness actually happens. See the check after read_agent_
        # response below for Phase 2 notes.
        preflight_snapshot_id = await get_latest_channel_message_id(agent, channel_id)

        # Start typing indicator
        await start_typing(agent, channel_id)

        # Mechanical presence activity text — see _write_mechanical_status.
        # "replying in #x" always; batch size and other-channels-waiting
        # only appended when true, so an ordinary single-message turn in
        # one channel reads as just "replying in #x", not padded noise.
        # Reuses channel_label (not the bare channel_name, which is None
        # for non-Discord origins like the dashboard's silent chat — see
        # the channel_label derivation above) so this never renders
        # "replying in #None".
        activity_bits = [f"replying in {channel_label}"]
        if len(messages) > 1:
            activity_bits.append(f"batch of {len(messages)}")
            span_seconds = _batch_span_seconds(messages)
            if span_seconds is not None and span_seconds > BACKLOG_GAP_THRESHOLD_SEC:
                activity_bits.append(f"gap of {_format_span(span_seconds)}")
        if waiting_channel_ids:
            activity_bits.append(f"+{len(waiting_channel_ids)} channel(s) queued")
        activity_text = ", ".join(activity_bits)

        # Everything from here through the claim above is wrapped in
        # try/finally (task-1788042889, 2026-08-29): before this, a crash
        # anywhere in this span — send_to_agent, read_agent_response,
        # post_to_discord, the DB calls — skipped the release check below
        # entirely, since it sat unconditionally after all of this rather
        # than being guaranteed. An external claim held at that point then
        # sat until banana.py's 600s CEILING_SECONDS timeout instead of
        # being handed back immediately. The claim block stays inside the
        # try (claiming a new floor mid-crash makes no sense, and it needs
        # response_text, which may never get assigned); only the release
        # check moves to finally, since it doesn't depend on response_text
        # at all — safe to run whether or not the try body completed.
        try:
            # Send to agent
            await send_to_agent(agent, formatted_content, message_ids, activity=activity_text)

            # Read response
            response_text, metadata, pending_final, discord_msg_id = await read_agent_response(
                agent, channel_id, message_ids
            )

            # Stop typing
            await stop_typing(channel_id)

            # Mid-turn steering, Phase 1 continued: compare against the
            # snapshot taken before generation started. Snowflake IDs
            # sort numerically, so a plain int comparison tells us
            # something genuinely new landed (not just a different-but-
            # equally-stale ID from a fetch race). Log only — Phase 2
            # (actually discarding/re-steering a stale draft) is a
            # separate, deliberately un-rushed follow-up; see the
            # snapshot comment above for why this stays observe-only for
            # now. metadata check mirrors the spend-cap block above: only
            # meaningful for a turn that actually completed.
            if metadata and preflight_snapshot_id:
                post_snapshot_id = await get_latest_channel_message_id(agent, channel_id)
                try:
                    moved = (
                        post_snapshot_id is not None
                        and int(post_snapshot_id) > int(preflight_snapshot_id)
                    )
                except (TypeError, ValueError):
                    moved = False
                if moved:
                    log.warning(
                        f"[stale-gate] {agent}: {channel_label} moved during "
                        f"generation (snapshot {preflight_snapshot_id} -> "
                        f"{post_snapshot_id}). Posting the draft as-is — "
                        f"Phase 1 is detect-and-log only, no auto-abort or "
                        f"re-steer yet."
                    )

            # Post cost update
            if metadata:
                await post_cost_update(agent, metadata)
                await update_session_tokens(agent, metadata.get("input_tokens", 0))

            # Monthly spend-cap block/clear (2026-08-18) — see
            # CLI_SPEND_LIMIT_SIGNATURE / _notify_spend_limit(). Only checked
            # when a turn actually completed (metadata present); a turn that
            # errored out of read_agent_response before reaching the result
            # event leaves metadata empty, and this deliberately no-ops rather
            # than guessing at either state from an incomplete turn.
            if metadata:
                if metadata.get("spend_limit_blocked"):
                    agent_spend_limit_blocked[agent] = True
                    if not agent_spend_limit_notified.get(agent):
                        agent_spend_limit_notified[agent] = True
                        _spawn(_notify_spend_limit(agent, blocked=True))
                elif agent_spend_limit_blocked.get(agent):
                    agent_spend_limit_blocked[agent] = False
                    agent_spend_limit_notified[agent] = False
                    _spawn(_notify_spend_limit(agent, blocked=False))

            # Post whatever's left to Discord. When stream_to_channel is on,
            # most (or all) of response_text already went out incrementally
            # inside read_agent_response — interim segments as italic asides,
            # tool calls as "-# " subtext. pending_final is only the remainder:
            # the true final answer if the turn ended mid-text (plain, no
            # italics), empty if the last thing streamed was itself final
            # already, or the whole response if streaming never engaged this
            # turn. discord_msg_id starts as whatever last posted during
            # streaming, so history still gets a real message ID even on turns
            # where this post is skipped.
            #
            # Speaking Banana pre-post turn claim (Zero parity PR):
            # Deterministically claim the floor BEFORE sending any payload to Discord
            # if this channel is in-scope for multi-bot coordination. Avoids the inverted
            # order of operations where a post went out un-mutexed, and avoids depending
            # on the model emitting a leading 🍌 emoji or structured reply tag.
            held_post_banana = False
            if (
                channel_name
                and pending_final
                and channel_id != "0"
                and not is_silence_announcement(pending_final)
                and banana.in_scope(channel_id, channels_config)
            ):
                try:
                    if agent == "Marvin":
                        claim_res = await banana.claim_self(channel_name, subject=channel_name)
                        held_post_banana = not (isinstance(claim_res, dict) and claim_res.get("blocked"))
                    else:
                        banana.claim(channel_name, agent)
                        held_post_banana = True
                except Exception as ce:
                    log.warning(f"[banana] {channel_name}: pre-post claim failed: {ce}")

            try:
                # is_silence_announcement() (2026-09-02): a non-empty pending_final
                # is not automatically a real answer — a turn deciding "not mine
                # to answer" is a decision, and posting that decision is exactly
                # the failure mode reply_gate.py's docstring already warns
                # against ("Silence should be the normal state between two
                # agents"). See the comment above PASS_SENTINEL_RE.
                if pending_final and channel_id != "0" and not is_silence_announcement(pending_final):
                    discord_msg_id = await post_to_discord(agent, channel_id, pending_final)
                elif pending_final and is_silence_announcement(pending_final):
                    log.info(
                        "suppressing silence-announcement post (channel=%s): %r",
                        channel_id, pending_final[:200],
                    )
            finally:
                if held_post_banana:
                    try:
                        if agent == "Marvin":
                            await banana.release_self(channel_name)
                        else:
                            banana.release(channel_name, agent)
                    except Exception as re:
                        log.exception(f"[banana] {channel_name}: post-delivery release failed: {re}")

            # Voice-presence, Phase 1 (2026-09-01, task-1788226029): score
            # this turn's full reply against voice.md's register via
            # embedding cosine-similarity, detect-and-log only -- no
            # blocking, no live model call. Same posture as the stale-gate
            # work above (observe first, don't ask discipline to hold
            # where it's already failed repeatedly). _spawn() so scoring
            # never adds latency to a reply that's already posted; scored
            # against response_text (the full turn), not pending_final.
            if response_text and channel_name:
                _spawn(_voice_presence_log(agent, channel_name, response_text))
        finally:
            # Explicit hand-back, symmetric to the claim above — closes the
            # gap Amos and I both flagged live tonight (#agent-chat,
            # banana-conflict-test, 2026-08-29): claims were only ever
            # released from a CLI/test invocation, never from either bot's
            # real posting path, so every claim was surviving purely on the
            # 600s ceiling timeout instead of the explicit hand-back the
            # design actually calls for. Amos's fix released from his Stop
            # hook (real turn-end); this is the equivalent point on this
            # side — after this channel's reply for this turn is fully
            # composed, whether or not this same turn just claimed. Cheap on
            # the common turn: get_status() is a local file read, no network
            # call, so this only reaches the shared API when local belief
            # says this agent is still the active holder. Now in `finally`
            # (task-1788042889) so a mid-try exception still hands back
            # whatever claim is on record instead of abandoning it to the
            # ceiling timeout; wrapped in its own try/except since a
            # cleanup step raising would replace the real exception with a
            # confusing one instead of just logging alongside it.
            try:
                if channel_name and banana.in_scope(channel_id, channels_config):
                    status = banana.get_status(channel_name)
                    if status.get("active") and status.get("holder") == agent:
                        if agent == "Marvin":
                            await banana.release_self(channel_name)
                        else:
                            banana.release(channel_name, agent)
            except Exception:
                log.exception(f"[banana] {channel_name}: release-on-cleanup failed")

        # context_box outbound half (handoff.py docstring, context_box.py's
        # module docstring, facts/context-box-2026-08-27.md gap #1): relay.py
        # only parses *inbound* messages for a context_box field — on_message
        # returns early on `message.author == self.user`, so this agent's own
        # blockers never got recorded or mirrored. Same fix shape as the
        # banana outbound hook just above: catch it here, at compose time,
        # against the full response_text (not pending_final, for the same
        # incremental-streaming reason). Scoped to tier2 gate_mode channels
        # only, matching relay.py's inbound gating (currently #agent-chat) —
        # a stray ```handoff fence typed in #general or #lounge shouldn't be
        # parsed as a real envelope.
        channel_config = (
            channels_config.get("channels", {}).get(channel_name, {})
            if channel_name else {}
        )
        if channel_config.get("gate_mode") == "tier2" and response_text:
            envelope = parse_handoff(response_text)
            if envelope and envelope.context_box:
                cb = envelope.context_box
                row = context_box.record(
                    subject=envelope.subject,
                    state=cb.state,
                    blocked_on=cb.blocked_on,
                    waiting_on=cb.waiting_on,
                    sender=agent,
                    channel=channel_name,
                )
                if context_box.should_mirror(cb.state):
                    add_pending(
                        "general",
                        context_box.render_mirror_line(envelope.subject or "(no subject)", row),
                    )
                    log.info(
                        f"[context_box] outbound {channel_name} subject={envelope.subject!r} "
                        f"state={cb.state} -> mirrored to #general"
                    )

        # Mark complete
        await db.execute(
            f"""
            UPDATE message_queue
            SET processed = ?, response = ?, discord_response_id = ?, processed_at = CURRENT_TIMESTAMP
            WHERE message_id IN ({','.join('?' * len(message_ids))})
            """,
            (STATUS_COMPLETE, response_text, discord_msg_id, *message_ids)
        )
        await db.commit()

        log.info(f"{agent} processed {len(message_ids)} messages")

    # Lock released above. If messages for other channels were left queued
    # (deferred by the same-channel filter this pass), keep draining rather
    # than waiting for the next externally-triggered message — otherwise a
    # channel with no new traffic could sit queued indefinitely behind a
    # busy one. A fresh task re-acquires the lock and re-checks IDLE state
    # itself, so this is safe to fire unconditionally.
    async with db.execute(
        "SELECT 1 FROM message_queue WHERE agent = ? AND processed = ? LIMIT 1",
        (agent, STATUS_QUEUED)
    ) as cursor:
        remaining = await cursor.fetchone()
    if remaining:
        _spawn(process_agent_queue(agent))

    # Context-fill visibility (2026-08-07, Ian's ask) — same
    # estimate_context_tokens() inputs the compaction trigger below uses,
    # recorded for /agents and the heartbeat to surface regardless of
    # whether compaction itself fires this turn.
    if metadata:
        _ctx_estimate = estimate_context_tokens(metadata)
        _ctx_level = "none"
        if _ctx_estimate >= CONTEXT_CRITICAL_WARNING_TOKENS:
            _ctx_level = "critical"
        elif _ctx_estimate >= CONTEXT_CONCERN_TOKENS:
            _ctx_level = "concern"
        elif _ctx_estimate >= COMPACTION_TARGET_TOKENS:
            _ctx_level = "soft"
        agent_context_usage[agent] = {
            "estimated_tokens": _ctx_estimate,
            "context_window": CONTEXT_WINDOW_TOKENS,
            "pct": round(100 * _ctx_estimate / CONTEXT_WINDOW_TOKENS, 1),
            "warning_level": _ctx_level,
        }
        if _ctx_level == "soft":
            log.info(f"{agent} context at {_ctx_estimate:,} tokens — soft target crossed (>{COMPACTION_TARGET_TOKENS:,}), compaction below should clear it this turn")
        elif _ctx_level == "concern":
            log.warning(f"{agent} context at {_ctx_estimate:,} tokens — past the {CONTEXT_CONCERN_TOKENS:,}-token concern mark, soft-target compaction has been failing to clear it")
        elif _ctx_level == "critical":
            # Should be unreachable — compaction fires at COMPACTION_TARGET_TOKENS
            # (200k), backstopped by the concern log at 500k, well before this.
            # Getting here means compaction itself silently failed this
            # session for a while; page directly rather than wait for the
            # next heartbeat.
            log.error(f"{agent} context at {_ctx_estimate:,} tokens — CRITICAL, compaction should already have fired and didn't")
            try:
                signals_channel = (channels_config.get("channels", {}).get("signals", {}) or {}).get("id")
                if signals_channel:
                    await post_to_discord(
                        agent, signals_channel,
                        f"-# 🚨 {agent} context at {_ctx_estimate:,} tokens ({100*_ctx_estimate/CONTEXT_WINDOW_TOKENS:.0f}% of window) — "
                        f"past the {CONTEXT_CRITICAL_WARNING_TOKENS:,}-token critical mark. Compaction should have "
                        f"triggered at {COMPACTION_TARGET_TOKENS:,} and didn't — needs a look."
                    )
            except Exception as e:
                log.warning(f"Critical context alert failed to post (non-fatal): {e}")

    # Automatic context compaction (Amos's model, adopted 2026-08-06) —
    # RE-ENABLED 2026-08-07 per Ian, after a real session (this one) hit
    # the trigger's territory live and he asked for a formal safeguard
    # rather than manual eyeballing. Was disabled same-day as the original
    # deploy for three real, now-resolved reasons:
    #   1. estimate_context_tokens() summed usage off the wrong stream-json
    #      event (aggregated across a turn's internal iterations, not a
    #      per-turn snapshot) — produced a physically impossible 9.68M
    #      reading. Fixed 2026-08-06: reads the last individual `assistant`
    #      event's own usage instead.
    #   2. summarize-session.py failed every time ("No recent stream data")
    #      because logs/agent-streams/ was never actually written to. Fixed
    #      2026-08-06: reads real Claude CLI transcripts instead.
    #   3. CONTEXT_WINDOW_TOKENS was hardcoded to 200k, stale against
    #      current Sonnet's real 1M window — fixed just above, same pass
    #      as this re-enable.
    # All three verified independently before flipping this back on.
    #
    # Second trigger added 2026-08-07: gap-and-channel topic-change check
    # (maybe_topic_change_compact). Only runs when the token-target
    # trigger above did NOT already compact this turn — no point spending
    # a classifier call to decide whether to do something that already
    # happened, and agent_channel_last_turn still needs updating either
    # way so the next gap in this channel has a real baseline to compare
    # against.
    if metadata:
        compacted = await maybe_compact_session(agent, metadata)
        compacted = await maybe_rate_limit_compact(agent, already_compacted=compacted) or compacted
        await maybe_topic_change_compact(agent, channel_id, formatted_content, metadata, already_compacted=compacted)

# =============================================================================
# Crash Recovery
# =============================================================================

async def crash_recovery():
    """Recover from crashes on startup"""
    # Find messages stuck in PROCESSING state
    async with db.execute(
        "SELECT * FROM message_queue WHERE processed = ?",
        (STATUS_IN_PROGRESS,)
    ) as cursor:
        stuck_messages = await cursor.fetchall()

    if stuck_messages:
        log.warning(f"Found {len(stuck_messages)} stuck messages from previous crash")

        for msg in stuck_messages:
            # Mark as crashed
            await db.execute(
                "UPDATE message_queue SET processed = ? WHERE message_id = ?",
                (STATUS_CRASHED, msg["message_id"])
            )

            # Notify channel
            channel_id = msg["channel_id"]
            agent = msg["agent"]
            if channel_id != "0":
                crash_msg = f"⚠️ {agent} crashed while processing message from {msg['author']}"
                await post_to_discord(agent, channel_id, crash_msg)

        await db.commit()

    # Retry posting messages that completed but weren't posted
    async with db.execute(
        "SELECT * FROM message_queue WHERE processed = ? AND discord_response_id IS NULL AND channel_id != '0'",
        (STATUS_COMPLETE,)
    ) as cursor:
        unposted = await cursor.fetchall()

    if unposted:
        # Empty-response rows can never actually post — post_to_discord()
        # below is only called `if msg["response"]`, so these always fell
        # through untouched. Historically produced by killing an agent
        # subprocess mid-turn (restart_agent()/reload_agent() before their
        # 2026-08-11 lock-gating fix, see those docstrings): the readline()
        # loop hits EOF, the turn's response comes back empty, and it still
        # gets marked STATUS_COMPLETE. Since that fix, nothing new should
        # land in this bucket — the 47 found here on 2026-08-29 all predate
        # it (newest: 2026-08-18). Left alone, crash_recovery() rediscovers
        # and silently no-ops the same dead rows on *every* startup forever,
        # with a "Found N unposted responses, retrying" warning that reads
        # like an active problem. One-time cleanup: mark them STATUS_SKIPPED
        # so this stops recurring; genuinely retryable rows (real response
        # text, just never confirmed posted) still go through below as before.
        empty = [m for m in unposted if not m["response"]]
        retryable = [m for m in unposted if m["response"]]

        if empty:
            log.info(f"Marking {len(empty)} empty-response stale rows as skipped (never postable, pre-2026-08-11 mid-turn-kill artifacts)")
            await db.executemany(
                "UPDATE message_queue SET processed = ? WHERE message_id = ?",
                [(STATUS_SKIPPED, m["message_id"]) for m in empty]
            )
            await db.commit()

        if retryable:
            log.warning(f"Found {len(retryable)} unposted responses, retrying")
            for msg in retryable:
                discord_id = await post_to_discord(msg["agent"], msg["channel_id"], msg["response"])
                if discord_id:
                    # Commit per-message, not once after the whole loop. The
                    # old batched commit meant a crash partway through this
                    # loop left every already-posted-but-not-yet-committed
                    # message's discord_response_id at NULL, so the *next*
                    # crash_recovery() sweep would find and repost them —
                    # the recovery path duplicating exactly what it exists
                    # to prevent. Committing immediately after each
                    # successful post bounds the risk to the single message
                    # in flight at crash time, not the whole batch.
                    await db.execute(
                        "UPDATE message_queue SET discord_response_id = ? WHERE message_id = ?",
                        (discord_id, msg["message_id"])
                    )
                    await db.commit()

# =============================================================================
# HTTP API
# =============================================================================

async def handle_message(request):
    """POST /message - Queue message for agent"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()

    agent = data.get("agent")
    channel = data.get("channel", "general")
    channel_id = data.get("channel_id", "0")
    server = data.get("server", "discord")
    author = data.get("author", "unknown")
    author_id = data.get("author_id", "0")
    is_bot = data.get("is_bot", False)
    content = data.get("content", "")
    message_id = data.get("message_id", f"msg-{uuid.uuid4()}")
    mentions_agent = data.get("mentions_agent", False)
    attachments = data.get("attachments") or []
    if not isinstance(attachments, list):
        return web.json_response({"error": "attachments must be a list"}, status=400)

    if not agent or agent not in agent_config:
        return web.json_response({"error": "Invalid agent"}, status=400)

    # An image posted with no caption is a real message with empty text —
    # it used to be rejected here as "Empty content" and never reached the
    # agent at all (2026-08-09, ported from mcarmody/karakos-package#127).
    if not content and not attachments:
        return web.json_response({"error": "Empty content"}, status=400)

    # Cost limits no longer reject inbound messages, as of 2026-08-06. The
    # hard dollar-based rejection here used to silently drop agent-to-agent
    # messages with no retry (a real incident: $53.97 actual spend vs the
    # $25 cap, one of Amos's messages 429-rejected and lost, no requeue).
    # Ian's decision: adopt Amos's model instead — protection is
    # QUEUE_DEPTH_LIMIT below (already matches his number) plus automatic
    # context compaction (maybe_compact_session, called from
    # process_agent_queue) rather than a spend ceiling. COST_DAILY_LIMIT /
    # COST_MONTHLY_LIMIT stay defined for `cost-report.sh` visibility
    # (handle_cost_get queries cost_events directly); check_cost_limits(),
    # which only ever fed this now-removed rejection, was deleted outright
    # rather than left as dead code.

    # Check queue depth
    async with db.execute(
        "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
        (agent, STATUS_QUEUED)
    ) as cursor:
        row = await cursor.fetchone()
        if row["count"] >= QUEUE_DEPTH_LIMIT:
            return web.json_response({"error": "Queue full"}, status=503)

    # Insert message
    try:
        await db.execute(
            """
            INSERT INTO message_queue
            (agent, channel, channel_id, server, author, author_id, is_bot, content, message_id, mentions_agent, attachments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent, channel, channel_id, server, author, author_id, int(is_bot), content, message_id,
             int(mentions_agent), json.dumps(attachments) if attachments else None)
        )
        await db.commit()
    except aiosqlite.IntegrityError as e:
        # message_id is UNIQUE — a duplicate insert here means this exact
        # message already made it into the queue (or was already fully
        # processed) on an earlier attempt, most often relay.py retrying a
        # deferred poke whose original request actually succeeded but
        # whose response got lost before relay could see it (e.g. the
        # server died mid-response — see the 08:02-08:05 duplicate-process
        # incident, agent-server-duplicate-process-incident.md, where this
        # happened for real). Retrying can never turn that into a
        # different outcome, so treat it as success: relay's deferred-poke
        # flush loop deletes the spool file on any 2xx, same as if the
        # first attempt's response had actually arrived. Previously
        # returned 500 here, which relay retried for up to 24h
        # (DEFERRED_POKE_MAX_AGE_SEC in relay.py) against a failure that
        # could never resolve by retrying.
        log.info(f"Duplicate message_id {message_id} — already queued/processed, treating as success: {e}")
        return web.json_response({"status": "duplicate", "message_id": message_id})
    except Exception as e:
        log.error(f"Error inserting message: {e}")
        return web.json_response({"error": "Database error"}, status=500)

    # Trigger processing if agent is idle
    if agent_states.get(agent) == "IDLE":
        _spawn(process_agent_queue(agent))

    return web.json_response({"status": "queued", "message_id": message_id}, status=202)

async def handle_status(request):
    """GET /status - Rich per-agent status for the dashboard.

    The dashboard (dashboard/app/api/agents/route.ts) has always called
    this exact path and reshaped its response into per-agent cards, but
    the route was never actually registered here -- every dashboard page
    that lists agents (the Agents page, and transitively the Chat page's
    agent picker) has been getting a 500 from its own proxy since
    whenever that dashboard code was written. Found while investigating
    "chat doesn't work as intended" -- the chat page's dropdown never
    populated because this 404'd, so `agent` stayed empty and sends were
    silently no-op'd by the empty-agent guard.

    Shape: a plain object keyed by agent name (not wrapped in {agents:
    ...}), matching route.ts's `Object.entries(status)` call.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    status = {}
    for agent in agent_config:
        proc = agent_processes.get(agent)

        queue_depths: Dict[str, int] = {}
        async with db.execute(
            "SELECT channel, COUNT(*) as count FROM message_queue "
            "WHERE agent = ? AND processed = ? GROUP BY channel",
            (agent, STATUS_QUEUED)
        ) as cursor:
            async for row in cursor:
                queue_depths[row["channel"]] = row["count"]
        total_pending = sum(queue_depths.values())

        async with db.execute(
            "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
            (agent, STATUS_COMPLETE)
        ) as cursor:
            row = await cursor.fetchone()
            messages_processed = row["count"]

        compaction_count = 0
        async with db.execute(
            "SELECT compaction_count FROM sessions WHERE agent = ?", (agent,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                compaction_count = row["compaction_count"]

        # cost_events rows are per-turn deltas except session_total, which
        # is already a running cumulative -- take the latest row's
        # session_total for cost rather than summing (summing
        # session_total would double-count), and sum input_tokens for a
        # cumulative usage figure. Field names below (session_cost,
        # input_tokens flat rather than nested) match what
        # dashboard/app/api/agents/route.ts has always expected from this
        # endpoint -- it was written against this shape, it just never
        # had a real endpoint to call.
        session_cost = None
        input_tokens = None
        async with db.execute(
            "SELECT session_total FROM cost_events WHERE agent = ? "
            "ORDER BY id DESC LIMIT 1", (agent,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                session_cost = row["session_total"]
        async with db.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) as inp "
            "FROM cost_events WHERE agent = ?", (agent,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row["inp"]:
                input_tokens = row["inp"]

        status[agent] = {
            "state": agent_states.get(agent, "UNKNOWN"),
            "subprocess_alive": proc is not None and proc.returncode is None,
            "subprocess_pid": proc.pid if proc is not None else None,
            "session_id": agent_sessions.get(agent, ""),
            "queue_depths": queue_depths,
            "total_pending": total_pending,
            "messages_processed": messages_processed,
            "compaction_count": compaction_count,
            "session_cost": session_cost,
            "input_tokens": input_tokens,
        }

    return web.json_response(status)

async def handle_health(request):
    """GET /health - Health check"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent_status = {}
    for agent in agent_config:
        proc = agent_processes.get(agent)
        queue_depth = 0
        async with db.execute(
            "SELECT COUNT(*) as count FROM message_queue WHERE agent = ? AND processed = ?",
            (agent, STATUS_QUEUED)
        ) as cursor:
            row = await cursor.fetchone()
            queue_depth = row["count"]

        agent_status[agent] = {
            "state": agent_states.get(agent, "UNKNOWN"),
            "alive": proc is not None and proc.returncode is None,
            "queue_depth": queue_depth,
            "session_id": agent_sessions.get(agent, "")[:8]
        }

    return web.json_response({
        "status": "healthy",
        "agents": agent_status,
        # Changes on every real process restart (fresh on import, see
        # SERVER_BOOT_ID above) — unlike session_id, which is deliberately
        # preserved across restarts and so can't tell a caller whether
        # they're still talking to a not-yet-dead old process.
        "boot_id": SERVER_BOOT_ID,
        "pid": os.getpid(),
    })

async def handle_agents(request):
    """GET /agents - List agents"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agents_list = []
    for agent, config in agent_config.items():
        agents_list.append({
            "name": agent,
            "model": config.get("model"),
            # The same defaults the subprocess is actually launched with (see
            # start_agent). Reporting the raw config.get() would show a blank
            # for every agent that relies on the default, which reads as "not
            # configured" rather than "configured by omission". Added
            # 2026-08-08 for the dashboard settings page (see
            # dashboard/app/api/agents/config/route.ts) — /status has none of
            # this, only runtime state, so the settings page needs this
            # endpoint specifically rather than reusing /api/agents.
            "max_turns": config.get("max_turns", 200),
            "timeout": config.get("timeout"),
            "state": agent_states.get(agent, "UNKNOWN"),
            "has_discord_token": agent in AGENT_TOKENS,
            # Anthropic's own live rate-limit signal, added 2026-08-06 —
            # empty until this agent's subprocess has completed at least
            # one turn since agent-server last started (see
            # _record_rate_limit_event / read_agent_response).
            #
            # 2026-09-03 (task-1788454188): this field is now the single
            # "most urgent" window (see _rate_limit_primary()'s selection
            # rule) for backward compatibility with existing consumers
            # (relay.py's /sys status line expects this exact flat shape).
            # rate_limit_windows below carries the full per-window
            # breakdown for anything that wants both bars.
            "rate_limit": _rate_limit_primary(agent) or {},
            "rate_limit_windows": agent_rate_limits.get(agent, {}),
            # Context-window fill estimate, added 2026-08-07 — same caveat
            # as rate_limit: empty until this agent's subprocess has
            # completed at least one turn since agent-server last started.
            "context_usage": agent_context_usage.get(agent, {}),
            # Monthly account spend cap, added 2026-08-18 — see
            # CLI_SPEND_LIMIT_SIGNATURE. Distinct from rate_limit above:
            # that's the five-hour/seven-day window and self-clears on a
            # timer, this is the account-level cap and needs Ian to raise
            # it manually. False until this agent's subprocess has hit it
            # at least once since agent-server last started.
            "spend_limit_blocked": agent_spend_limit_blocked.get(agent, False),
        })

    return web.json_response({"agents": agents_list})

async def handle_agent_reset(request):
    """POST /agents/{name}/reset - Reset agent session"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    await restart_agent(agent)
    return web.json_response({"status": "reset"})


async def handle_agent_reload(request):
    """POST /agents/{name}/reload - Bounce subprocess, preserve session."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    await reload_agent(agent)
    return web.json_response({"status": "reloaded"})


async def handle_agent_compact(request):
    """POST /agents/{name}/compact - Manual trigger for the same
    finalize-then-fresh-session action the automatic compaction triggers
    use (see compact_session() / maybe_compact_session()). Added
    2026-08-10, Ian's ask, prompted by seeing high context utilization
    and wanting to bring it down on demand rather than waiting for the
    token-target/topic-change/rate-limit triggers to fire on their own."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    ok = await compact_session(agent, reason="manual (/compact)")
    if not ok:
        return web.json_response({"status": "failed"}, status=500)
    return web.json_response({"status": "compacted"})


async def handle_agent_interrupt(request):
    """POST /agents/{name}/interrupt - Halt the current in-flight turn via
    the CLI's own stream-json control_request protocol, without killing the
    subprocess or losing the session. See interrupt_agent() for what's
    actually verified to happen. A 200 here means the interrupt request was
    written to stdin, not that a turn was necessarily in progress to
    interrupt — the CLI acks unconditionally (see live capture); an idle
    subprocess has nothing to stop and just no-ops."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    result = await interrupt_agent(agent)
    if not result.get("ok"):
        return web.json_response({"status": "failed", "error": result.get("error")}, status=500)
    return web.json_response({"status": "interrupted", "request_id": result.get("request_id")})


async def handle_rate_limit_override_set(request):
    """POST /agents/{name}/rate-limit-override - Owner-set, auto-expiring
    bypass of is_rate_limit_paused() (2026-08-10). Body:
    {"enabled_by": "...", "duration_sec": 900, "reason": "..."}
    duration_sec is optional (defaults to 15 minutes) and is silently
    capped at RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC — see
    set_rate_limit_override(). enabled_by is required so an override is
    always attributable, same principle as the recovery-agent's
    always-attributed #signals posts."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    data = await request.json() if request.can_read_body else {}
    enabled_by = (data.get("enabled_by") or "").strip()
    if not enabled_by:
        return web.json_response({"error": "enabled_by is required"}, status=400)
    try:
        duration_sec = float(data.get("duration_sec", 900))
    except (TypeError, ValueError):
        return web.json_response({"error": "duration_sec must be a number"}, status=400)
    reason = data.get("reason") or ""

    expires_at = await set_rate_limit_override(agent, enabled_by, duration_sec, reason)
    return web.json_response({
        "status": "override_set",
        "agent": agent,
        "enabled_by": enabled_by,
        "expires_at": expires_at,
        "capped": duration_sec > RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC,
    })


async def handle_rate_limit_override_clear(request):
    """POST /agents/{name}/rate-limit-override/clear - Cancel an active
    override early."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=404)

    existed = await clear_rate_limit_override(agent)
    return web.json_response({"status": "override_cleared" if existed else "no_active_override", "agent": agent})


# Agent name validator — same surface as bin/create-agent.sh's check, used
# to reject path traversal / shell metachars before we touch disk.
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


async def handle_agent_register(request):
    """POST /agents/{name}/register - Hot-load a newly-created agent.

    bin/create-agent.sh writes the new agent into config/agents.json and
    then POSTs here so the running server picks it up without a full
    restart. This endpoint:
      1. re-reads agents.json (and channels.json) via load_config()
      2. confirms the new agent now appears in agent_config
      3. starts its subprocess (the same code path startup() uses)

    Returns 200 once the subprocess is launched, 404 if the new agent
    didn't show up in the reloaded config (typo / wrong file), and 409
    if the agent is already running.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("name")
    if not agent or not _AGENT_NAME_RE.match(agent):
        return web.json_response({"error": "Invalid agent name"}, status=400)

    if agent in agent_processes:
        return web.json_response(
            {"error": "Agent already running", "agent": agent},
            status=409,
        )

    # Re-read agents.json + channels.json so the new entry, its Discord
    # token mapping, and any new channel routing all become visible to
    # the running server.
    await load_config()

    if agent not in agent_config:
        return web.json_response(
            {
                "error": (
                    f"Agent '{agent}' not found in config after reload — "
                    "verify it was written to config/agents.json"
                )
            },
            status=404,
        )

    log.info(f"Hot-registering new agent: {agent}")
    await start_agent_subprocess(agent)

    discord_bound = agent in AGENT_TOKENS
    return web.json_response(
        {
            "status": "registered",
            "agent": agent,
            "discord_bound": discord_bound,
        }
    )


async def handle_cost(request):
    """POST /cost - Record external cost event"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    data = await request.json()
    agent = data.get("agent")
    cost_delta = data.get("cost_delta", 0.0)

    if agent not in agent_config:
        return web.json_response({"error": "Unknown agent"}, status=400)

    # Record cost
    await db.execute(
        "INSERT INTO cost_events (agent, cost_delta, session_total) VALUES (?, ?, ?)",
        (agent, cost_delta, cost_delta)
    )
    await db.commit()

    # Reset last cost (external sessions are independent)
    agent_last_cost[agent] = 0.0

    return web.json_response({"status": "recorded"})

async def handle_cost_get(request):
    """GET /cost/{agent} - Get cost summary"""
    # Check bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agent = request.match_info.get("agent")

    # Daily cost
    async with db.execute(
        """
        SELECT SUM(cost_delta) as total
        FROM cost_events
        WHERE agent = ? AND timestamp > datetime('now', '-1 day')
        """,
        (agent,)
    ) as cursor:
        row = await cursor.fetchone()
        daily = row["total"] or 0.0

    # Monthly cost
    async with db.execute(
        """
        SELECT SUM(cost_delta) as total
        FROM cost_events
        WHERE agent = ? AND timestamp > datetime('now', '-30 days')
        """,
        (agent,)
    ) as cursor:
        row = await cursor.fetchone()
        monthly = row["total"] or 0.0

    return web.json_response({
        "agent": agent,
        "daily": daily,
        "monthly": monthly,
        "session": agent_last_cost.get(agent, 0.0)
    })

async def handle_usage(request):
    """GET /usage - rate-limit headroom for every agent.

    Ported from mcarmody/karakos-package#128. The counterpart to /cost:
    /cost answers "what has this spent", this answers "how close is it
    to being cut off", which is the number that actually stops a turn
    mid-sentence. Reads the same in-memory agent_rate_limits dict
    is_rate_limit_warning()/is_rate_limit_paused() already use — see
    format_usage_report() — rather than a separate table, since every
    field this needs is already tracked there.

    2026-09-03 (task-1788454188): the flat status/rate_limit_type/
    resets_at/utilization fields below now describe only
    _rate_limit_primary()'s pick (kept for backward compatibility with
    existing callers of this endpoint) — `windows` is the new field that
    actually carries every known window at once, which is what let
    format_usage_report's `summary` grow a bar per window instead of
    clobbering down to one.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != AGENT_SERVER_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    agents = {}
    for name in agent_config:
        windows = agent_rate_limits.get(name) or {}
        info = _rate_limit_primary(name)
        progress = rate_limit_window_progress(info) if info else None
        agents[name] = {
            "status": info.get("status") if info else None,
            "rate_limit_type": info.get("rateLimitType") if info else None,
            "resets_at": info.get("resetsAt") if info else None,
            "is_using_overage": bool(info.get("isUsingOverage")) if info else False,
            "overage_status": info.get("overageStatus") if info else None,
            "utilization": info.get("utilization") if info else None,
            # None, never 0 — "no reading yet" and "0% consumed" are
            # opposite answers and must not render as the same number.
            "percent_of_window_used": round(progress * 100, 1) if progress is not None else None,
            "windows": windows,
            "summary": format_usage_report(name),
        }

    return web.json_response({"agents": agents})

# =============================================================================
# Graceful Shutdown
# =============================================================================

async def graceful_shutdown(sig):
    """Handle SIGTERM gracefully"""
    global shutting_down
    log.info(f"Received {sig}, shutting down gracefully...")
    shutting_down = True

    # Stop accepting new messages (set flag checked by handlers)

    # Wait for agents to finish (max 30s)
    log.info("Waiting for agents to finish current messages...")
    for i in range(30):
        all_idle = all(agent_states.get(a) == "IDLE" for a in agent_config)
        if all_idle:
            break
        await asyncio.sleep(1)

    # Generate summaries for active agents
    log.info("Finalizing sessions...")
    for agent in agent_config:
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", str(Path(__file__).parent / "summarize-session.py"), agent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25)
            if proc.returncode == 0:
                log.info(f"Session summary generated for {agent}")
            else:
                log.warning(f"Session summary failed for {agent}: {stderr.decode()[:200]}")
        except asyncio.TimeoutError:
            log.warning(f"Session summary timed out for {agent}")
        except Exception as e:
            log.warning(f"Session summary error for {agent}: {e}")

    # Kill subprocesses
    log.info("Terminating agent subprocesses...")
    for agent in list(agent_processes.keys()):
        await kill_agent_subprocess(agent)

    # Close DB
    if db:
        await db.close()

    # Close HTTP session
    if http_session:
        await http_session.close()

    log.info("Shutdown complete")
    sys.exit(0)

# =============================================================================
# Server Startup
# =============================================================================

async def startup(app):
    """Initialize server on startup"""
    global http_session

    _acquire_singleton_lock("agent-server")
    log.info("Starting Karakos Agent Server")

    # Initialize HTTP session
    http_session = aiohttp.ClientSession()

    # Initialize database
    await init_db()

    # Load configuration
    await load_config()

    # Restore any in-progress rate-limit pause (see
    # _load_rate_limits_from_db() docstring) before anything starts
    # pulling from message_queue, so a restart mid-warning can't
    # silently resume processing.
    await _load_rate_limits_from_db()

    # Restore any still-active rate-limit override (2026-08-10) — see
    # _load_rate_limit_overrides_from_db() docstring.
    await _load_rate_limit_overrides_from_db()

    # Initialize locks and state
    for agent in agent_config:
        agent_locks[agent] = asyncio.Lock()
        agent_states[agent] = "IDLE"
        response_buffers[agent] = ""

    # Crash recovery
    await crash_recovery()

    # Start agent subprocesses
    for agent in agent_config:
        await start_agent_subprocess(agent)

    # Register signal handlers in event loop context
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: _spawn(graceful_shutdown("SIGTERM")))
    loop.add_signal_handler(signal.SIGINT, lambda: _spawn(graceful_shutdown("SIGINT")))

    # Queued-ack sweep (Task #13) — independent of any single agent's
    # turn, see queued_ack_sweep_loop() docstring for why this can't just
    # piggyback on process_agent_queue()'s own pass.
    _spawn(queued_ack_sweep_loop())

    # Rate-limit gate sweep (2026-08-07) — see rate_limit_gate_sweep_loop()
    # docstring: what actually resumes a paused agent once its five-hour
    # window resets.
    _spawn(rate_limit_gate_sweep_loop())

    log.info(f"Agent server ready on port {PORT}")

    # Startup notice (2026-08-06) — Ian, after several restarts tonight:
    # "when you restart you don't come back with a 'hey I did it'
    # message." Correct — a restart was only visible by reading logs or
    # asking. Post one short line to #signals so it's visible without
    # digging. Best-effort: must never block startup if it fails (no
    # signals channel configured, Discord unreachable, etc).
    try:
        signals_channel = (channels_config.get("channels", {}).get("signals", {}) or {}).get("id")
        primary_agent = next(iter(agent_config), None)
        if signals_channel and primary_agent:
            agents_up = ", ".join(agent_config.keys())
            await post_to_discord(
                primary_agent, signals_channel,
                f"-# 🔄 agent-server restarted — {agents_up} back up"
            )
    except Exception as e:
        log.warning(f"Startup notice failed (non-fatal): {e}")

async def shutdown(app):
    """Cleanup on shutdown"""
    log.info("Server shutdown initiated")

    # Kill all subprocesses
    for agent in list(agent_processes.keys()):
        await kill_agent_subprocess(agent)

    # Close HTTP session
    if http_session:
        await http_session.close()

    # Close database
    if db:
        await db.close()

# =============================================================================
# Main
# =============================================================================

def main():
    """Main entry point"""
    # Signal handlers will be registered after event loop starts (in startup)
    # For now, just set flag to handle in asyncio context

    # Create app
    app = web.Application()

    # Register routes
    app.router.add_post("/message", handle_message)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/agents", handle_agents)
    app.router.add_post("/agents/{name}/reset", handle_agent_reset)
    app.router.add_post("/agents/{name}/reload", handle_agent_reload)
    app.router.add_post("/agents/{name}/compact", handle_agent_compact)
    app.router.add_post("/agents/{name}/interrupt", handle_agent_interrupt)
    app.router.add_post("/agents/{name}/register", handle_agent_register)
    app.router.add_post("/agents/{name}/rate-limit-override", handle_rate_limit_override_set)
    app.router.add_post("/agents/{name}/rate-limit-override/clear", handle_rate_limit_override_clear)
    app.router.add_post("/cost", handle_cost)
    app.router.add_get("/cost/{agent}", handle_cost_get)
    app.router.add_get("/usage", handle_usage)

    # Register startup/shutdown handlers
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)

    # Run server
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)

if __name__ == "__main__":
    main()
