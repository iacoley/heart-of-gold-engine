#!/usr/bin/env python3
"""
digest.py — formats a scorer.DigestBucket into the plain-text email body
Ian actually reads, and sends it.

This is the delivery half of task-1788557410. It does not fetch or parse
anything itself (that's parse_craigslist_snapshot.py + whatever drives
the browser) and it does not score anything (that's scorer.py) — it only
turns already-scored candidates into an email and gets that email sent.

Delivery mechanism: shells out to skills/email/scripts/send_email.py the
same way mcp/tools-server.py itself invokes any skill script (TOOL_ARGS
env var, JSON on stdout) — see handle_skill_tool() in mcp/tools-server.py.
Reusing that exact contract instead of importing Mailgun logic directly
means this never duplicates or drifts from the credential-loading /
signature / standing-bcc behavior send_email.py already owns, and this
script keeps working unmodified if that behavior changes later.

Channel note: action_guard.py's guard_post_channel() gates *Discord*
channel choice (#general only, per facts/sf-apartment-search-standing-
constraints-2026-09-04.md) — it has no opinion on email, which is the
digest's actual intended delivery path per task-1788557410. This module
does not call guard_post_channel for that reason. It also never imports
or calls guard_contact_listing — nothing here contacts a listing, only
Ian.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from scorer import DigestBucket, ScoredListing

RECIPIENT = "iacoley.phone@gmail.com"
SEND_EMAIL_SCRIPT = (
    Path(__file__).resolve().parents[2] / "email" / "scripts" / "send_email.py"
)


def _listing_line(s: ScoredListing) -> str:
    l = s.listing
    price = l.price or "price unknown"
    sqft = f"{l.sqft}sqft" if l.sqft is not None else "sqft unknown"
    beds = l.beds or "beds unknown"
    neigh = f" — {l.neighborhood}" if l.neighborhood else ""
    score = f"[{s.score:.0f}]" if s.score is not None else "[--]"
    flags = f"  ({'; '.join(s.flags)})" if s.flags else ""
    url = l.url or "(no url captured)"
    return f"{score} {l.title}{neigh}\n      {price} · {sqft} · {beds}{flags}\n      {url}"


def build_digest_email(bucket: DigestBucket, source_label: str,
                        run_date: date | None = None) -> tuple[str, str]:
    """Returns (subject, body). Pure formatting, no I/O."""
    run_date = run_date or date.today()
    n_picks = len(bucket.top_picks)
    n_check = len(bucket.needs_check)

    subject = f"Apartment digest {run_date.isoformat()} — {n_picks} candidate(s)"

    lines = [
        f"SF apartment digest — {run_date.isoformat()}",
        f"Source: {source_label}",
        "Criteria: <=$5,500/mo, >=800sqft, posted <30d "
        "(7-30d flagged), Hayes Valley/NoPa primary + Mission fallback "
        "(Dogpatch/Potrero/SoMa/etc lower priority), 1BR ideal but "
        "building type flexible.",
        "",
    ]

    if n_picks:
        lines.append(f"-- Top picks ({n_picks}) --")
        for s in bucket.top_picks:
            lines.append(_listing_line(s))
            lines.append("")
    else:
        lines.append("-- Top picks --")
        lines.append("None this run.")
        lines.append("")

    if n_check:
        lines.append(f"-- Needs manual check ({n_check}, missing price or sqft) --")
        for s in bucket.needs_check:
            lines.append(_listing_line(s))
            lines.append("")

    lines.append(
        f"Discarded (over budget, under floor, or >=30d stale): "
        f"{bucket.discarded_count}"
    )
    lines.append("")
    lines.append(
        "Standing caveat: none of the above accounts for room-dimension "
        "fit against the Cal King bed or the Room & Board Andre sofa "
        "(~8.5ft wall run + 6ft chaise) — verify layout before ruling "
        "anything in or out on sqft alone."
    )
    lines.append("")
    lines.append(
        "Nothing above has been contacted. Reaching out to a listing "
        "is always your step, not this pipeline's."
    )

    return subject, "\n".join(lines)


def send_digest(subject: str, body: str) -> dict:
    """Shells out to send_email.py exactly as tools-server.py's own
    handle_skill_tool() would, and returns its parsed JSON result."""
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = os.environ.get("WORKSPACE_ROOT", "/opt/karakos")
    env["TOOL_ARGS"] = json.dumps({
        "to": RECIPIENT,
        "subject": subject,
        "body": body,
        "from_name": "Marvin",
    })
    result = subprocess.run(
        [sys.executable, str(SEND_EMAIL_SCRIPT)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or f"exit {result.returncode}"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": f"non-JSON output: {result.stdout.strip()}"}


# -- selftest ---------------------------------------------------------------
def _selftest() -> int:
    """Formatting-only checks, no network. Delivery is exercised for real
    by run_digest.py's live run, not by this selftest — sending a real
    email on every selftest invocation would spam Ian's inbox every time
    this module gets imported/tested."""
    import os as _os

    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")

    print("── digest selftest (formatting only, no send) ──")

    import parse_craigslist_snapshot as pcs
    import scorer as sc

    fixture_path = _os.path.join(
        _os.path.dirname(__file__), "fixtures",
        "craigslist_dogpatch_snapshot_2026-09-20.txt",
    )
    with open(fixture_path, encoding="utf-8") as f:
        listings = pcs.parse_snapshot(f.read())

    bucket = sc.score_all(listings, reference_date=date(2026, 9, 20))
    subject, body = build_digest_email(bucket, "fixture: dogpatch 2026-09-20",
                                        run_date=date(2026, 9, 20))

    check("subject mentions the candidate count",
          str(len(bucket.top_picks)) in subject)
    check("body contains the criteria line", "$5,500" in body)
    check("body contains at least one real listing URL",
          "craigslist.org/view" in body)
    check("body contains the no-contact caveat", "not this pipeline's" in body)
    check("body contains the furniture-fit caveat", "Andre sofa" in body)
    check("send_email.py script path resolves",
          SEND_EMAIL_SCRIPT.is_file())

    print("PASS  digest formatting verified" if not fails
          else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
