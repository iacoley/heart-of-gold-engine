"""pass_filter.py — shared "PASS sentinel" detection for outbound Discord
posts.

Incident (2026-09-28 ~20:42 PT): after an agent's posting permission in a
channel was restored, its backed-up outbox queue flushed ~50 messages that
were literally the bare word PASS (the agent's "stay silent" sentinel),
plus variants where the agent explained itself and then signed off with a
trailing PASS token, e.g.:

    Same as last round — nothing's changed, nothing needs saying again.

    PASS

    Already flagged and resolved with Zero last round. Nothing new here.PASS

    Healthy, inbox empty, nothing new since the last check. PASS

PASS must never reach Discord. agent-server.py's post_to_discord() call
site already screens pending_final through a PASS_SENTINEL_RE before
posting (see the comment above that regex, 2026-09-02) — but bin/outbox.py's
flush_pending() is a second, independent send path (the durable
cross-channel queue; discord-notify.sh is its actual delivery mechanism)
that funnels agent-authored content straight to Discord with no PASS
screening at all. That's the gap this incident actually went through.

This module is the single, dependency-free place both send paths import
from, so the definition of "is this message a PASS sentinel" can't drift
between them.

Rule (case-sensitive, uppercase PASS only):
  - the message begins with a bare PASS token (PASS, PASS., PASS — nothing
    new), or
  - the message ends with a trailing PASS token (the agent said its piece,
    then signed off with PASS) — in that case the WHOLE message is
    suppressed, since the agent's intent was silence.

Markdown emphasis wrapping the message (*PASS*, **PASS**, _PASS_, `PASS`)
is stripped before the check, same as ordinary whitespace. A PASS that is
merely a substring of a longer word (PASSWORD, Passed, bypass, compass) or
that appears mid-sentence never matches — case sensitivity alone rules out
the lowercase/mixed-case forms, and the leading/trailing anchoring rules
out the rest.

Evaluate the WHOLE reply with this function before any chunking/splitting
for Discord's 2000-char limit — chunking is a delivery detail and must
never change whether a message counts as a PASS sentinel.
"""

import re

# Whitespace + markdown emphasis/code-span markers to strip from both ends
# before testing. str.strip(chars) removes any run of these characters from
# each end in one pass, so "**_PASS_**" and "`PASS`" both reduce to "PASS"
# without needing to peel off each wrapper layer individually.
_STRIP_CHARS = " \t\r\n*_~`"

# Anchored to the stripped-and-unwrapped core string only (not to embedded
# markdown), so a real boundary word check (\b) still applies. No
# re.IGNORECASE — PASS is uppercase-only by spec.
_LEADING_PASS_RE = re.compile(r"^PASS\b")
_TRAILING_PASS_RE = re.compile(r"\bPASS[.!?]*$")


def is_pass_sentinel(text: str) -> bool:
    """True if `text` is (or ends with) the agent's PASS "stay silent"
    sentinel and must not be posted to Discord. See module docstring for
    the exact rule and the incident that made it necessary."""
    core = (text or "").strip(_STRIP_CHARS)
    if not core:
        return False
    return bool(_LEADING_PASS_RE.match(core) or _TRAILING_PASS_RE.search(core))
