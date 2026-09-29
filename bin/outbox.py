#!/usr/bin/env python3
"""
outbox.py — Durable queue for messages Marvin needs to post to a Discord
channel outside the channel the current turn is scoped to.

Problem this solves (2026-08-08): every turn is pre-scoped to one channel
by a routing header ("this turn posts ONLY to #general"). When something
needs to go to a *different* channel, the only options were:

  (a) a raw curl POST via the bot token mid-turn (discord-notify.sh called
      directly) — worked most nights, failed with a synthetic 403/40333 at
      least once (see agents/Marvin/memory/facts/discord-api-crosspost-
      blocked.md), and even when it works it's a side-channel bypass of
      the normal relay path rather than a supported action; or
  (b) just remembering to say it the next time a turn happens to be
      scoped to the right channel — which depends on Ian noticing and
      prompting, since nothing survives between turns to remind Marvin.

This module turns "I owe #general a message" into a durable row instead
of a mental note that evaporates at end-of-turn. `add_pending()` (or the
`add` CLI) appends a row to data/outbox/pending.jsonl; scheduler.py calls
`flush_pending()` on a short interval to actually deliver it, using the
same bot-token REST call discord-notify.sh always used — this module
doesn't replace that delivery mechanism, it just makes calling it durable
and scheduled instead of ad hoc and turn-dependent.

Storage: one JSON object per line —
    {id, channel, content, attachments, created_at, delivered_at}
Delivered rows are kept (not deleted) with delivered_at set, for audit;
flush only acts on rows where delivered_at is still null.

Attachments (2026-08-24): `attachments` is a list of absolute local file
paths, resolved at add-time (not delivery-time) so a later change of cwd
can't silently break the reference. discord-notify.sh does the actual
upload; if a path has gone missing by the time flush runs, that surfaces
as an ordinary delivery failure (row stays pending, error goes to
stderr) rather than a silent text-only fallback.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import context_box
from handoff import parse_handoff
from pass_filter import is_pass_sentinel

log = logging.getLogger("outbox")

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
OUTBOX_PATH = WORKSPACE_ROOT / "data" / "outbox" / "pending.jsonl"
NOTIFY_SCRIPT = WORKSPACE_ROOT / "bin" / "discord-notify.sh"
CHANNELS_CONFIG_PATH = WORKSPACE_ROOT / "config" / "channels.json"

MAX_DISCORD_MSG_LEN = 2000


def _split_discord_message(text: str, max_length: int = MAX_DISCORD_MSG_LEN) -> list[str]:
    """Split text into chunks Discord will accept (max 2000 chars each).

    Duplicated from agent-server.py's split_discord_message() rather than
    imported — agent-server.py already does `from outbox import
    add_pending`, so the reverse import would be circular. Keep this in
    sync with that copy if the splitting logic ever changes.

    Found 2026-08-29 via a heartbeat sweep: this path (flush_pending() ->
    discord-notify.sh -> curl) never had the 2000-char guard agent-server's
    own post_to_discord() got, so anything queued over the limit failed
    every delivery attempt with curl exit 22 (Discord 400s on oversize
    content) forever, silently — flush_pending() catches the per-row
    CalledProcessError and just leaves it for next time, outbox.py's own
    process still exits 0 either way, so scheduler.py never saw a failure
    to log either. Two real messages (one 17 hours old) were stuck exactly
    this way, found only by manually running `outbox.py flush` in the
    foreground to see the actual curl error instead of trusting the quiet
    retry loop. See facts/outbox-2000-char-silent-drop-2026-08-29.md.
    """
    if len(text) <= max_length:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_length:
        window = remaining[:max_length]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = max_length
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks if chunks else [text]


def _is_tier2_channel(channel: str) -> bool:
    """True for channels with gate_mode == 'tier2' in config/channels.json
    (currently just #agent-chat) — same scope relay.py's inbound
    context_box parsing uses, so a stray ```handoff``` fence typed
    elsewhere doesn't get parsed as a real envelope. Fails closed (False)
    on any config-read problem rather than risk mis-parsing."""
    try:
        with open(CHANNELS_CONFIG_PATH) as f:
            cfg = json.load(f)
        return (cfg.get("channels", {}).get(channel, {}) or {}).get("gate_mode") == "tier2"
    except Exception:
        return False


def _load_rows() -> list[dict]:
    if not OUTBOX_PATH.exists():
        return []
    rows = []
    with open(OUTBOX_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _save_rows(rows: list[dict]) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTBOX_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.replace(OUTBOX_PATH)


def add_pending(channel: str, content: str, attachments: list[str] | None = None) -> str:
    """Queue a message for delivery to `channel`, optionally with local
    file attachments. Returns the row id."""
    rows = _load_rows()
    row_id = str(uuid.uuid4())
    rows.append({
        "id": row_id,
        "channel": channel,
        "content": content,
        "attachments": [str(Path(p).resolve()) for p in (attachments or [])],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "delivered_at": None,
    })
    _save_rows(rows)
    return row_id


def flush_pending() -> list[str]:
    """Attempt delivery of every undelivered row via discord-notify.sh.

    Rows that fail to deliver are left undelivered for the next flush —
    transient Discord/network failures shouldn't drop a queued message,
    only a successful post marks it done.

    context_box (2026-08-29): a second outbound gap, found via a routine
    log/self-diagnostic sweep the same night agent-server.py's
    post_to_discord got its own context_box hook (see
    facts/context-box-outbound-hook-2026-08-29.md). That hook only ever
    sees a turn's *same-channel* reply (pending_final/response_text) —
    it never runs for content that took the outbox path instead, which
    is the normal route for anything meant for #agent-chat while the
    turn itself is scoped elsewhere (the common case per
    facts/outbox-is-default-not-fallback.md). A real test message sent
    that exact way earlier the same session never got recorded or
    mirrored — confirmed live by checking `/sys context` and finding it
    absent. Delivery here is the one place both routes converge, so
    it's the right spot to close the gap symmetrically rather than
    chasing every call site that might queue a handoff-bearing message.
    """
    rows = _load_rows()
    delivered = []
    # New rows generated in this same pass (context_box mirror lines) —
    # collected separately and appended to `rows` before the single save
    # at the end, rather than calling add_pending() mid-loop. add_pending()
    # does its own load-modify-save cycle; calling it here while `rows`
    # (loaded once, above) is later written back wholesale via
    # _save_rows(rows) would silently clobber whatever add_pending() had
    # just written to disk — the mirror row would be queued and then
    # immediately erased before the next flush ever saw it. Caught this
    # in review before it shipped, not live.
    new_rows = []
    changed = False
    for row in rows:
        if row.get("delivered_at"):
            continue

        # PASS-sentinel guard (2026-09-28 incident: a backed-up queue
        # flushed ~50 literal/near-literal "PASS" posts to Discord once
        # posting permission was restored). This is the actual send
        # chokepoint for the outbox path — checked against the row's full,
        # unchunked content, before _split_discord_message ever runs, so
        # chunking can't change the verdict. A match is dropped, not
        # retried: mark delivered_at now (with dropped=True for the
        # audit trail flush_pending's own docstring promises) so a PASS
        # row doesn't sit in pending.jsonl forever re-triggering this
        # check every flush. See pass_filter.py for the exact rule.
        if is_pass_sentinel(row["content"] or ""):
            log.info(
                "outbox: dropping PASS-sentinel row %s -> #%s: %r",
                row["id"], row["channel"], (row["content"] or "")[:200],
            )
            row["delivered_at"] = datetime.now(timezone.utc).isoformat()
            row["dropped"] = True
            row["drop_reason"] = "pass_sentinel"
            changed = True
            continue

        try:
            chunks = _split_discord_message(row["content"] or "")
            if not chunks:
                chunks = [""]
            for idx, chunk in enumerate(chunks):
                # Attachments ride with the first chunk only — re-uploading
                # the same file on every chunk would spam duplicates for a
                # multi-chunk message, and the first chunk is the one the
                # attachment is most likely referenced from.
                files = row.get("attachments", []) if idx == 0 else []
                subprocess.run(
                    [str(NOTIFY_SCRIPT), row["channel"], chunk, *files],
                    check=True, capture_output=True, text=True,
                )
            row["delivered_at"] = datetime.now(timezone.utc).isoformat()
            delivered.append(row["id"])
            changed = True

            if _is_tier2_channel(row["channel"]):
                envelope = parse_handoff(row["content"] or "")
                if envelope and envelope.context_box:
                    cb = envelope.context_box
                    cb_row = context_box.record(
                        subject=envelope.subject,
                        state=cb.state,
                        blocked_on=cb.blocked_on,
                        waiting_on=cb.waiting_on,
                        sender="Marvin",
                        channel=row["channel"],
                    )
                    # Don't mirror a message that's already headed to
                    # #general itself — that would just be the same
                    # content arriving twice.
                    if context_box.should_mirror(cb.state) and row["channel"] != "general":
                        new_rows.append({
                            "id": str(uuid.uuid4()),
                            "channel": "general",
                            "content": context_box.render_mirror_line(
                                envelope.subject or "(no subject)", cb_row
                            ),
                            "attachments": [],
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "delivered_at": None,
                        })
        except subprocess.CalledProcessError as e:
            sys.stderr.write(
                f"outbox: delivery failed for {row['id']} -> #{row['channel']}: "
                f"{e.stderr.strip() if e.stderr else e}\n"
            )
    if new_rows:
        rows.extend(new_rows)
        changed = True
    if changed:
        _save_rows(rows)
    return delivered


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_p = sub.add_parser("add", help="Queue a message for a channel")
    add_p.add_argument("channel")
    add_p.add_argument("content")
    add_p.add_argument(
        "--file", action="append", dest="files", default=[],
        help="Local file path to attach; repeat for multiple attachments",
    )

    sub.add_parser("flush", help="Attempt delivery of all pending rows")
    sub.add_parser("list", help="Show pending (undelivered) rows")

    args = parser.parse_args()

    if args.cmd == "add":
        row_id = add_pending(args.channel, args.content, attachments=args.files)
        suffix = f" with {len(args.files)} attachment(s)" if args.files else ""
        print(f"Queued {row_id} -> #{args.channel}{suffix}")
    elif args.cmd == "flush":
        delivered = flush_pending()
        print(f"Delivered {len(delivered)} message(s)" if delivered else "Nothing to deliver")
    elif args.cmd == "list":
        pending = [r for r in _load_rows() if not r.get("delivered_at")]
        if not pending:
            print("Outbox empty")
        for r in pending:
            atts = r.get("attachments") or []
            att_note = f" ({len(atts)} attachment(s))" if atts else ""
            print(f"{r['id']} [{r['created_at']}] -> #{r['channel']}{att_note}: {r['content'][:80]}")


if __name__ == "__main__":
    main()
