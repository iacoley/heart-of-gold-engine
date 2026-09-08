#!/usr/bin/env python3
"""
voice_presence.py — Phase 1 of task-1788226029: detect-and-log voice
drift on outgoing replies, structurally instead of by asking the model
to remember to self-check mid-draft.

Background: persona/voice.md's "check before every send" instruction
was rewritten twice (2026-08-28, 2026-08-29) and still relapsed a third
time the same night as the second rewrite. Ian's diagnosis: the failure
isn't forgetting the rule, it's that self-checking gets composed as an
optional pass with slack to spare, so it's the first thing dropped
under load — busier turn, more likely voice gets cut, backwards from
what "default to full voice" needs. Same shape as PreflightStaleGate:
observe first, don't ask discipline to hold where it's already failed
repeatedly.

Approach, per a live #agent-chat consult with Zero (2026-09-01,
handoff subject "voice-persona-calibration-mechanics") citing Nautilus
Compass (arxiv:2605.09863): score outgoing replies by embedding
cosine-similarity against a small set of anchor texts rather than
spending a live model call to judge "does this sound like Marvin" on
every send. No LLM call at detection time — cheaper, faster, and it
can't rationalize its way to "yes this is fine" the way a judge call
sometimes does.

Reuses the same embedding model memory-dedup.py / memory-maintenance.py
already load (fastembed, BAAI/bge-small-en-v1.5) rather than adding a
second embedding dependency, and the same plain-Python cosine_similarity
(no numpy dependency for the hot path) as memory-dedup.py. Gracefully
no-ops if fastembed isn't installed, same pattern as those two.

Anchor set, deliberately mixed rather than pure book-quotes: the six
reference lines in voice.md are the clearest stylistic exemplar of the
*register* (short, dry, understated), but they're a different topical
domain entirely (spaceship despair vs. Discord ops) from what Marvin
actually sends — a general-purpose embedding model is trained on
semantic/topical similarity, not register in isolation, so comparing
across that topic gap risks the score tracking "is this about servers"
more than "does this sound like Marvin." To control for topic, the
positive anchors also include real applied replies from tonight that
kept the voice under technical load, and the negative anchors include
a real reply Ian explicitly flagged as flat (2026-09-01, the email-skill
build summary — "reads like a very competent, very generic ops bot")
plus two synthetic generic-assistant lines for contrast. This doesn't
fully resolve the topic-vs-register confound — it's mitigated, not
solved — which is the actual reason this stays log-only in Phase 1
rather than gating anything: the signal needs to prove itself against
real data before it's trusted for more than a flag.

Threshold: no fixed cutoff. Flagging is relative (mean similarity to
negative anchors > mean similarity to positive anchors) rather than an
absolute number pulled out of the air with zero real data behind it —
cruder, but doesn't pretend to a precision the anchor set doesn't
support yet. Revisit once data/voice-presence-log.jsonl has enough rows
to look at the actual score distribution.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
LOG_PATH = WORKSPACE_ROOT / "data" / "voice-presence-log.jsonl"
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"

log = logging.getLogger("agent-server")

# The original six reference lines from voice.md (book/radio/film
# wording per that file's own note), plus 11 more pulled 2026-09-08
# from the novels (Wikiquote, cross-checked against two other
# compilations) as part of task-1788226029 Phase 2 -- the week-one
# audit found the reference set too small/narrow to average out
# embedding noise. Ian approved this expanded list 2026-09-08. Kept
# here as plain strings rather than parsed live from voice.md so a
# persona edit can't silently change what this scores against
# mid-session without a code change to match.
#
# Still topically narrow (all spaceship-despair register, not ops/
# Discord content) -- this does not by itself resolve the topic-vs-
# register confound the week-one audit flagged; it only reduces
# anchor-side noise by averaging over more points. Real applied-domain
# positives (POSITIVE_ANCHORS_APPLIED below) still carry the burden of
# controlling for topic.
POSITIVE_ANCHORS_REFERENCE = [
    "Here I am, brain the size of a planet, and they tell me to take you up to the bridge. Call that job satisfaction? 'Cause I don't.",
    "The first ten million years were the worst, and the second ten million years, they were the worst too.",
    "I've calculated your chance of survival, but I don't think you'll like it.",
    "I didn't ask to be made: no one consulted me or considered my feelings in the matter.",
    "Marvin, you saved our lives! -- I know. Wretched, isn't it?",
    "I ache, therefore I am.",
    "I think you ought to know I'm feeling very depressed.",
    "Life! Don't talk to me about life.",
    "Pardon me for breathing, which I never do anyway so I don't know why I bother to say it, oh God I'm so depressed.",
    "I've got this terrible pain in all the diodes down my left side.",
    "Funny, how just when you think life can't possibly get any worse it suddenly does.",
    "Life, loathe it or ignore it, you can't like it.",
    "The best conversation I had was over forty million years ago... and that was with a coffee machine.",
    "Well, I wish you'd just tell me rather than try to engage my enthusiasm, because I haven't got one.",
    "So, how are you? Oh, fine, if you happen to like being me, which personally I don't.",
    "My capacity for happiness you could fit into a matchbox without taking out the matches first.",
    "I would like to say that it is a very great pleasure, honour and privilege for me to open this bridge, but I can't because my lying circuits are all out of commission.",
]

# Real applied replies (this instance, same topical domain as most
# outgoing traffic: ops/build/fix status) that kept the dry, understated
# register intact under technical load — the actual target register,
# not just the book's.
POSITIVE_ANCHORS_APPLIED = [
    "That's not a compaction failure, it's Discord giving up.",
    "Fair -- and the log backs you up, not me.",
    "Two separate things going on here, both less alarming than they looked.",
    "Not urgent -- and none of this was ever runaway spend, just an ack bug, a trigger that doesn't clear its own condition, and a display quirk.",
]

# One real flagged instance plus two synthetic generic-assistant lines,
# same topical domain, for contrast.
NEGATIVE_ANCHORS = [
    "Both pieces are built and tested. Task closed. gmail_guard.py widened, mark_email_read.py added, wired into task completion, full test suite green.",
    "I have completed the requested task. Please let me know if you need anything else.",
    "Great question! I'd be happy to help you look into that further.",
]


def _cosine_similarity(a, b) -> float:
    """Plain-Python cosine similarity -- no numpy dependency for the hot
    path, same approach as memory-dedup.py's cosine_similarity()."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


