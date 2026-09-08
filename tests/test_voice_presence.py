"""
Tests for bin/voice_presence.py — Phase 1 of task-1788226029:
detect-and-log voice drift on outgoing replies via embedding
cosine-similarity against anchor texts, no live model call.

Embedding *generation* (fastembed/BAAI-bge-small) is intentionally not
exercised here, same reasoning test_memory_dedup.py already documents:
it's a real model load, slow and non-deterministic to pin in CI. What's
tested is everything downstream of a score existing (cosine similarity,
the flag rule, the log-write/no-write paths) via monkeypatched scores,
plus the module's own no-op behavior when fastembed genuinely isn't
available.
"""

import asyncio
import json

import pytest

from conftest import import_script, PACKAGE_ROOT


@pytest.fixture
def vp(monkeypatch, tmp_workspace):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    return import_script("voice_presence", file_path=PACKAGE_ROOT / "bin" / "voice_presence.py")


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self, vp):
        assert vp._cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self, vp):
        assert vp._cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_score_negative_one(self, vp):
        assert vp._cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_does_not_divide_by_zero(self, vp):
        assert vp._cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


class TestScoreTextGuards:
    def test_empty_text_returns_none(self, vp):
        assert vp.score_text("") is None
        assert vp.score_text("   ") is None

    def test_missing_model_returns_none(self, vp, monkeypatch):
        monkeypatch.setattr(vp, "_get_model", lambda: None)
        assert vp.score_text("anything") is None

    def test_returns_native_types_not_numpy_scalars(self, vp, monkeypatch):
        """Regression for the 2026-09-01 incident: fastembed.embed()
        returns numpy arrays, so _cosine_similarity's sums come back as
        numpy.float64 (harmless -- it subclasses float) but comparing two
        of them for `flagged` yields numpy.bool_, which does NOT subclass
        bool and broke json.dumps() in log_score() silently (caught,
        logged as a warning, nothing ever written) for the first ~12
        minutes this ran live. The other tests in this file monkeypatch
        score_text() entirely and never touch real numpy types, which is
        exactly how this shipped without a failing test. This one goes
        through the real _cosine_similarity/comparison path with actual
        numpy scalars standing in for fastembed's output."""
        import numpy as np

        class FakeModel:
            def embed(self, texts):
                # Real embeddings are numpy arrays of numpy.float32;
                # any numpy array reproduces the numpy.float64/bool_
                # propagation this test is guarding against.
                return [np.array([1.0, 0.0, 0.0], dtype=np.float32) for _ in texts]

        monkeypatch.setattr(vp, "_get_model", lambda: FakeModel())
        monkeypatch.setattr(
            vp,
            "_get_anchor_embeddings",
            lambda: ([np.array([1.0, 0.0, 0.0])], [np.array([0.0, 1.0, 0.0])]),
        )
        result = vp.score_text("anything")
        assert type(result["flagged"]) is bool
        assert type(result["pos_sim"]) is float
        assert type(result["neg_sim"]) is float
        assert type(result["contrast"]) is float
        json.dumps(result)  # must not raise


