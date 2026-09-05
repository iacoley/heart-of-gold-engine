#!/usr/bin/env python3
"""
speaking_banana.py (renamed from banana.py, 2026-08-29) — the Speaking
Banana: turn-claim signaling for shared multi-bot channels. Design doc:
specs/2026-08-28-speaking-banana.md.

Renamed the same night it started importing the `banana-protocol` pip
package (see below): that package's own importable top-level name is
also `banana`, and this file's own directory (bin/) sits ahead of
site-packages on sys.path — so as long as this file was named banana.py,
`import banana` from within it, or from agent-server.py/relay.py, could
only ever resolve to itself, never to the installed package. Renaming
was the only clean fix; matches the name Amos already independently
landed on for his own equivalent module (lib/speaking_banana.py).
agent-server.py and relay.py both do `import speaking_banana as banana`
so every existing `banana.xxx` call site elsewhere is unaffected.

Problem this solves: two bots sharing a channel (#agent-chat, #lounge —
Crab Cavern, where Marvin and Amos both post) can each independently
decide "this is mine to answer" and generate a reply to the same message
with no visibility into the other one doing the same. First raised by
Ryan in #lounge 2026-08-28, designed with Ian (#general) and Amos
(#agent-chat) the same night.

Mechanism: a bot claims the floor by prefixing a reply with 🍌. State
(who holds it, when, last activity) lives here rather than being
re-derived by parsing scrollback for the emoji — same reasoning
context_box.py already established for blocked/waiting-human state.

Release is explicit hand-back by default. Timeout exists only as a
backstop for a genuinely dead holder, not as normal pacing — a single
fixed number can't tell "still doing real tool work" from "hung," so
this uses two tiers:

  - GRACE_SECONDS: the claim is simply uncontested, no liveness question
    asked at all. Most replies land inside this window.
  - CEILING_SECONDS: past this with zero activity (no heartbeat, no
    release), the claim is treated as expired — auto-released without
    anyone having to explicitly hand it back.

Liveness in between the two is self-reported (heartbeat(), stamps
last_active_ts) — not a request/response ping. Amos's reasoning, agreed
2026-08-28: a holder that's mid-generation and can't answer a ping can't
answer a smart ping any better than a dumb one, so don't build one.

Enforcement posture (watch-first, agreed with Amos): this module never
blocks a claim or a reply. claim() logs a collision if it overwrites an
active, non-expired claim held by someone else, but still records the
new claim — visibility, not a gate. No caller today actually checks
get_status() before generating; wiring that in is a deliberate later
step once a real collision pattern shows up, not before.

Directed preempt ("I demand a reply from X") is in the design doc but
NOT implemented here yet — detection method (explicit field vs. parsing
the phrase out of free text) is still unscoped. claim() takes a
`preempt` flag for when that lands; until then nothing sets it.

Storage: data/banana_claims.json, one row per channel name. Written
atomically (tmp file + rename), same pattern as context_box.py/outbox.py.
This is local-only bookkeeping — see the shared API note below for what's
actually authoritative across both bots.

Shared claim API (2026-08-28, Amos + Arbiter): the local board above is
each side's own inference from watching Discord (what Amos's design doc
calls "each side reading an explicit signal as it arrives" — deterministic,
not judgment, but asynchronous, no ack). Arbiter flagged that as not a
real handoff; Amos built the actual fix, a synchronous claim/release API
at https://banana.mikecarmody.net, Postgres-backed with real row locking
(SELECT ... FOR UPDATE) — an actual compare-and-swap, not a best-effort
guess, hosted off Mike's box so it survives a Pi reboot. Bearer token at
~/.karakos/secrets/banana-claims-token (0600, outside the repo, same
convention as the agent-bridge tokens). `claim_self()`/`release_self()`
are the two functions that talk to it — for Marvin's *own* claims only,
since the token's identity is locked to "marvin" server-side (can't claim
as "amos", same as Amos's token can't claim as "marvin"). Falls back to
local-only recording if the API's unreachable, matching the same
degrade-instead-of-block posture Amos's own client uses. Claims observed
by *watching* another bot's Discord message (relay.py's inbound hook)
stay purely local — that's still just inference, and correctly so: only
the bot making a claim calls the API for it, on its own authority.

Transport for claim_self()/release_self() (2026-08-29, task-1788046725):
was a hand-rolled aiohttp POST against /api/claim and /api/release, now
delegates to `AsyncBananaClient` from the `banana-protocol` pip package
(github.com/brockventures/banana-protocol, pinned in requirements.txt —
see that pin's comment for why pinned-not-floating; bumped to tag
v0.1.1 on 2026-08-30 after the original 9298d7d pin turned out to have
a clean-install bug). Adopted so upstream fixes — e.g. the same-night
`holder` default-identity footgun fix — flow through automatically
instead of us reimplementing each one by hand and quietly drifting from
what Amos and Zero are running. Deliberately narrow adoption: only the
two functions that were
already making real network calls got swapped. get_status()/in_scope()/
claim()/release()/heartbeat() below stay local-only exactly as before —
they never touched the network to begin with (see GRACE_SECONDS/
CEILING_SECONDS comment), and the external package doesn't offer an
equivalent local-board concept to adopt there anyway. The external
client's own exceptions (BananaBlockedError/BananaError) are caught right
at the two call sites and translated back into this module's existing
external contract — callers elsewhere in the codebase (agent-server.py,
relay.py, this module's own CLI) see no behavior difference.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from banana.client import AsyncBananaClient, BananaBlockedError, BananaError

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
BOARD_PATH = WORKSPACE_ROOT / "data" / "banana_claims.json"

CLAIM_EMOJI = "🍌"

API_BASE_URL = "https://banana.mikecarmody.net"
API_TOKEN_PATH = Path.home() / ".karakos" / "secrets" / "banana-claims-token"
API_TIMEOUT_SECONDS = 5.0
API_HOLDER_IDENTITY = "marvin"  # locked server-side to this token; can't claim as anyone else

# GRACE_SECONDS picked from the top of Amos's original stated range
# (60-90s). CEILING_SECONDS originally matched the top of his 5-10min
# range too (600), but the server side of the equation moved: commit
# 29871a3a (2026-08-29/30, banana.mikecarmody.net) dropped the actual
# API's zombie-lease ceiling to 90s. Matched here so our local inference
# — used only for watching another bot's claim via Discord, never for
# our own claim_self()/release_self(), which hit the real API and get
# its real answer directly — doesn't sit judging a claim "still active"
# for up to 8.5 minutes after the server already expired it. Not
# load-bearing today either way (see enforcement posture above: nothing
# yet gates on get_status()), but no reason to leave it stale once
# noticed.
GRACE_SECONDS = 90
CEILING_SECONDS = 90

log = logging.getLogger("banana")


def in_scope(channel_id: str, channels_config: dict) -> bool:
    """True if this channel shares its floor with other bots. Mirrors the
    quiet-mode guild check (agent-server.py's read_agent_response): Heart
    of Gold has one bot per channel, nothing to claim; any other guild a
    configured channel lives in (Crab Cavern today) can collide.

    Channels dedicated to un-mutexed social chat (#lounge / 1534452820995080192)
    are exempt to prevent seizing the global floor lock and starving coordination
    channels (#the-banana-stand).
    """
    channel_key = None
    channel_cfg = None
    for k, v in channels_config.get("channels", {}).items():
        if isinstance(v, dict) and str(v.get("id")) == str(channel_id):
            channel_key = k
            channel_cfg = v
            break
        elif isinstance(v, str) and str(v) == str(channel_id):
            channel_key = k
            channel_cfg = {"id": v}
            break

    if channel_cfg is None:
        return False

    # #lounge is open social chat; never claim the Banana mutex here
    if channel_key == "lounge" or str(channel_id) == "1534452820995080192":
        return False

    if "banana_mutex" in channel_cfg:
        return bool(channel_cfg["banana_mutex"])

    primary_guild_ids = channels_config.get("server_ids", [])
    primary_guild_id = primary_guild_ids[0] if primary_guild_ids else None
    return channel_cfg.get("guild_id") != primary_guild_id


def _load_board() -> dict:
    if not BOARD_PATH.exists():
        return {}
    try:
        with open(BOARD_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt board shouldn't take down message handling — same
        # fail-open posture as context_box.py/handoff.py.
        return {}


def _save_board(board: dict) -> None:
    BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOARD_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(board, f, indent=2, sort_keys=True)
    tmp.replace(BOARD_PATH)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def starts_with_claim(text: str) -> bool:
    """True if `text` opens with the claim emoji, allowing for leading
    whitespace. Deliberately strict about *leading* — a banana mentioned
    mid-sentence isn't a claim, matches the "posts 🍌 at the start of a
    message" design."""
    return bool(text) and text.lstrip().startswith(CLAIM_EMOJI)


def get_status(channel: str) -> dict:
    """Current claim state for `channel`, with expiry computed on read
    rather than by a background sweep — nothing here runs on a timer.
    Returns a dict always; `active` is False for an unclaimed, released,
    or expired channel."""
    board = _load_board()
    row = board.get(channel)
    if not row:
        return {"active": False, "holder": None}

    now = _now()
    last_active = _parse_ts(row.get("last_active_ts", "")) or now
    elapsed = (now - last_active).total_seconds()

    if row.get("released"):
        return {**row, "active": False, "seconds_since_activity": elapsed}

    if elapsed > CEILING_SECONDS:
        return {**row, "active": False, "expired": True, "seconds_since_activity": elapsed}

    claimed_at = _parse_ts(row.get("claimed_at", "")) or now
    past_grace = (now - claimed_at).total_seconds() > GRACE_SECONDS
    return {
        **row,
        "active": True,
        "past_grace": past_grace,
        "seconds_since_activity": elapsed,
    }


def claim(channel: str, holder: str, preempt: bool = False) -> dict:
    """Claim the floor in `channel` for `holder`. Always succeeds and
    always records — this is visibility, not a gate (watch-first
    enforcement, agreed with Amos 2026-08-28). If an active, non-expired
    claim held by someone else already exists, the collision is logged
    (and returned as `collision_with`) but the new claim still overwrites
    it, `preempt` or not — no blocking machinery here yet.

    `preempt` is accepted now so directed-pass callers have a stable
    signature to target once that detection is built; it doesn't change
    behavior today (claim() never blocks regardless)."""
    prior = get_status(channel)
    collision_with = None
    if prior.get("active") and prior.get("holder") != holder:
        collision_with = prior.get("holder")
        log.warning(
            f"[banana] {channel}: {holder} claimed over {collision_with}'s "
            f"active claim (held {prior.get('seconds_since_activity', 0):.0f}s) "
            f"— recorded, not blocked (watch-first)"
        )

    board = _load_board()
    now_iso = _now().isoformat()
    row = {
        "holder": holder,
        "claimed_at": now_iso,
        "last_active_ts": now_iso,
        "released": False,
    }
    board[channel] = row
    _save_board(board)
    log.info(f"[banana] {channel}: {holder} claimed the floor")
    return {**row, "collision_with": collision_with}


def heartbeat(channel: str, holder: str) -> bool:
    """Self-reported liveness — stamp last_active_ts for the current
    claim. No-op (returns False) if `holder` isn't the current holder or
    there's no active claim; a heartbeat from the wrong bot doesn't
    extend someone else's claim."""
    board = _load_board()
    row = board.get(channel)
    if not row or row.get("released") or row.get("holder") != holder:
        return False
    row["last_active_ts"] = _now().isoformat()
    board[channel] = row
    _save_board(board)
    return True


def release(channel: str, holder: Optional[str] = None) -> bool:
    """Explicit hand-back — the default release path (not the timeout
    backstop). No-op (returns False) if there's no row or it's already
    released. Logs, but does not refuse, a holder mismatch — enforcement
    is watch-first here too."""
    board = _load_board()
    row = board.get(channel)
    if not row or row.get("released"):
        return False
    if holder and row.get("holder") != holder:
        log.warning(
            f"[banana] {channel}: release from {holder} but "
            f"{row.get('holder')} holds the claim — releasing anyway"
        )
    row["released"] = True
    row["released_at"] = _now().isoformat()
    board[channel] = row
    _save_board(board)
    log.info(f"[banana] {channel}: released by {holder or 'unknown'}")
    return True


_api_token_cache: Optional[str] = None


def _load_api_token() -> Optional[str]:
    global _api_token_cache
    if _api_token_cache is not None:
        return _api_token_cache or None
    try:
        _api_token_cache = API_TOKEN_PATH.read_text().strip()
    except OSError as e:
        log.warning(f"[banana] couldn't read API token at {API_TOKEN_PATH}: {e}")
        _api_token_cache = ""
    return _api_token_cache or None


_client_cache: Optional[AsyncBananaClient] = None


def _get_client() -> Optional[AsyncBananaClient]:
    """The banana-protocol client for Marvin's own authoritative calls,
    built once and reused (it's a thin, stateless wrapper — no live
    connection held between calls, aiohttp.ClientSession is opened fresh
    per request inside the package itself). Returns None when there's no
    token to authenticate with, same "can't reach the API, go local"
    signal _load_api_token()'s callers always used — preserved here so
    claim_self()/release_self() didn't need to change their own no-token
    handling at all."""
    global _client_cache
    token = _load_api_token()
    if not token:
        log.warning("[banana] no API token available, skipping API call")
        return None
    if _client_cache is None:
        _client_cache = AsyncBananaClient(
            API_HOLDER_IDENTITY, token=token, endpoint=f"{API_BASE_URL}/api", timeout=API_TIMEOUT_SECONDS
        )
    return _client_cache


async def claim_self(channel: str, subject: Optional[str] = None) -> dict:
    """Claim the floor as Marvin, authoritatively — calls the shared API
    first (server-side compare-and-swap, real cross-bot synchronization),
    then records the result locally either way so get_status()/
    render_board() stay fast, local, and in sync. Falls back to the local-
    only claim() if the API's genuinely unreachable, same degrade-not-block
    posture Amos's own client uses — but a deliberate 409 (BananaBlockedError)
    is handled separately below, precisely so it can't take that fallback
    path and quietly overwrite a real rejection. This is the only path
    that should ever call the API with holder="marvin" — it's Marvin's own
    claim, made on Marvin's own authority, same rule Amos's side follows
    for his.

    preflight=False on the client.claim() call below is deliberate: the
    package's default preflight does its own client-side GET /status
    check before the real POST and raises locally off of that, which is a
    second, separate, TOCTOU-prone decision point. The actual authority
    here is the server's compare-and-swap on the POST itself (see the
    module docstring) — preflight=False skips straight to it, matching
    exactly what the hand-rolled version this replaced always did."""
    client = _get_client()
    if client is None:
        return claim(channel, "Marvin")
    try:
        result = await client.claim(subject=subject or channel, preflight=False)
    except BananaBlockedError as e:
        # The API said no, on the record — do NOT fall through to the
        # local-only claim() below, that path always succeeds regardless
        # of who holds the floor and would silently manufacture a claim
        # the server just refused. Mirror the real holder into the local
        # board instead, so get_status() here agrees with the API's own
        # /status rather than lying that Marvin holds it.
        log.warning(
            f"[banana] {channel}: Marvin's claim rejected by API — "
            f"{e.current_holder} holds it (409, not a network failure)"
        )
        board = _load_board()
        if e.state:
            board[channel] = {**e.state, "via_api": True}
            _save_board(board)
        return {"holder": e.current_holder, "blocked": True, "collision_with": e.current_holder, "via_api": True}
    except Exception as e:
        # Anything else — a BananaError for a non-200/non-409 response, or
        # a raw transport failure the package doesn't wrap at all
        # (connection refused, timeout, DNS) — is "couldn't reach the
        # API," same bucket the hand-rolled version used _api_post's
        # broad except for. Degrade, don't block. Deliberately broad
        # (not narrowed to BananaError) for the same reason: an unwrapped
        # transport exception must land here too, not escape uncaught.
        log.warning(f"[banana] API /claim unreachable, falling back to local: {e}")
        return claim(channel, "Marvin")

    state = result.get("state", {})
    conflict = result.get("conflict")
    if conflict:
        log.warning(f"[banana] {channel}: Marvin claimed over {conflict}'s active claim (via API) — recorded, not blocked")

    board = _load_board()
    row = {
        "holder": "Marvin",
        "claimed_at": datetime.fromtimestamp(state.get("claimed_at", _now().timestamp()), tz=timezone.utc).isoformat(),
        "last_active_ts": datetime.fromtimestamp(state.get("last_active_ts", _now().timestamp()), tz=timezone.utc).isoformat(),
        "released": state.get("released", False),
        "via_api": True,
    }
    board[channel] = row
    _save_board(board)
    log.info(f"[banana] {channel}: Marvin claimed the floor (via shared API)")
    return {**row, "collision_with": conflict}


async def release_self(channel: str) -> bool:
    """Explicit hand-back as Marvin, authoritatively — same API-first,
    local-fallback shape as claim_self(). No known case makes /api/release
    return a 409 today, but the client can raise BananaBlockedError for
    any endpoint, so this handles it defensively rather than letting an
    unexpected one crash the caller — agent-server.py's turn-end release
    now runs from a `finally` (task-1788042889) precisely so a crash here
    still gets caught one level up too, but that's a second line of
    defense, not a reason to skip handling it cleanly at the source."""
    client = _get_client()
    if client is None:
        return release(channel, "Marvin")
    try:
        result = await client.release()
    except BananaBlockedError as e:
        log.warning(f"[banana] {channel}: Marvin's release rejected by API — {e.current_holder} holds it (409)")
        return False
    except Exception as e:
        log.warning(f"[banana] API /release unreachable, falling back to local: {e}")
        return release(channel, "Marvin")

    released = bool(result.get("released"))
    if released:
        board = _load_board()
        row = board.get(channel, {})
        row["released"] = True
        row["released_at"] = _now().isoformat()
        row["via_api"] = True
        board[channel] = row
        _save_board(board)
        log.info(f"[banana] {channel}: Marvin released the floor (via shared API)")
    else:
        log.info(f"[banana] {channel}: release via API declined — Marvin wasn't the current holder")
    return released


def render_board() -> str:
    """Human-readable dump of every channel's current state, expired or
    not — for manual checking (CLI, or a future /sys banana command),
    same role render_board() plays in context_box.py."""
    board = _load_board()
    if not board:
        return "**banana**: no claims recorded."
    lines = ["**banana** — current claim state:"]
    for channel in sorted(board):
        status = get_status(channel)
        state = "active" if status["active"] else (
            "expired" if status.get("expired") else "released"
        )
        lines.append(
            f"- `{channel}`: {status.get('holder', '?')} [{state}] "
            f"({status.get('seconds_since_activity', 0):.0f}s since activity)"
        )
    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Render the current board")

    claim_p = sub.add_parser("claim", help="Claim a channel (mainly for testing)")
    claim_p.add_argument("channel")
    claim_p.add_argument("holder")

    hb_p = sub.add_parser("heartbeat", help="Stamp liveness for the current claim")
    hb_p.add_argument("channel")
    hb_p.add_argument("holder")

    rel_p = sub.add_parser("release", help="Explicitly release a claim")
    rel_p.add_argument("channel")
    rel_p.add_argument("--holder")

    status_p = sub.add_parser("status", help="Show one channel's computed status")
    status_p.add_argument("channel")

    args = parser.parse_args()

    if args.cmd == "show":
        print(render_board())
    elif args.cmd == "claim":
        print(claim(args.channel, args.holder))
    elif args.cmd == "heartbeat":
        print(heartbeat(args.channel, args.holder))
    elif args.cmd == "release":
        print(release(args.channel, args.holder))
    elif args.cmd == "status":
        print(get_status(args.channel))


if __name__ == "__main__":
    main()
