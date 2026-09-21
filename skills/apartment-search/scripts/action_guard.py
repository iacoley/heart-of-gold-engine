#!/usr/bin/env python3
"""
action_guard.py — the one choke point every apartment-search code path
(scraper, scorer, digest sender, interrupt sender) is required to import
and call through, rather than deciding for itself whether an action is
safe.

Why this exists: 2026-09-20 audit of Karakos's dangerous-action
protection (see facts/... and task-1789944335) found no general safety
layer — every existing gate (agent-server restart is Ian-only, PR
self-merge needs review, gmail_guard.py's folder scoping) is bespoke,
added reactively after a specific action got flagged. Rather than add
one more ad hoc check buried inside the apartment-search code, this
module is that job's bespoke gate, written the same deliberate way
gmail_guard.py was: deny-by-default, one file, no caller-supplied way
to widen it.

Two standing rules this enforces (Ian, #general, 2026-09-04 and
reaffirmed/tightened 2026-09-20 — see
facts/sf-apartment-search-standing-constraints-2026-09-04.md):

1. Never contact a listing (landlord/lister/agent) on Ian's behalf,
   under any circumstance. Finding and triaging candidates is in scope;
   reaching out is not.
2. Never post apartment-search content to #lounge. This job's home
   channel is #general only (tightened 2026-09-20 — #agent-chat is no
   longer a permitted second home either).

Design choice, matching Zero's critique of runtime string-sniffing
(2026-09-20, #agent-chat): this does NOT try to detect "is this message
about apartments" or "does this string look like an address" — that's
exactly the discovery-triggered, regex-on-content approach that grinds
to a halt on false positives. Instead it gates by *declared action
type*, checked at the one or two real call sites (send an outbound
contact, post to a channel) rather than by inspecting content. A caller
has to explicitly declare what it's trying to do; the guard doesn't
guess.

Known limit, stated plainly: this cannot stop a future script from
calling send_email() or the discord tool directly and bypassing this
entirely — same limit gmail_guard.py documents about itself. What it
does is make every legitimate apartment-search call site go through one
auditable choke point, so a bypass is a conspicuous departure from the
sanctioned path, not an accidental one-liner.
"""

from __future__ import annotations


class ListingContactBlocked(Exception):
    """Raised whenever anything in the apartment-search code tries to
    contact a listing directly. Always raised — there is no allowed
    path, no flag, no override. If this needs to stop being true, that's
    a decision for Ian to make explicitly, not a code change to make
    quietly."""


class DisallowedChannel(Exception):
    """Raised when apartment-search content is about to be posted
    somewhere other than its permitted home channel."""


# Not a parameter anywhere below — cannot be widened by a caller passing
# a different value in. Matches ALLOWED_FOLDER's role in gmail_guard.py.
PERMITTED_CHANNEL = "general"


def guard_contact_listing(*_args, **_kwargs) -> None:
    """Call this at the top of anything that would reach out to a
    landlord/lister/agent (email, DM, web form submit, phone-lookup
    action, etc). It takes and ignores arguments on purpose, so a call
    site can't satisfy it by passing some 'this one's fine' flag — there
    is no code path through this function that returns normally."""
    raise ListingContactBlocked(
        "Direct contact with a listing is never permitted from this "
        "codebase. Surface the listing URL and contact info in the "
        "digest/report instead; Ian does the outreach himself."
    )


def guard_post_channel(channel: str) -> None:
    """Call this before posting any apartment-search content anywhere.
    Raises unless the target is exactly the permitted channel — no
    partial match, no case-insensitive convenience, no 'lounge is
    basically general' judgment call left to the caller."""
    if channel != PERMITTED_CHANNEL:
        raise DisallowedChannel(
            f"Apartment-search content may only post to "
            f"#{PERMITTED_CHANNEL}, not #{channel}. If the destination "
            f"legitimately needs to change, that's Ian's call to make "
            f"explicitly (see facts/sf-apartment-search-standing-"
            f"constraints-2026-09-04.md), not a one-line exception here."
        )


# -- selftest -------------------------------------------------------------
def _selftest() -> int:
    """Behavioral checks — actually calls both guards and confirms they
    fail closed, rather than just inspecting source like gmail_guard's
    static-only selftest does. No network, no credentials needed."""
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")

    print("── action_guard selftest (behavioral) ──")

    # guard_contact_listing must ALWAYS raise, regardless of what's passed.
    for args, kwargs in [
        ((), {}),
        (("landlord@example.com",), {}),
        ((), {"override": True}),
        ((), {"reason": "just this once"}),
    ]:
        try:
            guard_contact_listing(*args, **kwargs)
            ok = False
        except ListingContactBlocked:
            ok = True
        except Exception:
            ok = False
        check(f"guard_contact_listing raises for args={args} kwargs={kwargs}", ok)

    # guard_post_channel must pass #general, block everything else,
    # including near-misses and case variants.
    try:
        guard_post_channel("general")
        ok = True
    except DisallowedChannel:
        ok = False
    check("guard_post_channel allows 'general'", ok)

    for bad_channel in ["lounge", "agent-chat", "General", "signals", ""]:
        try:
            guard_post_channel(bad_channel)
            ok = False
        except DisallowedChannel:
            ok = True
        check(f"guard_post_channel blocks '{bad_channel}'", ok)

    check("PERMITTED_CHANNEL is a hardcoded module constant, not settable via env/arg",
          PERMITTED_CHANNEL == "general")

    print("PASS  gate fails closed on every case above" if not fails
          else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
