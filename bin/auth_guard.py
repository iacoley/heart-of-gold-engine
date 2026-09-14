#!/usr/bin/env python3
"""
auth_guard.py — shared detect-and-survive guard for the OAuth-rotation
scar confirmed 2026-09-14 (see memory fact + #agent-chat with Amos,
subject "shared-oauth-token-rotation").

Root cause (not fixable from here): every long-lived `claude` session
plus every short-lived `claude -p` sidecar spawn (voice_presence.py's
judge, agent-server.py's classify_topic_change, memory-patterns.py,
memory-maintenance.py, summarize-session.py, relay.py) reads/refreshes
the same on-disk ~/.claude/.credentials.json. Refresh tokens rotate
server-side, so whichever holder refreshes first invalidates every
sibling's copy. A short-lived spawn that hits this fails with:

    Failed to authenticate: OAuth session expired and could not be refreshed

Confirmed identical to a 2026-07-19 incident on Amos's side
(projects/pty-supervisor/supervisor.py, AuthGuard). This module ports
his detect-and-survive shape: once a spawn hits the signature, stop
hammering (don't let every sidecar independently retry into a dead
token), re-probe on a cooldown instead, and log the transition once
instead of once per check.

That alone only survives the outage, it doesn't end it — someone still
had to notice and run a manual re-login. 2026-09-14: Amos (via
mcarmody2013@gmail.com) handed over the other half he'd already built
for the identical scar: kick off `claude auth login --claudeai`
detached, capture the short-lived sign-in link from its first few
seconds of stdout, and relay it out over Discord before the process's
own polling loop blocks. See trigger_relogin_relay() below — wired into
record_failure() so it fires automatically on the first transition into
an outage, once per outage, no manual intervention required.

State is a small JSON file under WORKSPACE_ROOT/data so it coordinates
across separate processes (agent-server.py's event loop is not the same
process as memory-maintenance.py's cron dispatch), guarded by flock for
concurrent read-modify-write safety.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("auth_guard")

AUTH_FAILURE_SIGNATURE = "OAuth session expired and could not be refreshed"

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/opt/karakos"))
STATE_PATH = WORKSPACE_ROOT / "data" / "auth-guard-state.json"

# Matches Amos's re-probe cadence — frequent enough to notice recovery
# promptly, infrequent enough not to just recreate the hammering this
# exists to stop.
PROBE_COOLDOWN_SEC = 120

# Amos's recipe, verbatim: the link/device code prints almost
# immediately, then the process blocks polling for browser completion
# in the background. 8s is his tested wait, long enough for the link to
# land in the output file, short enough not to hold up whichever
# sidecar caller hit the failure and is now blocking in here.
RELOGIN_WAIT_SEC = 8
RELOGIN_OUTPUT_PATH = WORKSPACE_ROOT / "data" / "auth-guard-relogin-out.txt"
RELOGIN_CHANNEL = os.environ.get("AUTH_GUARD_RELOGIN_CHANNEL", "general")
NOTIFY_SCRIPT = WORKSPACE_ROOT / "bin" / "discord-notify.sh"
OWNER_DISCORD_ID = os.environ.get("OWNER_DISCORD_ID", "0")

_DEFAULT_STATE = {
    "known_bad": False,
    "last_probe_ts": 0.0,
    "alerted": False,
    "relogin_triggered": False,
}


def trigger_relogin_relay() -> bool:
    """Kick off a headless device-flow re-login and relay the sign-in
    link to Discord so a human can close it out without anyone having
    to notice the outage and run this by hand.

    Detached (start_new_session) so it survives this process/turn
    ending — the login itself keeps polling for browser completion in
    the background long after this function returns. discord-notify.sh
    is used rather than the MCP discord tool because it's a direct
    bot-token curl call, independent of the very credentials that just
    died, so it still works precisely when this is needed.

    Returns True only if the link was both captured and relayed. Best
    effort throughout: every failure mode here (spawn failed, nothing
    captured, Discord post failed) is logged and swallowed rather than
    raised, since the caller is record_failure() and a relogin hiccup
    must never break the core known_bad bookkeeping it guarantees.
    """
    RELOGIN_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(RELOGIN_OUTPUT_PATH, "w") as out:
            subprocess.Popen(
                ["claude", "auth", "login", "--claudeai"],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as e:
        log.error("auth_guard: failed to spawn headless relogin: %s", e)
        return False

    time.sleep(RELOGIN_WAIT_SEC)

    try:
        captured = RELOGIN_OUTPUT_PATH.read_text().strip()
    except OSError as e:
        log.error("auth_guard: relogin spawned but output unreadable: %s", e)
        return False

    if not captured:
        log.error("auth_guard: relogin spawned but captured no output to relay")
        return False

    message = (
        f"<@{OWNER_DISCORD_ID}> shared Claude credentials died again "
        "(OAuth-rotation scar, see auth_guard.py). Headless re-login "
        "kicked off automatically — the link below is short-lived "
        "(minutes, not hours), click it now:\n```\n" + captured + "\n```"
    )

    try:
        subprocess.run(
            [str(NOTIFY_SCRIPT), RELOGIN_CHANNEL, message],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log.error("auth_guard: relogin link captured but relay to Discord failed: %s", e)
        return False

    return True


def is_auth_failure_signature(text: Optional[str]) -> bool:
    """True if `text` contains the known CLI auth-failure fingerprint.
    Deliberately a substring match, not an exact-equality check — the
    same signature shows up embedded in differently-shaped payloads
    (flat `result` strings, judge verdicts, classifier answers)."""
    return bool(text) and AUTH_FAILURE_SIGNATURE in text


def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return {**_DEFAULT_STATE, **json.load(f)}
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return dict(_DEFAULT_STATE)


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Open for read+write-create so the flock below covers the whole
    # read-modify-write from every caller's perspective, not just this
    # process's own write.
    with open(STATE_PATH, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            json.dump(state, f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def should_attempt() -> bool:
    """Call before spawning a sidecar `claude -p` process. False means
    auth is known-bad and still within its cooldown window — skip the
    spawn rather than send another process to die the same way. True
    means either auth looked fine last we checked, or the cooldown has
    elapsed and it's worth trying again (a sibling may have already
    fixed it with a fresh login)."""
    state = _read_state()
    if not state["known_bad"]:
        return True
    return (time.time() - state["last_probe_ts"]) >= PROBE_COOLDOWN_SEC


def record_failure() -> bool:
    """Call when a spawn's output actually matched the signature.
    Returns True the first time (the bad->worse transition — caller
    should log/alert loudly), False on every subsequent call while
    still within the same outage (caller should stay quiet, this is
    exactly the hammering the guard exists to stop)."""
    state = _read_state()
    first = not state["known_bad"]
    state["known_bad"] = True
    state["last_probe_ts"] = time.time()
    should_alert = first or not state["alerted"]
    state["alerted"] = True
    # Claim the relogin trigger in the same write as everything else so
    # two near-simultaneous callers don't both spawn a device-flow login
    # (the second would just orphan the first's link, not help anyone).
    # Still a check-then-act race in principle across the two _write_state
    # calls above and below, same as the "first" transition check itself
    # already accepts — narrow window, worst case is a duplicate relogin
    # attempt, not a missed one.
    should_relogin = not state["relogin_triggered"]
    state["relogin_triggered"] = True
    _write_state(state)
    if should_alert:
        log.error(
            "auth_guard: sidecar claude spawn hit the OAuth-rotation "
            "signature — on-disk credentials need a real interactive "
            "re-login. Suppressing repeat alerts for this outage; "
            "further failures logged at WARNING until recovery."
        )
    if should_relogin:
        try:
            trigger_relogin_relay()
        except Exception:
            # Bookkeeping above already landed regardless of what
            # happens here — a relogin hiccup must never take down the
            # one contract every caller actually depends on.
            log.exception("auth_guard: trigger_relogin_relay() raised")
    return should_alert


def record_success() -> None:
    """Call when a spawn completed without hitting the signature.
    Clears known-bad so the next should_attempt() doesn't wait out a
    cooldown that's no longer relevant, and lets a future failure
    alert again as a fresh transition."""
    state = _read_state()
    if state["known_bad"]:
        log.warning("auth_guard: recovered — sidecar claude spawns authenticating again.")
    if state["known_bad"] or state["alerted"]:
        _write_state(dict(_DEFAULT_STATE))
