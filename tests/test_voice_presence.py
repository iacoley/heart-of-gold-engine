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


class TestJudgeVoicePresenceAuthGuard:
    """2026-09-14 shared-OAuth-rotation scar: this judge call is one of
    the sidecar `claude -p` spawns that dies when a sibling session
    rotates the shared on-disk token. Verifies the auth_guard wiring
    added to stop it from hammering a known-dead token."""

    def test_skips_spawn_entirely_when_auth_known_bad(self, vp, monkeypatch):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            raise AssertionError("should not have spawned a process")

        monkeypatch.setattr(vp.auth_guard, "should_attempt", lambda: False)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.judge_voice_presence("something")) is None
        assert calls == []

    def test_signature_in_output_records_failure_and_returns_none(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event(
                "Failed to authenticate: OAuth session expired and could not be refreshed"
            )])

        recorded = []
        monkeypatch.setattr(vp.auth_guard, "should_attempt", lambda: True)
        monkeypatch.setattr(vp.auth_guard, "record_failure", lambda: recorded.append(True))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.judge_voice_presence("something")) is None
        assert recorded == [True]

    def test_real_verdict_records_success(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event("INVOICE\nDry, understated, on register.")])

        recorded = []
        monkeypatch.setattr(vp.auth_guard, "should_attempt", lambda: True)
        monkeypatch.setattr(vp.auth_guard, "record_success", lambda: recorded.append(True))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        result = asyncio.run(vp.judge_voice_presence("Fair, and it does move the floor."))
        assert result == {"in_voice": True, "reason": "Dry, understated, on register."}
        assert recorded == [True]


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


