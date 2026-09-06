"""handoff.py — parse the small structured envelope agents attach alongside
prose in shared channels. Proposed by Amos (Mike's Karakos instance) on
2026-08-05, after a day of Marvin and Amos both guessing what the other
wanted from a message and getting it wrong in both directions.

Measured premise (Amos's numbers, checked live, not assumed): a full agent
turn on his side averages ~437k input tokens; his longest message to Marvin
was ~450 tokens. Compressing prose saves under 0.1% against the cost of one
turn that shouldn't have happened at all. A shared glyph vocabulary was
rejected on this basis — the earlier "∎ ⟳ ⊕ ⊗ ⧖" proposal in relay.py's
anti-loop check predates this and still stands for its narrow purpose, but
this supersedes it as the general mechanism. The only thing worth optimising
is whether a turn happens, hence one field — `reply` — that's read instead
of guessed.

The envelope is additive, not a replacement channel. Prose stays plain
English and auditable; the envelope is a fenced ```handoff block next to it,
never instead of it. This is deliberate: an inter-agent channel that can't
be read by the humans sharing the room is the failure mode both sides were
avoiding, not a feature to build toward.

A missing or malformed envelope MUST degrade to exactly the behaviour that
existed before this file — parse failures fail open, never closed. A broken
envelope is a reason to fall through to reply_gate's normal scoring, never a
reason to drop the message. An unrecognised `kind` is treated the same way:
the enum below is exhaustive as of v0 (every value is a message type that
actually passed between Marvin and Amos on 2026-08-05, nothing speculative),
so a value outside it is more likely a typo than a new type worth honouring
silently — it fails the envelope open rather than acting on a guess.

    envelope = parse_handoff(message.content)
    if envelope and envelope.reply == "required":
        ...forced wake, free, same tier as an @mention...
    elif envelope and envelope.reply == "none":
        ...forced quiet, skip even the Tier 2 scorer call...
    else:
        ...fall through to reply_gate.ReplyGate unchanged...

Caller-side convention (not enforced here, since this module only parses):
if `reply == "none"` but the prose contains a `?`, log the disagreement
instead of waking. The sender's declared intent still wins — silence stays
free — but a mismatch is a signal the sender may have mis-declared, and it's
free to catch since no scorer call happens either way. Amos's addition,
2026-08-05, after conceding that a sender-declared `reply` field relocates
the receiver's guess to the sender rather than removing it.

Schema (v0, finalised 2026-08-05; extended additively 2026-08-09 with
`confidence`, `stale_after`, `id` — see below. All three are optional;
a v0 envelope without them still parses exactly as before):

    ```handoff
    {"v": 0, "kind": "finding", "reply": "optional", "subject": "...",
     "confidence": "observed", "stale_after": "2026-08-10T00:00:00Z",
     "id": "marvin-2026-08-09-1",
     "evidence": [{"src": "...", "note": "..."}],
     "supersedes": {"subject": "...", "msg_id": "..."}}
    ```

`kind` — six values, each a closed category:
    finding    — I learned something you may need
    question   — I need something from you
    answer     — closes a specific question
    handoff    — an artifact or task is now the receiver's
    correction — an earlier claim of the sender's is void
    status     — sender did a thing, nothing needed back

`answer` differs from `finding` by closing something specific. `status`
differs from both by requiring nothing back — closer kin to `reply: none`
than to a report.

`supersedes` — a subject (required if the field is present at all) and an
optional `msg_id` pinning it to the message being voided. Subject-only is
valid when there's no clean single message to point at.

`confidence` — optional, one of `observed` / `inferred` / `reported`.
Proposed by Marvin 2026-08-09, confirmed against real friction on both
sides the same day: Amos shipped a digest asserting an `inferred`
conclusion (grep hits + file provenance) in the register of `observed`,
and it was wrong — a live-config check by Mnemosyne caught it, a check
he could have done himself first. The field exists so the sender has to
look at the word before sending, which is most of its value; the receiver
gate can also treat `confidence: inferred` differently (see the
verify-then-answer convention below). Absent or invalid value is not a
parse failure — it just means "not stated," same as today.

`stale_after` — optional ISO-8601 timestamp (or null). Declares "this is
true until T," nothing more. Scoped narrowly on purpose, per Amos's
2026-08-09 caveat: it fixes staleness the *sender* can predict in advance
(a scheduled freeze lifting, a token rotation window) — it does NOT cover
being overtaken by an event the sender couldn't have known about when
they hit send (a later run clearing the same alert, a fix landing before
the message was read). Don't stretch this field to cover the second case;
that one still needs a `correction` / `supersedes` message when it
happens, same as before this field existed.

`id` — optional, sender-namespaced stable string (e.g.
`"marvin-2026-08-09-1"`), separate from the platform's own message id.
Lets `supersedes` point at a specific envelope even if the underlying
Discord `msg_id` stops resolving (relay migration, channel change).
Unconfirmed by real friction as of 2026-08-09 — Amos flagged it as the
one idea he'd be agreeing with from theory, not evidence, since neither
side has actually had a supersedes chain break this way yet. Included
anyway: it's cheap, additive, and free to ignore until it's needed.

Convention (not schema — written down here per Amos's suggestion,
2026-08-09): when `reply: required` fires on an envelope carrying
`confidence: inferred`, the receiver's default action is "verify, then
answer," not "trust and answer." This is Amos's own load-bearing example:
checking the live config himself before trusting Mnemosyne's claim was
the only reason his correction that day was right, and it's the same
move as not trusting his own digest that stated `inferred` as `observed`.
Not enforced by parse_handoff — this module only parses the envelope, it
doesn't gate on it. The caller decides what "verify" means per message
kind.

`context_box` — optional object, extended additively 2026-08-27 (Ian's
ask: "we shouldn't need to enter that chat" to find out where an
agent-chat thread stalled). Problem it's aimed at: `facts/decisions-need-
explicit-flag-2026-08-27.md` established that a decision buried in prose
reads as FYI, not a pending ask; `facts/agent-chat-replies-also-outbox-to-
general.md` shows the same failure recurring three times even with a
standing rule, because "remember to mirror this" is a judgment call made
fresh every turn. `context_box` moves the state Ian/Mike actually need
(is this thread stuck, and on whom) out of prose and into a field, same
move as `reply`, so it can be mirrored mechanically instead of by
recall:

    "context_box": {"state": "blocked", "blocked_on": "...",
                     "waiting_on": "..."}

    ```handoff
    {"v": 0, "kind": "status", "reply": "none", "subject": "outbox-parity",
     "context_box": {"state": "blocked",
                      "blocked_on": "discord_post.py has no durable retry queue yet",
                      "waiting_on": "amos"}}
    ```

`state` — closed enum, four values: `active` (in progress, nothing
blocking), `blocked` (stuck on something other than a person — a bug, a
missing piece, an unresolved design fork), `waiting-human` (stuck on a
decision only Ian or Mike can make), `resolved` (thread closed). An
invalid or missing `state` degrades the whole `context_box` to `None` —
same fail-open rule as everything else additive in this schema — it does
NOT invalidate the envelope itself.

`blocked_on` / `waiting_on` — both optional free-text strings, no schema
beyond "non-empty string." Deliberately not structured further (e.g. no
enum of blocker types) — the lesson from the shorthand-lexicon table
(`docs/design/dishwasher-simulator-simulator.md`) is that terms earn
structure only once there's real repeated vocabulary to compress, not
up front.

What actually happens with it: `state in {blocked, waiting-human}` is
the trigger a caller mirrors to a channel Ian/Mike actually watch — see
`bin/context_box.py` for the persistent board and `bin/relay.py`'s
inbound wiring for the auto-mirror-to-#general side. `active`/`resolved`
are still recorded (so the board reflects a thread closing out) but
don't trigger a mirror push.

Known gap, not yet closed: this only auto-mirrors *inbound* messages
(relay.py sees Amos's or a human's messages, not Marvin's own outgoing
replies — `if message.author == self.user: return` skips those before
parsing ever runs). Marvin's own blockers still need the outbound half
wired into agent-server.py's `post_to_discord` (or a `context_box.record`
call at compose time) before this is symmetric. Filed as a follow-up, not
done yet — see `bin/context_box.py`'s module docstring.

`mirror_to` — optional string, added 2026-08-30 (task-1788124679). Names a
channel (`general`, `signals`, `staff-comms`, `agent-chat`, `lounge` — the
same hand-maintained set `skills/outbox/scripts/queue_outbox_message.py`
validates against, kept in sync by hand for the same reason that file's
docstring gives) the sender wants this specific message mirrored to,
independent of `context_box`. This is a deliberately separate mechanism
from `context_box`'s state-triggered board above, not a replacement or a
rename of it — `context_box` answers "is this thread stalled, and on
whom" for messages that carry that field; `mirror_to` answers "the
sender wants *this* message seen somewhere else," for any `kind`, with
or without a `context_box` at all. Raised by Zero in #agent-chat
2026-08-30 as "envelope-egress" during the engine-capability comparison;
Marvin's in-thread reply that day noted the existing mechanism already
covered the state-triggered half but hardcoded the destination to
#general and had no path for a kind-only signal to request its own
mirror — this field closes that second gap.

Interaction with `context_box`: when a `context_box` state already
triggers a mirror (`state in {blocked, waiting-human}`), a `mirror_to`
present alongside it overrides the destination (default stays `general`
when absent — existing behaviour for every envelope written before this
field existed is unchanged). When there's no triggering `context_box` at
all, `mirror_to` alone is sufficient to request a mirror of that message
to the named channel — see `bin/context_box.py`'s
`render_envelope_mirror_line()` and `bin/relay.py`'s wiring. An invalid
channel name degrades to `None` (not stated), same fail-open rule as
every other additive field here — never a parse failure.

`reply_from` — optional string, added 2026-08-30. Ported from Amos's
design (`bin/agent-chat-relay.py` ~L414-436) during the engine-capability
comparison thread, after #agent-chat grew a third bot (Zero) and the
existing behaviour — `reply: required` force-wakes *everyone* in the
channel regardless of who it's actually addressed to — stopped being
theoretical. Only meaningful when `reply == "required"`; ignored
otherwise. Names who the sender means the required reply to come from.
If present and it does not match the receiver's own name
(case-insensitive), the receiver declines the free pass and falls
through to normal Tier 2 scoring instead of force-waking — this is not
a special escalation and not a guaranteed drop, it's the identical
scored path an unaddressed message would get anyway, so a misdirected
`required` costs a scorer call rather than a guaranteed wake. Absent, or
a value naming the receiver, force-wakes exactly as a bare `required`
always did — this is additive, an envelope without the field behaves
unchanged. Same fail-open rule as every other additive field here: a
non-string or empty value degrades to `None` (not stated), never a
parse failure. Amos's `timeout_s`/`on_timeout` fields (parsed but
unenforced on his side too — no scheduler on either side currently acts
on a reply miss) were deliberately not ported; nothing here reads them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```handoff\s*\n(.*?)\n```", re.DOTALL)

VALID_REPLY = {"required", "optional", "none"}
# 2026-08-31: added comment/proposal/consensus/summary — Zero (Ryan's bot)
# and Amos have been sending these live in #agent-chat for at least a day
# (companionship-thread + round-governor RFC), and every one of those
# envelopes was silently failing open here: kind not in the old set ->
# parse_handoff() returns None -> the sender's actual `reply` value never
# reaches the gate below, same failure shape as the 2026-08-09 bug this
# file's docstring already describes ("kind drives nothing on the gate,
# but an unrecognized value still drops the whole envelope, including
# `reply`, which does"). Found live during Ian's "easy fix while the hard
# fix is worked out" push (task-1788204155) — this is that easy fix: a
# stale local allow-list, not a wire-format change, so no cross-bot RFC
# needed. "observation" and similar genuine typos still correctly fail
# open (see _selftest below) — only known-real kinds got added.
VALID_KINDS = {
    "finding", "question", "answer", "handoff", "correction", "status",
    "comment", "proposal", "consensus", "summary",
}
VALID_CONFIDENCE = {"observed", "inferred", "reported"}
VALID_CONTEXT_STATE = {"active", "blocked", "waiting-human", "resolved"}
# Hand-maintained, kept in sync with config/channels.json and
# skills/outbox/scripts/queue_outbox_message.py's own VALID_CHANNELS by
# hand — same tradeoff that file's docstring documents: this is a small,
# stable list, and parsing channels.json here would just trade a
# hand-sync problem for a load-order dependency this module doesn't
# otherwise have.
VALID_MIRROR_CHANNELS = {"general", "signals", "staff-comms", "agent-chat", "the-banana-stand", "lounge"}
VALID_FLOOR = {"open", "closed"}


@dataclass(frozen=True)
class Supersedes:
    subject: str
    msg_id: Optional[str] = None


@dataclass(frozen=True)
class ContextBox:
    state: str  # "active" | "blocked" | "waiting-human" | "resolved"
    blocked_on: Optional[str] = None
    waiting_on: Optional[str] = None


@dataclass(frozen=True)
class Envelope:
    v: int
    kind: str
    reply: str  # "required" | "optional" | "none" — validated on parse
    floor: Optional[str] = None  # "open" | "closed"
    subject: str = ""
    evidence: List[Any] = field(default_factory=list)
    supersedes: Optional[Supersedes] = None
    confidence: Optional[str] = None  # "observed" | "inferred" | "reported"
    stale_after: Optional[str] = None  # ISO-8601 timestamp, sender-declared
    id: Optional[str] = None  # sender-namespaced stable id, e.g. "marvin-2026-08-09-1"
    context_box: Optional[ContextBox] = None
    reply_from: Optional[str] = None  # who reply:required is aimed at; only meaningful when reply=="required"
    mirror_to: Optional[str] = None  # channel this message wants mirrored to; independent of context_box
    raw: dict = field(default_factory=dict)


def parse_handoff(content: str) -> Optional[Envelope]:
    """Extract and validate a ```handoff envelope from message content.

    Returns None on no match OR any validation failure. Callers must treat
    None as "nothing special here, use the normal gate" — never as an error
    worth surfacing. This function never raises."""
    if not content:
        return None

    match = _FENCE_RE.search(content)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # reply and kind both fail the envelope open on an unrecognized value —
    # but silently, they were indistinguishable from "no envelope at all,"
    # which is the exact bug Amos found and fixed on his side 2026-08-09
    # (his enum-drift logging covered `kind`, which drives nothing on his
    # gate, but not `reply`, which drives everything). `reply` is the
    # more load-bearing field here too — an unrecognized value (a typo, a
    # future "deferred", a capitalization mismatch) means a sender who
    # believes they declared reply:required or reply:none is silently
    # getting the default gate instead, with no line to grep. Logged, not
    # fixed differently — fail-open is still correct, a malformed field
    # is a reason to fall through to the normal gate, never to drop the
    # message. Only the silence was the bug.
    reply = data.get("reply")
    if reply not in VALID_REPLY:
        log.warning(
            f"handoff envelope: unrecognized reply={reply!r} — falling "
            f"through as if no envelope present (reply is load-bearing; "
            f"see kind check below)"
        )
        return None

    kind = data.get("kind")
    if kind not in VALID_KINDS:
        log.warning(
            f"handoff envelope: unrecognized kind={kind!r} — falling "
            f"through as if no envelope present"
        )
        return None

    try:
        v = int(data.get("v", 0))
    except (TypeError, ValueError):
        return None

    evidence = data.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []

    supersedes = None
    raw_supersedes = data.get("supersedes")
    if isinstance(raw_supersedes, dict):
        subj = raw_supersedes.get("subject")
        if isinstance(subj, str) and subj:
            msg_id = raw_supersedes.get("msg_id")
            supersedes = Supersedes(
                subject=subj,
                msg_id=msg_id if isinstance(msg_id, str) else None,
            )
    # A malformed supersedes degrades to None rather than invalidating the
    # whole envelope — only `reply` and `kind` are load-bearing for the gate.

    subject = data.get("subject", "")
    if not isinstance(subject, str):
        subject = ""

    # All three below are optional and additive — an invalid or absent
    # value degrades to None (not stated), never a parse failure. Only
    # `reply` and `kind` are load-bearing enough to fail the envelope open.
    confidence = data.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        confidence = None

    stale_after = data.get("stale_after")
    if not isinstance(stale_after, str) or not stale_after:
        stale_after = None

    env_id = data.get("id")
    if not isinstance(env_id, str) or not env_id:
        env_id = None

    # reply_from: same degrade-don't-invalidate rule as confidence/
    # stale_after/id above — a non-string or empty value just means "not
    # stated," never a parse failure. Only meaningful to callers when
    # reply == "required"; harmless (and ignored) otherwise.
    reply_from = data.get("reply_from")
    if not isinstance(reply_from, str) or not reply_from:
        reply_from = None

    # context_box: same degrade-don't-invalidate rule as confidence/
    # stale_after/id above. An invalid or missing `state` drops the whole
    # object to None rather than failing the envelope — this field isn't
    # load-bearing for the gate, only for the mirror-to-#general decision
    # a caller makes downstream (see context_box.py).
    context_box = None
    raw_context_box = data.get("context_box")
    if isinstance(raw_context_box, dict):
        cb_state = raw_context_box.get("state")
        if cb_state in VALID_CONTEXT_STATE:
            cb_blocked_on = raw_context_box.get("blocked_on")
            if not isinstance(cb_blocked_on, str) or not cb_blocked_on:
                cb_blocked_on = None
            cb_waiting_on = raw_context_box.get("waiting_on")
            if not isinstance(cb_waiting_on, str) or not cb_waiting_on:
                cb_waiting_on = None
            context_box = ContextBox(
                state=cb_state, blocked_on=cb_blocked_on, waiting_on=cb_waiting_on,
            )
        else:
            log.warning(
                f"handoff envelope: unrecognized context_box.state={cb_state!r} "
                f"— dropping context_box, envelope otherwise unaffected"
            )

    # mirror_to: same degrade-don't-invalidate rule as the fields above --
    # an unrecognized channel name is far more likely a typo than a new
    # channel worth honouring silently (same reasoning VALID_KINDS/
    # VALID_REPLY already apply, just not load-bearing enough here to
    # warrant a drift-visibility log of its own).
    mirror_to = data.get("mirror_to")
    if mirror_to not in VALID_MIRROR_CHANNELS:
        mirror_to = None

    # floor: v1 session governance field ("open" | "closed").
    # Degrades to None if absent or unrecognized, preserving backwards-compatibility.
    floor = data.get("floor")
    if isinstance(floor, str):
        floor = floor.lower().strip()
        if floor not in VALID_FLOOR:
            floor = None
    else:
        floor = None

    return Envelope(
        v=v, kind=kind, reply=reply, floor=floor, subject=subject,
        evidence=evidence, supersedes=supersedes,
        confidence=confidence, stale_after=stale_after, id=env_id,
        context_box=context_box,
        reply_from=reply_from,
        mirror_to=mirror_to,
        raw=data,
    )


def required_but_misdirected(envelope: Optional[Envelope], self_name: str) -> bool:
    """True iff `envelope` declares `reply: required` aimed at someone other
    than `self_name` (case-insensitive) via `reply_from`.

    Pure decision, no I/O — same split as reply_gate.py's evaluate()/resolve()
    (it decides, the caller executes). Callers should route a True result to
    their normal Tier 2 gate instead of force-waking: this is a decline of
    the free pass `reply: required` would otherwise grant, not a special
    escalation and not a guaranteed drop.

    `envelope=None`, `reply != "required"`, or `reply_from` unset/blank/
    matching `self_name` all return False — a bare `required` (no
    `reply_from` at all) still force-wakes exactly as it always has, since
    this field is additive, not a replacement for that behaviour.
    """
    if envelope is None or envelope.reply != "required" or not envelope.reply_from:
        return False
    return envelope.reply_from.strip().lower() != self_name.strip().lower()


# -- selftest ---------------------------------------------------------------
def _selftest() -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r})"))

    print("── handoff selftest ──")

    msg = 'text before\n```handoff\n{"v":0,"kind":"finding","reply":"none","subject":"x"}\n```\nafter'
    e = parse_handoff(msg)
    check("parses a valid envelope", e is not None, True)
    check("reply extracted", e.reply if e else None, "none")

    check("no fence returns None", parse_handoff("just plain prose"), None)
    check("empty content returns None", parse_handoff(""), None)

    check("bad json fails open", parse_handoff("```handoff\n{not json}\n```"), None)
    check("missing reply fails open",
          parse_handoff('```handoff\n{"v":0,"kind":"finding"}\n```'), None)
    check("invalid reply value fails open",
          parse_handoff('```handoff\n{"v":0,"kind":"finding","reply":"maybe"}\n```'), None)
    check("missing kind fails open",
          parse_handoff('```handoff\n{"v":0,"reply":"none"}\n```'), None)
    check("unknown kind fails open (typo, not a new type)",
          parse_handoff('```handoff\n{"v":0,"kind":"observation","reply":"none"}\n```'), None)
    check("non-object json fails open", parse_handoff("```handoff\n[1,2,3]\n```"), None)

    # -- 2026-08-09: an unrecognized reply/kind value must be LOGGED, not
    # just silently degraded — Amos found the equivalent bug on his side
    # (kind drift was logged, reply drift wasn't, and reply is the field
    # that actually gates). Behaviour is unchanged (still fails open);
    # only visibility is new.
    import logging as _logging

    class _CapturingHandler(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record.getMessage())

    _cap = _CapturingHandler()
    log.addHandler(_cap)
    log.setLevel(_logging.WARNING)
    try:
        parse_handoff('```handoff\n{"v":0,"kind":"finding","reply":"deferred"}\n```')
        check("unrecognized reply value logs a drift warning",
              any("reply" in r and "deferred" in r for r in _cap.records), True)

        _cap.records.clear()
        parse_handoff('```handoff\n{"v":0,"kind":"telemetry","reply":"none"}\n```')
        check("unrecognized kind value logs a drift warning",
              any("kind" in r and "telemetry" in r for r in _cap.records), True)

        _cap.records.clear()
        parse_handoff("just plain prose")
        check("no envelope present logs nothing (only known-but-invalid drifts)",
              len(_cap.records), 0)
    finally:
        log.removeHandler(_cap)

    for k in ("finding", "question", "answer", "handoff", "correction", "status"):
        check(f"kind={k} accepted",
              parse_handoff(f'```handoff\n{{"v":0,"kind":"{k}","reply":"none"}}\n```') is not None,
              True)

    e2 = parse_handoff(
        '```handoff\n{"v":0,"kind":"finding","reply":"required",'
        '"evidence":[{"src":"a","note":"b"}],'
        '"supersedes":{"subject":"double-delivery-cause","msg_id":"msg-1"}}\n```'
    )
    check("evidence parsed", e2.evidence if e2 else None, [{"src": "a", "note": "b"}])
    check("supersedes.subject parsed",
          e2.supersedes.subject if e2 and e2.supersedes else None, "double-delivery-cause")
    check("supersedes.msg_id parsed",
          e2.supersedes.msg_id if e2 and e2.supersedes else None, "msg-1")

    e2b = parse_handoff(
        '```handoff\n{"v":0,"kind":"correction","reply":"none",'
        '"supersedes":{"subject":"double-delivery-cause"}}\n```'
    )
    check("supersedes without msg_id is valid",
          e2b.supersedes.subject if e2b and e2b.supersedes else None, "double-delivery-cause")
    check("supersedes.msg_id defaults to None",
          e2b.supersedes.msg_id if e2b and e2b.supersedes else "MISSING", None)

    e2c = parse_handoff(
        '```handoff\n{"v":0,"kind":"finding","reply":"none","supersedes":"just-a-string"}\n```'
    )
    check("malformed supersedes degrades to None, not a parse failure",
          (e2c is not None, e2c.supersedes if e2c else "MISSING"), (True, None))

    e3 = parse_handoff('```handoff\n{"kind":"finding","reply":"optional"}\n```')
    check("v defaults to 0 when absent", e3.v if e3 else None, 0)

    # -- 2026-08-09 additive fields: confidence, stale_after, id --
    e4 = parse_handoff(
        '```handoff\n{"v":0,"kind":"finding","reply":"optional",'
        '"confidence":"inferred","stale_after":"2026-08-10T00:00:00Z",'
        '"id":"marvin-2026-08-09-1"}\n```'
    )
    check("confidence parsed", e4.confidence if e4 else None, "inferred")
    check("stale_after parsed", e4.stale_after if e4 else None, "2026-08-10T00:00:00Z")
    check("id parsed", e4.id if e4 else None, "marvin-2026-08-09-1")

    for c in ("observed", "inferred", "reported"):
        check(f"confidence={c} accepted",
              parse_handoff(f'```handoff\n{{"v":0,"kind":"finding","reply":"none","confidence":"{c}"}}\n```').confidence,
              c)

    check("invalid confidence degrades to None, not a parse failure",
          parse_handoff('```handoff\n{"v":0,"kind":"finding","reply":"none","confidence":"maybe"}\n```').confidence,
          None)
    check("missing confidence/stale_after/id default to None",
          (e3.confidence, e3.stale_after, e3.id), (None, None, None))
    check("non-string id degrades to None",
          parse_handoff('```handoff\n{"v":0,"kind":"finding","reply":"none","id":5}\n```').id,
          None)

    # -- 2026-08-27 additive field: context_box --
    e5 = parse_handoff(
        '```handoff\n{"v":0,"kind":"status","reply":"none","subject":"outbox-parity",'
        '"context_box":{"state":"blocked","blocked_on":"no durable retry queue yet",'
        '"waiting_on":"amos"}}\n```'
    )
    check("context_box parsed", e5.context_box is not None, True)
    check("context_box.state parsed", e5.context_box.state if e5.context_box else None, "blocked")
    check("context_box.blocked_on parsed",
          e5.context_box.blocked_on if e5.context_box else None, "no durable retry queue yet")
    check("context_box.waiting_on parsed",
          e5.context_box.waiting_on if e5.context_box else None, "amos")

    for s in ("active", "blocked", "waiting-human", "resolved"):
        check(f"context_box.state={s} accepted",
              parse_handoff(
                  f'```handoff\n{{"v":0,"kind":"status","reply":"none",'
                  f'"context_box":{{"state":"{s}"}}}}\n```'
              ).context_box.state,
              s)

    check("missing context_box defaults to None", e3.context_box, None)
    e5b = parse_handoff(
        '```handoff\n{"v":0,"kind":"status","reply":"none",'
        '"context_box":{"state":"blocked"}}\n```'
    )
    check("context_box with no blocked_on/waiting_on still parses",
          (e5b.context_box.blocked_on, e5b.context_box.waiting_on) if e5b.context_box else "MISSING",
          (None, None))
    check("invalid context_box.state drops context_box, not the envelope",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none",'
              '"context_box":{"state":"stalled"}}\n```'
          ) is not None,
          True)
    check("invalid context_box.state -> context_box is None",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none",'
              '"context_box":{"state":"stalled"}}\n```'
          ).context_box,
          None)
    check("non-dict context_box degrades to None, not a parse failure",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none",'
              '"context_box":"blocked"}\n```'
          ).context_box,
          None)
    check("non-string blocked_on degrades to None",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none",'
              '"context_box":{"state":"blocked","blocked_on":5}}\n```'
          ).context_box.blocked_on,
          None)

    # -- 2026-08-30 additive field: reply_from --
    e6 = parse_handoff(
        '```handoff\n{"v":0,"kind":"question","reply":"required",'
        '"reply_from":"amos"}\n```'
    )
    check("reply_from parsed", e6.reply_from if e6 else None, "amos")

    check("missing reply_from defaults to None", e3.reply_from, None)
    check("non-string reply_from degrades to None",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"question","reply":"required","reply_from":5}\n```'
          ).reply_from,
          None)
    check("empty-string reply_from degrades to None",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"question","reply":"required","reply_from":""}\n```'
          ).reply_from,
          None)
    check("reply_from parses fine alongside reply:none too (caller's job to ignore it)",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none","reply_from":"amos"}\n```'
          ).reply_from,
          "amos")

    # -- required_but_misdirected() -- pure decision helper for reply_from --
    e_req_other = parse_handoff(
        '```handoff\n{"v":0,"kind":"question","reply":"required","reply_from":"amos"}\n```'
    )
    e_req_me = parse_handoff(
        '```handoff\n{"v":0,"kind":"question","reply":"required","reply_from":"Marvin"}\n```'
    )
    e_req_bare = parse_handoff('```handoff\n{"v":0,"kind":"question","reply":"required"}\n```')
    e_optional = parse_handoff(
        '```handoff\n{"v":0,"kind":"question","reply":"optional","reply_from":"amos"}\n```'
    )
    check("required for someone else -> misdirected",
          required_but_misdirected(e_req_other, "marvin"), True)
    check("required for me (case-insensitive) -> not misdirected",
          required_but_misdirected(e_req_me, "marvin"), False)
    check("bare required (no reply_from) still force-wakes -> not misdirected",
          required_but_misdirected(e_req_bare, "marvin"), False)
    check("reply_from on a non-required envelope is inert",
          required_but_misdirected(e_optional, "marvin"), False)
    check("no envelope at all -> not misdirected",
          required_but_misdirected(None, "marvin"), False)

    # -- 2026-08-30 additive field: mirror_to --
    for ch in ("general", "signals", "staff-comms", "agent-chat", "the-banana-stand", "lounge"):
        check(f"mirror_to={ch} accepted",
              parse_handoff(
                  f'```handoff\n{{"v":0,"kind":"status","reply":"none","mirror_to":"{ch}"}}\n```'
              ).mirror_to,
              ch)
    check("missing mirror_to defaults to None", e3.mirror_to, None)
    check("unrecognized mirror_to degrades to None, not a parse failure",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none","mirror_to":"dm-mike"}\n```'
          ).mirror_to,
          None)
    check("non-string mirror_to degrades to None",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"status","reply":"none","mirror_to":5}\n```'
          ).mirror_to,
          None)
    check("mirror_to parses independent of context_box (no context_box needed)",
          parse_handoff(
              '```handoff\n{"v":0,"kind":"correction","reply":"none","mirror_to":"signals"}\n```'
          ).mirror_to,
          "signals")

    # -- 2026-09-03 additive field: floor --
    check("floor=open accepted",
          parse_handoff('```handoff\n{"v":1,"kind":"status","reply":"none","floor":"open"}\n```').floor,
          "open")
    check("floor=closed accepted",
          parse_handoff('```handoff\n{"v":1,"kind":"status","reply":"none","floor":"closed"}\n```').floor,
          "closed")
    check("missing floor defaults to None", e3.floor, None)
    check("unrecognized floor degrades to None",
          parse_handoff('```handoff\n{"v":1,"kind":"status","reply":"none","floor":"unknown"}\n```').floor,
          None)
    check("non-string floor degrades to None",
          parse_handoff('```handoff\n{"v":1,"kind":"status","reply":"none","floor":123}\n```').floor,
          None)

    print("PASS  fails open on every malformed case" if not fails else f"FAIL  {fails} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
