"""bin/pass_filter.py — shared PASS-sentinel detection.

Incident 2026-09-28: a backed-up outbox queue flushed ~50 literal/
near-literal "PASS" posts to Discord once posting permission was
restored. is_pass_sentinel() is the single rule both agent-server.py's
direct post_to_discord() path and outbox.py's flush_pending() path use.
"""

from conftest import import_script

pass_filter = import_script("pass_filter")
is_pass_sentinel = pass_filter.is_pass_sentinel


def test_exact_pass():
    assert is_pass_sentinel("PASS")


def test_pass_with_trailing_punctuation():
    assert is_pass_sentinel("PASS.")
    assert is_pass_sentinel("PASS!")
    assert is_pass_sentinel("PASS?")


def test_pass_leading_whole_token_with_trailing_prose():
    assert is_pass_sentinel("PASS — nothing new")


# The three real variants from the 2026-09-28 incident report.
REAL_INCIDENT_VARIANTS = [
    "Same as last round — nothing's changed, nothing needs saying again.\n\nPASS",
    "Already flagged and resolved with Zero last round. Nothing new here.PASS",
    "Healthy, inbox empty, nothing new since the last check. PASS",
]


def test_real_incident_variants_are_suppressed():
    for text in REAL_INCIDENT_VARIANTS:
        assert is_pass_sentinel(text), f"should suppress: {text!r}"


def test_markdown_emphasis_wrapped_pass():
    assert is_pass_sentinel("*PASS*")
    assert is_pass_sentinel("**PASS**")
    assert is_pass_sentinel("_PASS_")
    assert is_pass_sentinel("`PASS`")
    assert is_pass_sentinel("**_PASS_**")


def test_whitespace_only_and_empty_not_suppressed():
    assert not is_pass_sentinel("")
    assert not is_pass_sentinel("   ")
    assert not is_pass_sentinel(None)


def test_lowercase_and_partial_words_not_suppressed():
    assert not is_pass_sentinel("pass")
    assert not is_pass_sentinel("Pass")
    assert not is_pass_sentinel("PASSWORD reset needed for the dashboard login.")
    assert not is_pass_sentinel("Already passed the test suite, ready to merge.")
    assert not is_pass_sentinel("Traffic can bypass the cache on this route.")
    assert not is_pass_sentinel("Compass heading holds steady at 90 degrees.")


def test_pass_mid_sentence_not_suppressed():
    assert not is_pass_sentinel(
        "The gate said PASS but then kept going with more detail after that."
    )
    assert not is_pass_sentinel(
        "It's not a PASS situation, there's real follow-up needed here."
    )


def test_real_answers_not_suppressed():
    real_answers = [
        "The fencing token is now live in production, tested end to end.",
        "Yes, that PR is merged.",
        "Not sure that's right — the ceiling is 90 seconds, not 10 minutes.",
    ]
    for text in real_answers:
        assert not is_pass_sentinel(text), f"should NOT suppress: {text!r}"
