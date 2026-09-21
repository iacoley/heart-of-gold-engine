#!/usr/bin/env python3
"""
run_digest.py — single entry point tying parse -> score -> deliver
together for one digest run.

Why this exists as a separate module rather than leaving each run as
inline glue code: the 2026-09-21 first live run (see memory fact
apartment-digest-scorer-and-first-live-send-2026-09-21) wired
parse_craigslist_snapshot -> scorer -> digest by hand in a one-off
script. This is that glue, made callable so the next run (or a cron
job, once one exists) isn't reconstructing it from scratch.

Snapshot acquisition is deliberately NOT done here. Fetching a live
Craigslist page requires a real browser session (mcp__browser__* tools),
which only an agent turn can drive — a plain python script invoked by
tools-server.py or cron has no browser context. So the contract is:
whatever already has a browser_snapshot text blob (an agent turn today;
possibly a headless-browser-capable cron job later) calls
run(snapshot_text, source_label) with it. This module owns everything
downstream of that.

Usage (from an agent turn that just captured a live snapshot):
    import run_digest
    result = run_digest.run(snapshot_text, source_label="live craigslist dogpatch query")
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import digest as dg
import parse_craigslist_snapshot as pcs
import scorer as sc


def run(snapshot_text: str, source_label: str,
        run_date: Optional[date] = None, send: bool = True) -> dict:
    """Parse a browser_snapshot text blob, score it, build the digest
    email, and (unless send=False) actually send it. Returns a summary
    dict — never raises on an empty/no-match snapshot, since parse_snapshot
    itself treats [] as a legitimate signal rather than an error (see its
    own docstring)."""
    run_date = run_date or date.today()

    listings = pcs.parse_snapshot(snapshot_text)
    bucket = sc.score_all(listings, reference_date=run_date)
    subject, body = dg.build_digest_email(bucket, source_label, run_date=run_date)

    result = {
        "parsed": len(listings),
        "top_picks": len(bucket.top_picks),
        "needs_check": len(bucket.needs_check),
        "discarded": bucket.discarded_count,
        "subject": subject,
        "sent": False,
    }

    if send:
        send_result = dg.send_digest(subject, body)
        result["sent"] = send_result.get("status") == "sent"
        result["send_result"] = send_result

    return result


# -- selftest ---------------------------------------------------------------
def _selftest() -> int:
    """End-to-end wiring check against the real fixture, send=False so it
    never mails anything. Component behavior is already covered by
    parse_craigslist_snapshot.py's, scorer.py's, and digest.py's own
    selftests — this only checks the three actually compose."""
    import os

    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")

    print("── run_digest selftest (wiring only, send disabled) ──")

    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures",
        "craigslist_dogpatch_snapshot_2026-09-20.txt",
    )
    with open(fixture_path, encoding="utf-8") as f:
        text = f.read()

    result = run(text, "fixture: dogpatch 2026-09-20",
                 run_date=date(2026, 9, 20), send=False)

    check("parsed a non-trivial number of listings", result["parsed"] >= 15)
    check("at least one top pick", result["top_picks"] >= 1)
    check("send=False means nothing was actually sent", result["sent"] is False)
    check("no send_result key when send=False", "send_result" not in result)

    print("PASS  end-to-end wiring verified" if not fails
          else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
