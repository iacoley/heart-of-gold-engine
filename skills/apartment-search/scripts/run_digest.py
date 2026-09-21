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

Multi-source as of 2026-09-21 (second pass, same day): the first cut
only ever took one snapshot per run, which in practice meant one
neighborhood search (Hayes Valley+Dogpatch combined) per digest even
though task-1788557410's own params name Hayes Valley, NoPa, *and*
Mission. That's a real functionality gap, not a style choice — run()
now takes a list of (snapshot_text, source_label) pairs, one per
Craigslist search actually run, and merges + dedupes (by URL, falling
back to title/price/sqft when a card has no URL) before scoring, so a
digest can cover every neighborhood query in one pass instead of only
whichever one happened to get captured.

Snapshot acquisition is deliberately NOT done here. Fetching a live
Craigslist page requires a real browser session (mcp__browser__* tools),
which only an agent turn can drive — a plain python script invoked by
tools-server.py or cron has no browser context. So the contract is:
whatever already has browser_snapshot text blobs (an agent turn today;
possibly a headless-browser-capable cron job later) calls
run(snapshots) with them. This module owns everything downstream of
that.

Usage (from an agent turn that just captured live snapshots for each
neighborhood search):
    import run_digest
    result = run_digest.run([
        (hayes_valley_snapshot_text, "craigslist: hayes valley"),
        (nopa_snapshot_text, "craigslist: nopa"),
        (mission_snapshot_text, "craigslist: mission"),
    ])
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import digest as dg
import parse_craigslist_snapshot as pcs
import scorer as sc


def _dedupe_key(listing):
    # URL is the strongest identity signal a Craigslist card has. Some
    # cards come back with no URL at all (parse_craigslist_snapshot's own
    # documented gap) — fall back to a (title, price, sqft) tuple rather
    # than dropping the listing or treating every url-less card as
    # colliding with every other one.
    return listing.url or (listing.title, listing.price, listing.sqft)


def run(snapshots: list[tuple[str, str]],
        run_date: Optional[date] = None, send: bool = True) -> dict:
    """Parse one or more browser_snapshot text blobs (one per
    neighborhood/query actually run), merge + dedupe the listings, score
    the combined set, build the digest email, and (unless send=False)
    actually send it. Returns a summary dict — never raises on an empty/
    no-match snapshot, since parse_snapshot itself treats [] as a
    legitimate signal rather than an error (see its own docstring)."""
    run_date = run_date or date.today()

    parsed_by_source: dict[str, int] = {}
    seen = set()
    merged = []
    for snapshot_text, source_label in snapshots:
        listings = pcs.parse_snapshot(snapshot_text)
        parsed_by_source[source_label] = len(listings)
        for listing in listings:
            key = _dedupe_key(listing)
            if key in seen:
                continue
            seen.add(key)
            merged.append(listing)

    total_parsed = sum(parsed_by_source.values())
    bucket = sc.score_all(merged, reference_date=run_date)
    combined_label = "; ".join(
        f"{label} ({n})" for label, n in parsed_by_source.items()
    )
    subject, body = dg.build_digest_email(bucket, combined_label, run_date=run_date)

    result = {
        "parsed": len(merged),
        "parsed_by_source": parsed_by_source,
        "deduped": total_parsed - len(merged),
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

    result = run([(text, "fixture: dogpatch 2026-09-20")],
                 run_date=date(2026, 9, 20), send=False)

    check("parsed a non-trivial number of listings", result["parsed"] >= 15)
    check("at least one top pick", result["top_picks"] >= 1)
    check("send=False means nothing was actually sent", result["sent"] is False)
    check("no send_result key when send=False", "send_result" not in result)
    check("parsed_by_source keyed by the one source label",
          result["parsed_by_source"] == {"fixture: dogpatch 2026-09-20": result["parsed"]})
    check("no dedup collisions against a single source",
          result["deduped"] == 0)

    # Multi-source: feed the same fixture in twice under different labels
    # to prove real listings (same URL) get deduped rather than doubled,
    # while parsed_by_source still reports the raw per-source counts.
    multi = run(
        [(text, "fixture: pass 1"), (text, "fixture: pass 2")],
        run_date=date(2026, 9, 20), send=False,
    )
    check("feeding the same snapshot twice does not double top_picks",
          multi["top_picks"] == result["top_picks"])
    check("duplicate pass is fully deduped away",
          multi["deduped"] == multi["parsed_by_source"]["fixture: pass 1"])
    check("parsed_by_source reports both source labels",
          set(multi["parsed_by_source"]) == {"fixture: pass 1", "fixture: pass 2"})

    print("PASS  end-to-end wiring verified" if not fails
          else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