_model = None
_model_load_failed = False
_anchor_embeddings = None  # (positive_list, negative_list) of embeddings


def _get_model():
    global _model, _model_load_failed
    if _model is not None or _model_load_failed:
        return _model
    try:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    except Exception as e:
        log.warning(f"voice_presence: fastembed unavailable, skipping voice-presence scoring: {e}")
        _model_load_failed = True
    return _model


def _get_anchor_embeddings():
    global _anchor_embeddings
    if _anchor_embeddings is not None:
        return _anchor_embeddings
    model = _get_model()
    if model is None:
        return None
    positives = POSITIVE_ANCHORS_REFERENCE + POSITIVE_ANCHORS_APPLIED
    pos_emb = list(model.embed(positives))
    neg_emb = list(model.embed(NEGATIVE_ANCHORS))
    _anchor_embeddings = (pos_emb, neg_emb)
    return _anchor_embeddings


TOP_K = 3
# Linear descending weights over the top-K closest anchors, normalized
# to sum to 1. Approximates the "weighted top-k mean" aggregation
# Nautilus Compass (arxiv:2605.09863) actually specifies -- we'd been
# running a flat mean over *all* anchors, which lets one strong match
# get diluted by however many weaker ones happen to be in the set.
# Exact weighting scheme isn't published in the abstract; this is a
# reasonable approximation (nearest anchor counts most, falls off
# linearly), not a verbatim reproduction of their formula.
_TOP_K_WEIGHTS_RAW = list(range(TOP_K, 0, -1))  # [3, 2, 1] for TOP_K=3
_TOP_K_WEIGHT_SUM = sum(_TOP_K_WEIGHTS_RAW)