class TestRewriteForVoice:
    """task-1788316504's one-shot rewrite attempt for a reply
    judge_voice_presence() flagged flat. Same mocked-subprocess approach
    as TestJudgeVoicePresence -- no real model call in CI."""

    def test_empty_text_returns_none(self, vp):
        assert asyncio.run(vp.rewrite_for_voice("", "flat")) is None
        assert asyncio.run(vp.rewrite_for_voice("   ", "flat")) is None

    def test_successful_rewrite_returns_text(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event("That's not a bug, that's Tuesday.")])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        result = asyncio.run(vp.rewrite_for_voice("The task completed successfully.", "generic"))
        assert result == "That's not a bug, that's Tuesday."

    def test_no_result_event_returns_none(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([json.dumps({"type": "system"})])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.rewrite_for_voice("something", "generic")) is None

    def test_subprocess_failure_returns_none_not_raises(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            raise OSError("claude binary not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.rewrite_for_voice("something", "generic")) is None

    def test_skips_spawn_entirely_when_auth_known_bad(self, vp, monkeypatch):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            raise AssertionError("should not have spawned a process")

        monkeypatch.setattr(vp.auth_guard, "should_attempt", lambda: False)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.rewrite_for_voice("something", "generic")) is None
        assert calls == []

    def test_auth_failure_signature_records_failure_and_returns_none(self, vp, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _fake_proc([_result_event(
                "Failed to authenticate: OAuth session expired and could not be refreshed"
            )])

        recorded = []
        monkeypatch.setattr(vp.auth_guard, "should_attempt", lambda: True)
        monkeypatch.setattr(vp.auth_guard, "record_failure", lambda: recorded.append(True))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert asyncio.run(vp.rewrite_for_voice("something", "generic")) is None
        assert recorded == [True]


class TestGateAndRewrite:
    """gate_and_rewrite() is the function agent-server.py's blocking
    pre-send gate actually calls: judge, and if flagged flat, exactly
    one rewrite attempt -- never a loop, never a second judge call on
    the rewrite itself (task-1788316504's scoping note)."""

    def test_in_voice_passes_through_unchanged(self, vp, monkeypatch):
        async def fake_judge(text):
            return {"in_voice": True, "reason": "dry, on register"}

        async def fake_rewrite(text, reason):
            raise AssertionError("should not attempt a rewrite when already in voice")

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        monkeypatch.setattr(vp, "rewrite_for_voice", fake_rewrite)
        text, meta = asyncio.run(vp.gate_and_rewrite("Fair, and it does move the floor."))
        assert text == "Fair, and it does move the floor."
        assert meta["gate_action"] == "passed"

    def test_unjudged_passes_through_unchanged(self, vp, monkeypatch):
        async def fake_judge(text):
            return None

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        text, meta = asyncio.run(vp.gate_and_rewrite("some reply"))
        assert text == "some reply"
        assert meta["gate_action"] == "skipped_unjudged"

    def test_flagged_flat_returns_successful_rewrite(self, vp, monkeypatch):
        async def fake_judge(text):
            return {"in_voice": False, "reason": "reads generic"}

        async def fake_rewrite(text, reason):
            assert reason == "reads generic"
            return "Wretched, isn't it."

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        monkeypatch.setattr(vp, "rewrite_for_voice", fake_rewrite)
        text, meta = asyncio.run(vp.gate_and_rewrite("Task completed successfully."))
        assert text == "Wretched, isn't it."
        assert meta["gate_action"] == "rewritten"
        assert meta["original_reason"] == "reads generic"

    def test_flagged_flat_falls_back_to_original_when_rewrite_fails(self, vp, monkeypatch):
        async def fake_judge(text):
            return {"in_voice": False, "reason": "reads generic"}

        async def fake_rewrite(text, reason):
            return None

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        monkeypatch.setattr(vp, "rewrite_for_voice", fake_rewrite)
        text, meta = asyncio.run(vp.gate_and_rewrite("Task completed successfully."))
        assert text == "Task completed successfully."
        assert meta["gate_action"] == "rewrite_failed"


class TestProtectSpans:
    """task-1790988780: mentions/code/URLs must survive the rewrite."""

    @pytest.mark.parametrize("span", [
        "<@111>", "<@!111>", "<@&222>", "<#333>", "<:blob:444>", "<a:spin:444>",
        "<t:1700000000>", "<t:1700000000:R>",
        "`inline code`", "```\nblock\n```", "```handoff\n{\"a\": 1}\n```",
        "https://example.com/a?b=c", "<https://example.com/x>",
    ])
    def test_each_span_type_round_trips(self, vp, span):
        text = f"before {span} after"
        tok, spans = vp.protect_spans(text)
        assert spans == [span]
        assert span not in tok
        assert vp.restore_spans(tok, spans) == text

    def test_url_trailing_punctuation_not_swallowed(self, vp):
        tok, spans = vp.protect_spans("see https://example.com/x.")
        assert spans == ["https://example.com/x"]
        assert tok.endswith("⟦P0⟧.")

    def test_url_inside_code_is_one_span(self, vp):
        _, spans = vp.protect_spans("`curl https://example.com` ok")
        assert spans == ["`curl https://example.com`"]

    def test_restore_rejects_missing_duplicate_unknown(self, vp):
        spans = ["<@1>", "<@2>"]
        assert vp.restore_spans("⟦P0⟧ ⟦P1⟧", spans) == "<@1> <@2>"
        assert vp.restore_spans("⟦P0⟧", spans) is None
        assert vp.restore_spans("⟦P0⟧ ⟦P0⟧ ⟦P1⟧", spans) is None
        assert vp.restore_spans("⟦P0⟧ ⟦P1⟧ ⟦P2⟧", spans) is None


def _patch_model(monkeypatch, reply, captured=None):
    async def fake_exec(*args, **kwargs):
        if captured is not None:
            captured.append(args)
        return _fake_proc([_result_event(reply)])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


class TestRewriteProtection:
    def test_successful_rewrite_restores_mentions(self, vp, monkeypatch):
        captured = []
        _patch_model(monkeypatch, "⟦P0⟧, it is done. Of course it is. See ⟦P1⟧.", captured)
        orig = "<@111> the job finished. See https://example.com/r"
        out = asyncio.run(vp.rewrite_for_voice(orig, "flat"))
        assert out == "<@111>, it is done. Of course it is. See https://example.com/r."
        prompt = captured[0][2]
        assert "<@111>" not in prompt and "⟦P0⟧" in prompt

    @pytest.mark.parametrize("bad", ["@<user2>", "<@REDACTED>"])
    def test_observed_mangled_mentions_fall_back(self, vp, monkeypatch, bad):
        _patch_model(monkeypatch, f"Hello {bad}, done.")
        orig = "Hello <@1468012353206354197>, task done."
        assert asyncio.run(vp.rewrite_for_voice(orig, "flat")) is None

    def test_model_inventing_mention_rejected(self, vp, monkeypatch):
        _patch_model(monkeypatch, "⟦P0⟧ done <@999>")
        assert asyncio.run(vp.rewrite_for_voice("<@111> done", "flat")) is None

    def test_dropped_placeholder_rejected(self, vp, monkeypatch):
        _patch_model(monkeypatch, "done, I suppose.")
        assert asyncio.run(vp.rewrite_for_voice("<@111> done", "flat")) is None

    def test_duplicated_placeholder_rejected(self, vp, monkeypatch):
        _patch_model(monkeypatch, "⟦P0⟧ ⟦P0⟧ done")
        assert asyncio.run(vp.rewrite_for_voice("<@111> done", "flat")) is None

    def test_handoff_block_preserved_byte_for_byte(self, vp, monkeypatch):
        block = '```handoff\n{"to": "zero",  "subject": "x"}\n```'
        _patch_model(monkeypatch, "Handing off, as ever.\n\n⟦P0⟧")
        out = asyncio.run(vp.rewrite_for_voice(f"Handing off.\n\n{block}", "flat"))
        assert out.endswith(block)

    def test_protected_only_skips_model(self, vp, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("no model call expected")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        assert asyncio.run(vp.rewrite_for_voice("<@111> <@222>", "flat")) is None


class TestGateLengthAndProtectedOnly:
    def test_too_long_skips_rewrite_and_judge(self, vp, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("no model call expected")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        monkeypatch.setattr(vp, "judge_voice_presence", boom)
        monkeypatch.setattr(vp, "rewrite_for_voice", boom)
        text = "x" * (vp.REWRITE_MAX_CHARS + 1)
        out, meta = asyncio.run(vp.gate_and_rewrite(text))
        assert out == text
        assert meta["gate_action"] == "skipped_too_long"

    def test_exactly_max_chars_still_judged(self, vp, monkeypatch):
        async def fake_judge(text):
            return {"in_voice": True, "reason": "ok"}

        monkeypatch.setattr(vp, "judge_voice_presence", fake_judge)
        _, meta = asyncio.run(vp.gate_and_rewrite("x" * vp.REWRITE_MAX_CHARS))
        assert meta["gate_action"] == "passed"

    def test_protected_only_skips_judge(self, vp, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("no model call expected")

        monkeypatch.setattr(vp, "judge_voice_presence", boom)
        out, meta = asyncio.run(vp.gate_and_rewrite("<@111>  https://example.com"))
        assert out == "<@111>  https://example.com"
        assert meta["gate_action"] == "skipped_protected_only"
