#!/usr/bin/env python3
"""
scorer.py — turns parse_craigslist_snapshot.Listing objects into ranked,
digest-ready candidates against Ian's standing SF apartment criteria.

Criteria source: data/attachments/1544416431842787540/
0-sf-apartment-search-handoff.md, specifically the 2026-09-02 budget/size
update (binding, supersedes the original transit-first framing) and the
staleness buckets Ian specified in that same thread:
  - rent ceiling: $5,500/month (hard)
  - minimum size: 750 sqft (hard)
  - posted >=30 days ago: discard (hard)
  - posted 7-30 days ago: include but flag as stale
  - posted <7 days ago: include, no flag
  - preferred neighborhoods (walkable, transit-adjacent, matches the
    day-to-day-livability pivot): Mission, Dogpatch, Potrero Hill, SoMa,
    South Beach, Mission Bay, Glen Park
  - 1BR ideal but not required — the loft/larger-2BR direction was
    explicitly reopened once the $5,500/750sqft numbers were set, so beds
    is a soft signal, not a filter
  - sofa footprint (Room & Board André, ~8.5ft wall run + 6ft chaise) and
    Cal King bed are real fit constraints but not extractable from a
    Craigslist card — surfaced as a standing caveat in the digest, not
    scored, so a listing never gets silently dropped for a factor this
    module can't actually see.

Design choice matching parse_craigslist_snapshot.py's own stated posture:
state limits plainly rather than pretend precision. Missing price/sqft
data doesn't get guessed at — a listing with an unscorable required field
goes to a separate "needs manual check" bucket instead of being either
silently dropped or silently scored as if the field were fine.

This module does not import action_guard itself — it only produces data
(scored Listing wrappers), it never contacts a listing or picks a
delivery channel. Callers that turn this into outbound anything (email,
Discord post) are the ones responsible for going through action_guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from parse_craigslist_snapshot import Listing

RENT_CEILING = 5500
SQFT_FLOOR = 750
DISCARD_AGE_DAYS = 30
FLAG_AGE_DAYS = 7

# Below this $/sqft/month, a listing is priced well outside anything seen
# in real SF comps in this price range (observed live 2026-09-21: genuine
# nearby listings cluster $5-8/sqft/month; a $1,925/1,035sqft Potrero
# listing came back at $1.86/sqft and ranked #1 on budget points alone
# before this existed). Classic bait-and-switch/wire-fraud shape on
# Craigslist. Not discarded outright — a real below-market deal does
# exist sometimes — but never allowed to rank on unexamined budget
# points; flagged loud instead so Ian sees the warning before the price.
SUSPICIOUS_PRICE_PER_SQFT = 3.0

PREFERRED_NEIGHBORHOODS = [
    "mission", "dogpatch", "potrero", "soma", "south beach",
    "mission bay", "glen park",
]

_RELATIVE_AGO = re.compile(r"^\s*(\d+)\s*([a-zA-Z]+)\s*ago\s*$")
_ABS_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")


@dataclass
class ScoredListing:
    listing: Listing
    score: Optional[float]         # None if it landed in needs_manual_check
    age_days: Optional[int]
    flags: list = field(default_factory=list)
    reject_reason: Optional[str] = None   # set only for discarded listings


def normalize_age_days(posted: Optional[str], reference_date: date) -> Optional[int]:
    """Best-effort normalize Craigslist's raw 'posted' string to an
    integer day-count relative to reference_date. Returns None if posted
    is missing or doesn't match either known shape — callers must treat
    None as 'unknown', not as 0 or as infinitely old."""
    if not posted:
        return None

    m = _RELATIVE_AGO.match(posted)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("h"):
            return 0
        if unit.startswith("d"):
            return n
        if unit.startswith("min"):
            return 0
        if unit.startswith("w"):
            return n * 7
        return None

    m = _ABS_DATE.match(posted)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            posted_date = date(reference_date.year, month, day)
        except ValueError:
            return None
        if posted_date > reference_date:
            # Wrapped a year boundary (e.g. Dec posting, Jan reference).
            posted_date = date(reference_date.year - 1, month, day)
        return (reference_date - posted_date).days

    return None


def _price_to_int(price: Optional[str]) -> Optional[int]:
    if not price:
        return None
    digits = re.sub(r"[^\d]", "", price)
    return int(digits) if digits else None


def _neighborhood_match(listing: Listing) -> bool:
    haystack = f"{listing.neighborhood or ''} {listing.title}".lower()
    return any(n in haystack for n in PREFERRED_NEIGHBORHOODS)


def score_listing(listing: Listing, reference_date: date) -> ScoredListing:
    age_days = normalize_age_days(listing.posted, reference_date)
    price = _price_to_int(listing.price)
    flags: list = []

    # -- hard filters (discard, never shown in the digest) -----------
    if listing.sqft is not None and listing.sqft < SQFT_FLOOR:
        return ScoredListing(listing, None, age_days, [],
                              f"under sqft floor ({listing.sqft} < {SQFT_FLOOR})")
    if price is not None and price > RENT_CEILING:
        return ScoredListing(listing, None, age_days, [],
                              f"over rent ceiling (${price} > ${RENT_CEILING})")
    if age_days is not None and age_days >= DISCARD_AGE_DAYS:
        return ScoredListing(listing, None, age_days, [],
                              f"stale posting ({age_days}d >= {DISCARD_AGE_DAYS}d)")

    # -- needs-manual-check: a required field is unscorable, not guessed
    if listing.sqft is None or price is None:
        missing = []
        if listing.sqft is None:
            missing.append("sqft")
        if price is None:
            missing.append("price")
        flags.append(f"missing {'/'.join(missing)} — needs manual check")
        return ScoredListing(listing, None, age_days, flags, None)

    # -- soft flags (included, but marked) ----------------------------
    if age_days is not None and age_days >= FLAG_AGE_DAYS:
        flags.append(f"posted {age_days}d ago — verify still available")
    if age_days is None:
        flags.append("posting date unrecognized — verify still available")

    # -- scam/implausible-price check (flag, don't silently rank) ------
    price_per_sqft = price / listing.sqft
    suspicious_price = price_per_sqft < SUSPICIOUS_PRICE_PER_SQFT
    if suspicious_price:
        flags.append(
            f"price implausibly low for area (${price_per_sqft:.2f}/sqft) "
            "— possible scam/bait listing, verify carefully before any contact"
        )

    # -- scoring (0-100) ------------------------------------------------
    budget_pts = max(0.0, min(40.0, (RENT_CEILING - price) / RENT_CEILING * 40))
    if suspicious_price:
        # Don't let an implausible price buy top-of-digest placement on
        # budget points alone — cap it back down to a neutral value.
        budget_pts = min(budget_pts, 10.0)
    size_pts = max(0.0, min(30.0, (listing.sqft - SQFT_FLOOR) / 450 * 30))
    neighborhood_pts = 20.0 if _neighborhood_match(listing) else 10.0
    if age_days is None:
        recency_pts = 5.0
    elif age_days < FLAG_AGE_DAYS:
        recency_pts = 10.0
    else:
        recency_pts = 5.0

    score = round(budget_pts + size_pts + neighborhood_pts + recency_pts, 1)
    return ScoredListing(listing, score, age_days, flags, None)


@dataclass
class DigestBucket:
    top_picks: list       # ScoredListing, sorted by score desc
    needs_check: list     # ScoredListing with score=None, reject_reason=None
    discarded_count: int  # not surfaced individually, just a count


def score_all(listings: list[Listing], reference_date: Optional[date] = None) -> DigestBucket:
    reference_date = reference_date or date.today()
    scored = [score_listing(l, reference_date) for l in listings]

    discarded = [s for s in scored if s.reject_reason is not None]
    needs_check = [s for s in scored if s.score is None and s.reject_reason is None]
    ranked = sorted(
        (s for s in scored if s.score is not None),
        key=lambda s: s.score, reverse=True,
    )
    return DigestBucket(top_picks=ranked, needs_check=needs_check,
                         discarded_count=len(discarded))


# -- selftest -------------------------------------------------------------
def _selftest() -> int:
    """Runs against the same real fixture parse_craigslist_snapshot.py
    verifies against, so the scorer is checked against ground truth
    listings, not synthetic ones."""
    import os

    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")

    print("── scorer selftest (fixture-based) ──")

    # normalize_age_days unit checks
    ref = date(2026, 9, 20)
    check("'2h ago' -> 0 days", normalize_age_days("2h ago", ref) == 0)
    check("'5d ago' -> 5 days", normalize_age_days("5d ago", ref) == 5)
    check("'9/20' (same day) -> 0 days", normalize_age_days("9/20", ref) == 0)
    check("'9/11' -> 9 days", normalize_age_days("9/11", ref) == 9)
    check("None posted -> None", normalize_age_days(None, ref) is None)
    check("garbage string -> None", normalize_age_days("whenever", ref) is None)

    # suspicious-price flag: a real observed case (live 2026-09-21 run,
    # $1,925/1,035sqft Potrero listing) hardcoded as a regression check.
    scam_listing = Listing(title="scam-shaped", url="http://x", price="$1,925",
                            sqft=1035, beds="2br", posted="1d ago",
                            neighborhood="potrero hill")
    scam_scored = score_listing(scam_listing, ref)
    check("implausibly cheap listing gets flagged, not silently top-ranked",
          scam_scored.score is not None
          and any("possible scam" in f for f in scam_scored.flags))
    check("implausibly cheap listing's budget points are capped, not maxed",
          scam_scored.score is not None and scam_scored.score < 60)

    import parse_craigslist_snapshot as pcs

    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures",
        "craigslist_dogpatch_snapshot_2026-09-20.txt",
    )
    with open(fixture_path, encoding="utf-8") as f:
        text = f.read()
    listings = pcs.parse_snapshot(text)

    bucket = score_all(listings, reference_date=date(2026, 9, 20))

    check("at least one top pick survives real fixture data",
          len(bucket.top_picks) >= 1)
    check("every top pick clears the hard sqft floor",
          all(s.listing.sqft >= SQFT_FLOOR for s in bucket.top_picks))
    check("every top pick clears the hard rent ceiling",
          all(_price_to_int(s.listing.price) <= RENT_CEILING for s in bucket.top_picks))
    check("top picks are sorted descending by score",
          all(bucket.top_picks[i].score >= bucket.top_picks[i + 1].score
              for i in range(len(bucket.top_picks) - 1)))
    check("the no-price 794ft2 listing lands in needs_check, not silently scored",
          any(s.listing.title.startswith("Community Workspaces")
              for s in bucket.needs_check))
    check("no listing appears in both top_picks and needs_check",
          {id(s.listing) for s in bucket.top_picks}
          .isdisjoint({id(s.listing) for s in bucket.needs_check}))

    print("PASS  scorer verified against real captured data" if not fails
          else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