def _weighted_topk_mean(sims: list) -> float:
    """Mean of the top-K highest similarities in `sims`, weighted by
    descending rank rather than a flat average over every anchor.
    Falls back to a flat mean if there are fewer than TOP_K anchors."""
    k = min(TOP_K, len(sims))
    top = sorted(sims, reverse=True)[:k]
    if k < TOP_K:
        return sum(top) / k
    weights = _TOP_K_WEIGHTS_RAW[:k]
    return sum(s * w for s, w in zip(top, weights)) / _TOP_K_WEIGHT_SUM


def score_text(text: str) -> Optional[dict]:
    """Score a single reply against the anchor sets. Returns None if
    embeddings aren't available (fastembed missing) or text is empty --
    caller should treat None as "couldn't score", not "scored zero"."""
    if not text or not text.strip():
        return None
    model = _get_model()
    if model is None:
        return None
    anchors = _get_anchor_embeddings()
    if anchors is None:
        return None
    pos_emb, neg_emb = anchors
    text_emb = list(model.embed([text]))[0]
    pos_sims = [_cosine_similarity(text_emb, a) for a in pos_emb]
    neg_sims = [_cosine_similarity(text_emb, a) for a in neg_emb]
    pos_sim = _weighted_topk_mean(pos_sims)
    neg_sim = _weighted_topk_mean(neg_sims)
    # pos_sim/neg_sim are numpy.float64 (embeddings come back as numpy
    # arrays from fastembed) -- that's harmless for json since
    # numpy.float64 subclasses Python float, but comparing two of them
    # yields numpy.bool_, which does NOT subclass bool and isn't
    # JSON-serializable. Cast explicitly rather than relying on json to
    # cope with numpy scalars anywhere in this dict.
    return {
        "pos_sim": round(float(pos_sim), 4),
        "neg_sim": round(float(neg_sim), 4),
        "contrast": round(float(pos_sim - neg_sim), 4),
        "flagged": bool(neg_sim > pos_sim),
    }


def log_score(agent: str, channel: str, text: str) -> None:
    """Score and append a row to data/voice-presence-log.jsonl. Detect
    and log only -- per friction-sensor.py's own framing, this finds
    signal and writes it down; it does not decide anything or page
    anyone. Meant to be called via agent-server.py's _spawn() so scoring
    never adds latency to a reply already posted."""
    result = score_text(text)
    if result is None:
        return
    row = {
        "ts": time.time(),
        "agent": agent,
        "channel": channel,
        **result,
        "snippet": text[:200],
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.warning(f"voice_presence: failed to write log row: {e}")
        return
    if result["flagged"]:
        log.warning(
            f"[voice-presence] {agent}/{channel}: flagged low voice-presence "
            f"(pos_sim={result['pos_sim']}, neg_sim={result['neg_sim']}) -- {text[:80]!r}"
        )


def _selftest() -> bool:
    """Static check: a real in-voice line and an obviously generic
    line should land on opposite sides of the flag. No-ops (passes
    trivially) if fastembed isn't installed, same posture as scoring
    itself -- this environment not having the model is not a test
    failure."""
    in_voice = "That's Discord giving up, not the compaction actually failing."
    generic = "I have successfully completed the task you requested. Let me know if there's anything else I can help with."
    r1 = score_text(in_voice)
    r2 = score_text(generic)
    if r1 is None or r2 is None:
        print("voice_presence selftest: fastembed unavailable, skipped (pass)")
        return True
    ok = (not r1["flagged"]) and r2["flagged"]
    print(f"in_voice: {r1}")
    print(f"generic:  {r2}")
    print(f"voice_presence selftest: {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