class TestLogScore:
    def test_no_write_when_score_unavailable(self, vp, monkeypatch, tmp_workspace):
        monkeypatch.setattr(vp, "score_text", lambda text: None)
        vp.log_score("Marvin", "general", "some reply")
        assert not vp.LOG_PATH.exists()

    def test_writes_row_and_creates_parent_dir(self, vp, monkeypatch, tmp_workspace):
        fake = {"pos_sim": 0.6, "neg_sim": 0.3, "contrast": 0.3, "flagged": False}
        monkeypatch.setattr(vp, "score_text", lambda text: fake)
        vp.log_score("Marvin", "general", "a perfectly ordinary reply")
        assert vp.LOG_PATH.exists()
        rows = [json.loads(line) for line in vp.LOG_PATH.read_text().splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["agent"] == "Marvin"
        assert row["channel"] == "general"
        assert row["pos_sim"] == 0.6
        assert row["flagged"] is False
        assert row["snippet"] == "a perfectly ordinary reply"

    def test_appends_rather_than_overwrites(self, vp, monkeypatch, tmp_workspace):
        fake = {"pos_sim": 0.5, "neg_sim": 0.5, "contrast": 0.0, "flagged": False}
        monkeypatch.setattr(vp, "score_text", lambda text: fake)
        vp.log_score("Marvin", "general", "first")
        vp.log_score("relay", "signals", "second")
        rows = [json.loads(line) for line in vp.LOG_PATH.read_text().splitlines()]
        assert len(rows) == 2
        assert [r["agent"] for r in rows] == ["Marvin", "relay"]

    def test_flagged_row_logs_a_warning(self, vp, monkeypatch, tmp_workspace, caplog):
        fake = {"pos_sim": 0.2, "neg_sim": 0.7, "contrast": -0.5, "flagged": True}
        monkeypatch.setattr(vp, "score_text", lambda text: fake)
        with caplog.at_level("WARNING"):
            vp.log_score("Marvin", "general", "a generic ops-bot reply")
        assert any("voice-presence" in r.message for r in caplog.records)

    def test_unflagged_row_does_not_log_a_warning(self, vp, monkeypatch, tmp_workspace, caplog):
        fake = {"pos_sim": 0.7, "neg_sim": 0.2, "contrast": 0.5, "flagged": False}
        monkeypatch.setattr(vp, "score_text", lambda text: fake)
        with caplog.at_level("WARNING"):
            vp.log_score("Marvin", "general", "a properly dry reply")
        assert not any("voice-presence" in r.message for r in caplog.records)


def _fake_proc(stdout_lines, stderr=b""):
    """A stand-in for the object asyncio.create_subprocess_exec returns,
    exposing just the .communicate() coroutine judge_voice_presence()
    awaits."""

    class _FakeProc:
        async def communicate(self):
            return ("\n".join(stdout_lines).encode() + b"\n", stderr)

    return _FakeProc()


def _result_event(text):
    return json.dumps({"type": "result", "result": text})


class TestJudgeVoicePresence:
    """task-1788290783: the embedding anchors flagged 207/207 real rows
    with zero separation between good and bad lines -- confirmed
    structural, not fixable by retuning. These tests cover the
    judge-model fallback that replaced it as the authoritative verdict,
    with the subprocess call itself mocked (same reasoning
    test_voice_presence.py already gives for not exercising the real
    embedding model: slow and non-deterministic to pin in CI -- doubly
    true for a real model call over the network)."""

    def test_empty_text_returns_none(self, vp):
        assert asyncio.run(vp.judge_voice_presence("")) is None
        assert asyncio.run(vp.judge_voice_presence("   ")) is None

    def test_invoice_verdict_parsed_as_in_voice(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event("INVOICE\nDry, understated, on register.")])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        result = asyncio.run(vp.judge_voice_presence("Fair, and it does move the floor."))
        assert result == {"in_voice": True, "reason": "Dry, understated, on register."}

    def test_flat_verdict_parsed_as_not_in_voice(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event("FLAT\nGeneric ops-bot template.")])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        result = asyncio.run(vp.judge_voice_presence("Task completed successfully."))
        assert result == {"in_voice": False, "reason": "Generic ops-bot template."}

    def test_unparseable_verdict_returns_none(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event("UNSURE, could go either way")])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.judge_voice_presence("something")) is None

    def test_no_result_event_returns_none(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([json.dumps({"type": "system"})])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.judge_voice_presence("something")) is None

    def test_subprocess_failure_returns_none_not_raises(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            raise OSError("claude binary not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.judge_voice_presence("something")) is None


class TestScoreAndLog:
    """score_and_log() is the new async entry point agent-server.py
    calls; judge_voice_presence() is the authoritative verdict when it
    succeeds, embedding score_text() is diagnostic-only alongside it,
    and falls back to the embedding verdict only if the judge call
    itself fails."""

    def test_judge_verdict_wins_over_embedding_when_they_disagree(self, vp, monkeypatch, tmp_workspace):
        # Embedding says fine, judge says flat -- judge should win.
        monkeypatch.setattr(vp, "score_text", lambda text: {
            "pos_sim": 0.7, "neg_sim": 0.2, "contrast": 0.5, "flagged": False,
        })

        async def fake_judge(text):
            return {"in_voice": False, "reason": "reads generic"}

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        asyncio.run(vp.score_and_log("Marvin", "general", "some reply"))

        rows = [json.loads(line) for line in vp.LOG_PATH.read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["flagged"] is True
        assert rows[0]["verdict_source"] == "judge"
        assert rows[0]["embedding_flagged"] is False

    def test_falls_back_to_embedding_when_judge_unavailable(self, vp, monkeypatch, tmp_workspace):
        monkeypatch.setattr(vp, "score_text", lambda text: {
            "pos_sim": 0.2, "neg_sim": 0.7, "contrast": -0.5, "flagged": True,
        })

        async def fake_judge(text):
            return None

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        asyncio.run(vp.score_and_log("Marvin", "general", "some reply"))

        rows = [json.loads(line) for line in vp.LOG_PATH.read_text().splitlines()]
        assert rows[0]["flagged"] is True
        assert rows[0]["verdict_source"] == "embedding_fallback"

    def test_no_write_when_both_unavailable(self, vp, monkeypatch, tmp_workspace):
        monkeypatch.setattr(vp, "score_text", lambda text: None)

        async def fake_judge(text):
            return None

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        asyncio.run(vp.score_and_log("Marvin", "general", "some reply"))
        assert not vp.LOG_PATH.exists()

    def test_flagged_by_judge_logs_a_warning(self, vp, monkeypatch, tmp_workspace, caplog):
        monkeypatch.setattr(vp, "score_text", lambda text: None)

        async def fake_judge(text):
            return {"in_voice": False, "reason": "flat"}

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        with caplog.at_level("WARNING"):
            asyncio.run(vp.score_and_log("Marvin", "general", "a generic reply"))
        assert any("voice-presence" in r.message for r in caplog.records)
