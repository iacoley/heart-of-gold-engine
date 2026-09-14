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
instead of once per check. It does not and cannot fix rotation itself
— that needs a real interactive re-login (see Amos's headless
device-flow relay pattern for that part).

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

_DEFAULT_STATE = {"known_bad": False, "last_probe_ts": 0.0, "alerted": False}


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
    _write_state(state)
    if should_alert:
        log.error(
            "auth_guard: sidecar claude spawn hit the OAuth-rotation "
            "signature — on-disk credentials need a real interactive "
            "re-login. Suppressing repeat alerts for this outage; "
            "further failures logged at WARNING until recovery."
        )
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
