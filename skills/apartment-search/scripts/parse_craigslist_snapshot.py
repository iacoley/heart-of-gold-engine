#!/usr/bin/env python3
"""
parse_craigslist_snapshot.py — turns a Playwright accessibility-tree
snapshot of a Craigslist apartments search-results page into structured
listing dicts.

Why this exists: 2026-09-20 apartment-cron work established that
Craigslist's plain-fetch HTML is a JS shell (no real listings render),
but the Playwright browser tool's snapshot of the *rendered* page
carries everything needed per listing, including square footage, which
Zumper's plain-fetch view doesn't expose at all. That made the browser
pass the only working source for the Dogpatch profile's core 750-800+
sqft filter. This module is the reusable extractor for that snapshot
text — replacing the manual grep-and-eyeball pass done earlier in the
session with something a cron job can actually call.

Input shape: the `text` field of a browser_snapshot tool result (the
mcp__browser__browser_snapshot yaml-ish accessibility tree), NOT raw
HTML. This module doesn't fetch or drive the browser itself — that's
the caller's job (navigate, then snapshot, then hand the text here).

Known limits, stated plainly rather than glossed over:
- Craigslist's own min_sqft= URL param gets silently dropped on its
  internal redirect (confirmed live 2026-09-20) — this module does NOT
  compensate for that; filtering by sqft is the CALLER's job against the
  structured output, not something parse_snapshot() does for you.
- "posted" is returned as the raw string Craigslist shows (either an
  absolute "M/D" or a relative "Xh ago"/"Xd ago"), not normalized to a
  timestamp or day-count. Normalizing that into the staleness buckets
  Ian specified (discard >=30d, flag 7-30d, quiet <7d) is separate work,
  not done here.
- price and posted are sometimes genuinely absent from the snapshot for
  a given card (observed live: a 794ft2 2br listing with no price line
  at all). Both fields come back as None rather than guessed at or
  inherited from a neighboring listing.
- This is a structural/regex parse of a specific accessibility-tree
  shape, not a real DOM/HTML parser — if Craigslist changes their
  markup, or Playwright's ref numbering scheme changes, this breaks.
  That's the same fragility any scraper has; no attempt made to pretend
  otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Listing:
    title: str
    url: Optional[str]
    price: Optional[str]       # raw, e.g. "$5,107" — not parsed to int here
    sqft: Optional[int]
    beds: Optional[str]        # raw, e.g. "2br", "studio"
    posted: Optional[str]      # raw, e.g. "9/16" or "2h ago"
    neighborhood: Optional[str]


_TITLE_MARKER = re.compile(r'-\s*generic "([^"]+)" \[ref=e\d+\]:')
_URL = re.compile(r'/url:\s*(\S+)')
_PRICE = re.compile(r'-\s*generic \[ref=e\d+\]:\s*(\$[\d,]+)\s*$', re.MULTILINE)
_SQFT_WITH_BEDS = re.compile(
    r'text:\s*(\S+)\s*\n\s*-\s*generic \[ref=e\d+\]:\s*(\d+)ft2'
)
_POSTED = re.compile(
    r'-\s*generic \[ref=e\d+\]:\s*((?:\d{1,2}/\d{1,2})|(?:\d+\s*\w*\s*ago))\s*$',
    re.MULTILINE,
)
# Neighborhood is the bullet-separated text after the beds/sqft field,
# before the next bullet or end of that inline group — best-effort only.
_NEIGHBORHOOD = re.compile(r'ft2\s*\n(?:.*\n)*?\s*-\s*generic \[ref=e\d+\]:\s*•\s*\n\s*-\s*generic \[ref=e\d+\]:\s*([^\n$]+?)\s*$', re.MULTILINE)


def parse_snapshot(text: str) -> list[Listing]:
    """Parse one browser_snapshot text blob into a list of Listing.
    Order matches the page's own listing order. Returns [] on a blob
    with no recognizable listing markers rather than raising — an empty
    result is a legitimate signal (e.g. a genuinely empty search), not
    necessarily a parse failure, so callers should treat [] as
    "nothing found or format changed" and log accordingly rather than
    assume success."""
    markers = list(_TITLE_MARKER.finditer(text))
    listings: list[Listing] = []

    for i, m in enumerate(markers):
        title = m.group(1)
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        chunk = text[start:end]

        url_m = _URL.search(chunk)
        price_m = _PRICE.search(chunk)
        sqft_beds_m = _SQFT_WITH_BEDS.search(chunk)
        posted_m = _POSTED.search(chunk)
        neigh_m = _NEIGHBORHOOD.search(chunk)

        listings.append(Listing(
            title=title,
            url=url_m.group(1) if url_m else None,
            price=price_m.group(1) if price_m else None,
            sqft=int(sqft_beds_m.group(2)) if sqft_beds_m else None,
            beds=sqft_beds_m.group(1) if sqft_beds_m else None,
            posted=posted_m.group(1) if posted_m else None,
            neighborhood=neigh_m.group(1).strip() if neigh_m else None,
        ))

    return listings


# -- selftest -------------------------------------------------------------
def _selftest() -> int:
    """Runs against the real fixture captured live 2026-09-20 (a genuine
    Dogpatch Craigslist snapshot, not synthetic data) and checks known
    values pulled by hand during the session against what the parser
    extracts, so this is verified against ground truth rather than
    against itself."""
    import os
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")

    print("── parse_craigslist_snapshot selftest (fixture-based) ──")

    fixture_path = os.path.join(
        os.path.dirname(__file__), "fixtures",
        "craigslist_dogpatch_snapshot_2026-09-20.txt",
    )
    with open(fixture_path, encoding="utf-8") as f:
        text = f.read()

    listings = parse_snapshot(text)
    check(f"parsed a non-trivial number of listings (got {len(listings)})",
          len(listings) >= 15)

    by_title = {l.title: l for l in listings}

    l = by_title.get("Modern 2BR/2BA Apartment in Historic Dogpatch District")
    check("980ft2/$7,995 listing found with correct sqft", l is not None and l.sqft == 980)
    check("980ft2/$7,995 listing found with correct price", l is not None and l.price == "$7,995")
    check("980ft2/$7,995 listing has a real craigslist URL",
          l is not None and l.url is not None and l.url.startswith("https://www.craigslist.org/view/"))
    check("980ft2/$7,995 listing beds parsed as 2br", l is not None and l.beds == "2br")

    ll_specials = [l for l in listings
                   if l.title.startswith("TMLP 12-mo. | Dogpatch - L&L Special")]
    check("three L&L Special duplicate-unit listings found", len(ll_specials) == 3)
    check("all three L&L Special units are 766 sqft", all(l.sqft == 766 for l in ll_specials))
    check("all three L&L Special units are $5,107", all(l.price == "$5,107" for l in ll_specials))

    cw = by_title.get("Community Workspaces, Walk-in Closet, Service Alarm Ready")
    check("Community Workspaces listing found", cw is not None)
    check("Community Workspaces sqft parsed as 794", cw is not None and cw.sqft == 794)
    check("Community Workspaces has NO price (known gap, must be None not guessed)",
          cw is not None and cw.price is None)
    check("Community Workspaces posted parsed as relative time",
          cw is not None and cw.posted == "2h ago")

    # A candidate that actually clears the Dogpatch profile bar end to
    # end: sqft >= 750, price <= $6000 hard cap.
    def price_to_int(p):
        return int(p.replace("$", "").replace(",", "")) if p else None

    candidates = [l for l in listings
                  if l.sqft and l.sqft >= 750
                  and l.price and price_to_int(l.price) <= 6000]
    check("at least one real listing clears the Dogpatch profile bar (>=750sqft, <=$6000)",
          len(candidates) >= 1)

    print("PASS  parser verified against real captured data" if not fails
          else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
