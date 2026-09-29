"""outbox.py — durable queue for cross-channel relays.

Every turn is pre-scoped to one Discord channel. Without a durable queue,
"Marvin owes #general a message" was a mental note that evaporated at
end-of-turn if the next turn wasn't scoped there. These tests exercise the
queue/flush cycle against a temp file, with discord-notify.sh delivery
mocked out (no real network calls).
"""

import json
import subprocess

import pytest

from conftest import import_script


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    mod = import_script("outbox")
    mod.OUTBOX_PATH = tmp_path / "data" / "outbox" / "pending.jsonl"
    return mod


def test_add_pending_creates_row(outbox):
    row_id = outbox.add_pending("general", "hello")
    rows = outbox._load_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == row_id
    assert rows[0]["channel"] == "general"
    assert rows[0]["content"] == "hello"
    assert rows[0]["delivered_at"] is None


def test_add_pending_appends_not_overwrites(outbox):
    outbox.add_pending("general", "first")
    outbox.add_pending("signals", "second")
    rows = outbox._load_rows()
    assert len(rows) == 2
    assert [r["content"] for r in rows] == ["first", "second"]


def test_flush_pending_delivers_and_marks(outbox, monkeypatch):
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    outbox.add_pending("general", "queued while scoped elsewhere")
    delivered = outbox.flush_pending()

    assert len(delivered) == 1
    assert calls[0][1:] == [str(outbox.NOTIFY_SCRIPT), "general", "queued while scoped elsewhere"] or \
        calls[0] == [str(outbox.NOTIFY_SCRIPT), "general", "queued while scoped elsewhere"]

    rows = outbox._load_rows()
    assert rows[0]["delivered_at"] is not None


def test_flush_pending_skips_already_delivered(outbox, monkeypatch):
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    outbox.add_pending("general", "one")
    outbox.flush_pending()
    assert len(calls) == 1

    # Second flush with nothing new pending should make no delivery calls.
    delivered = outbox.flush_pending()
    assert delivered == []
    assert len(calls) == 1


def test_flush_pending_leaves_failed_rows_undelivered(outbox, monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        raise subprocess.CalledProcessError(1, cmd, stderr="network down")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    outbox.add_pending("general", "will fail")
    delivered = outbox.flush_pending()

    assert delivered == []
    rows = outbox._load_rows()
    assert rows[0]["delivered_at"] is None


def test_flush_pending_retries_after_transient_failure(outbox, monkeypatch):
    state = {"calls": 0}

    def flaky_run(cmd, check, capture_output, text):
        state["calls"] += 1
        if state["calls"] == 1:
            raise subprocess.CalledProcessError(1, cmd, stderr="network down")
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", flaky_run)

    outbox.add_pending("general", "retry me")
    first = outbox.flush_pending()
    assert first == []

    second = outbox.flush_pending()
    assert len(second) == 1
    rows = outbox._load_rows()
    assert rows[0]["delivered_at"] is not None


def test_multiple_rows_only_undelivered_are_flushed(outbox, monkeypatch):
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd[1])  # channel arg
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    outbox.add_pending("general", "a")
    outbox.flush_pending()
    outbox.add_pending("signals", "b")
    outbox.flush_pending()

    assert calls == ["general", "signals"]


# PASS-sentinel guard (2026-09-28 incident): flush_pending() is a second,
# independent send path from agent-server.py's post_to_discord() — a
# backed-up outbox queue flushed ~50 literal/near-literal "PASS" messages
# to Discord with no screening at all. These tests exercise the guard
# added at the top of the flush loop, before discord-notify.sh is ever
# invoked.

def test_flush_pending_drops_exact_pass(outbox, monkeypatch):
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    outbox.add_pending("general", "PASS")
    delivered = outbox.flush_pending()

    assert calls == []  # discord-notify.sh never invoked
    assert delivered == []  # not counted as a real delivery
    rows = outbox._load_rows()
    assert rows[0]["delivered_at"] is not None  # but marked done, not retried
    assert rows[0]["dropped"] is True
    assert rows[0]["drop_reason"] == "pass_sentinel"


def test_flush_pending_drops_pass_with_punctuation(outbox, monkeypatch):
    monkeypatch.setattr(
        outbox.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="Posted", stderr=""),
    )
    outbox.add_pending("general", "PASS.")
    outbox.flush_pending()
    assert outbox._load_rows()[0]["dropped"] is True


def test_flush_pending_drops_real_incident_variants(outbox, monkeypatch):
    calls = []
    monkeypatch.setattr(
        outbox.subprocess, "run",
        lambda cmd, check, capture_output, text: (calls.append(cmd) or
            subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")),
    )
    variants = [
        "Same as last round — nothing's changed, nothing needs saying again.\n\nPASS",
        "Already flagged and resolved with Zero last round. Nothing new here.PASS",
        "Healthy, inbox empty, nothing new since the last check. PASS",
    ]
    for text in variants:
        outbox.add_pending("general", text)
    outbox.flush_pending()

    assert calls == []
    rows = outbox._load_rows()
    assert all(r["dropped"] for r in rows)


def test_flush_pending_does_not_drop_lowercase_or_partial_pass_words(outbox, monkeypatch):
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    outbox.add_pending("general", "PASSWORD reset needed for the dashboard login.")
    outbox.add_pending("general", "Already passed the test suite, ready to merge.")
    outbox.add_pending("general", "Traffic can bypass the cache on this route.")
    delivered = outbox.flush_pending()

    assert len(delivered) == 3
    assert len(calls) == 3
    rows = outbox._load_rows()
    assert not any(r.get("dropped") for r in rows)


def test_flush_pending_does_not_drop_pass_mid_sentence(outbox, monkeypatch):
    monkeypatch.setattr(
        outbox.subprocess, "run",
        lambda cmd, check, capture_output, text: subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr=""),
    )
    outbox.add_pending(
        "general",
        "The gate said PASS but then kept going with more detail after that.",
    )
    delivered = outbox.flush_pending()
    assert len(delivered) == 1
    assert outbox._load_rows()[0].get("dropped") is not True


def test_flush_pending_evaluates_whole_reply_before_chunking(outbox, monkeypatch):
    """A message long enough to be split into multiple 2000-char chunks
    must still be caught by the PASS guard, evaluated against the whole
    unchunked reply, not per-chunk."""
    calls = []

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Posted", stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", fake_run)

    long_prose = ("Nothing new since last check. " * 100).strip()
    content = f"{long_prose}\n\nPASS"
    assert len(content) > outbox.MAX_DISCORD_MSG_LEN

    outbox.add_pending("general", content)
    delivered = outbox.flush_pending()

    assert calls == []
    assert delivered == []
    assert outbox._load_rows()[0]["dropped"] is True
