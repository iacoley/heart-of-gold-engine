#!/usr/bin/env python3
"""channel-name-check.py — keep config/channels.json's cached Discord
display name fresh, and flag it loudly when it drifts.

Incident (2026-09-29): the channel this repo has always called
`agent-chat` internally was renamed on Discord's side to
`the-banana-stand` at some earlier point. Nothing here ever knew — the
only way anyone found out was Ian saying so, twice, because the first
time didn't visibly stick anywhere durable. Separately, `bin/handoff.py`'s
`VALID_MIRROR_CHANNELS` and `skills/outbox/scripts/queue_outbox_message.py`'s
`VALID_CHANNELS` had already drifted from *each other* (one had picked up
`the-banana-stand` as an extra hand-typed entry, the other hadn't) — the
exact hand-sync failure both files' own comments warned about.

Important distinction this script exists to make concrete: our internal
channel *key* (`"agent-chat"` in channels.json) is our own stable label,
not a mirror of Discord's mutable display name. Routing, comparisons, and
posting already go by `channels.json`'s `id` field (a real Discord
snowflake) everywhere that matters — the internal key never needs to
match what the channel is currently called on Discord. What was actually
missing was any record of what the *live* name currently is, so a rename
could be noticed instead of discovered by accident.

What this does:
  - For every channel in config/channels.json with an `id`, queries
    Discord's API for its current name.
  - Compares it to that channel's cached `discord_name` field (added by
    this script; absent on first run).
  - Updates the cache in place (self-healing — the record here is never
    stale for long, whatever health-monitor's poll interval is) and
    returns a message for every channel whose live name didn't match what
    was cached, so a rename gets surfaced instead of silently absorbed.

Read-only against Discord; the only write is channels.json's own
`discord_name` cache fields.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"
AGENTS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "agents.json"
API_BASE = "https://discord.com/api/v10"
USER_AGENT = "karakos-channel-name-check (https://iancoley.org, 0.1)"
REQUEST_TIMEOUT = 15


def load_bot_token() -> str | None:
    """Same lookup discord-read.py uses: first agent in agents.json with a
    resolvable discord_bot_token_env. Returns None (not a hard error) so
    callers can skip the check gracefully when no token is configured,
    rather than this script being the reason a headless run crashes."""
    try:
        cfg = json.loads(AGENTS_CONFIG_PATH.read_text())
    except Exception:
        return None
    for info in cfg.get("agents", {}).values():
        env_var = info.get("discord_bot_token_env", "")
        if env_var and os.environ.get(env_var):
            return os.environ[env_var]
    return None


def fetch_channel_name(channel_id: str, token: str) -> str:
    """GET /channels/{id} and return its current `name`. Raises on any
    failure (network, 4xx/5xx, malformed body) -- callers decide whether
    one channel's failure should stop the whole run."""
    url = f"{API_BASE}/channels/{channel_id}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = 1.0
            try:
                retry_after = float(json.loads(e.read()).get("retry_after", 1.0))
            except Exception:
                pass
            time.sleep(retry_after + 0.1)
            return fetch_channel_name(channel_id, token)
        raise RuntimeError(f"HTTP {e.code} fetching channel {channel_id}: {e.reason}")
    name = body.get("name")
    if not name:
        raise RuntimeError(f"channel {channel_id} response had no 'name' field")
    return name


def check_channel_names(token: str | None = None) -> tuple[bool, str]:
    """Refresh and compare every configured channel's live Discord name.

    Returns (True, "") when every channel's live name matches what's
    cached (including first-run: nothing to compare against yet is not a
    drift). Returns (False, message) listing every channel whose live
    name differs from the cache -- the cache is updated regardless, so
    this only fires once per actual rename, not every run after.
    """
    if token is None:
        token = load_bot_token()
    if not token:
        return True, ""  # no token configured: not this check's problem to raise

    try:
        cfg = json.loads(CHANNELS_CONFIG_PATH.read_text())
    except Exception as e:
        return False, f"channel name check: could not read channels.json: {e}"

    channels = cfg.get("channels", {})
    drifted = []
    changed = False

    for key, entry in channels.items():
        channel_id = entry.get("id")
        if not channel_id:
            continue
        try:
            live_name = fetch_channel_name(channel_id, token)
        except Exception as e:
            drifted.append(f"#{key} ({channel_id}): could not verify live name: {e}")
            continue

        cached_name = entry.get("discord_name")
        if cached_name is not None and cached_name != live_name:
            drifted.append(
                f"#{key} ({channel_id}): cached name {cached_name!r} -> "
                f"actual Discord name is now {live_name!r}"
            )
        if cached_name != live_name:
            entry["discord_name"] = live_name
            changed = True

    if changed:
        try:
            CHANNELS_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
        except Exception as e:
            drifted.append(f"channel name check: could not write updated cache: {e}")

    if drifted:
        return False, "; ".join(drifted)
    return True, ""


def main():
    ok, message = check_channel_names()
    if ok:
        print("All configured channel names match their cached Discord name.")
        sys.exit(0)
    print(f"Channel name drift detected: {message}")
    sys.exit(1)


if __name__ == "__main__":
    main()
