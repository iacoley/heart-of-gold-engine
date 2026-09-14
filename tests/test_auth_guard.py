"""
Tests for bin/auth_guard.py — the detect-and-survive guard for the
2026-09-14 shared-OAuth-rotation scar (confirmed identical to Amos's
2026-07-19 incident, projects/pty-supervisor/supervisor.py AuthGuard).

State lives in a small JSON file under WORKSPACE_ROOT/data so it
coordinates across separate processes; tests exercise that file
directly via the tmp_workspace fixture rather than mocking it away.
"""

import time

import pytest

from conftest import import_script, PACKAGE_ROOT


@pytest.fixture
def ag(monkeypatch, tmp_workspace):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    return import_script("auth_guard", file_path=PACKAGE_ROOT / "bin" / "auth_guard.py")


class TestIsAuthFailureSignature:
    def test_matches_the_real_leaked_string(self, ag):
        assert ag.is_auth_failure_signature(
            "Failed to authenticate: OAuth session expired and could not be refreshed"
        )

    def test_matches_as_a_substring(self, ag):
        """The signature shows up embedded in differently-shaped
        payloads (judge verdicts, classifier answers) -- not always as
        the whole string."""
        assert ag.is_auth_failure_signature(
            "some wrapper text\nOAuth session expired and could not be refreshed\nmore text"
        )

    def test_none_and_empty_do_not_match(self, ag):
        assert not ag.is_auth_failure_signature(None)
        assert not ag.is_auth_failure_signature("")

    def test_unrelated_text_does_not_match(self, ag):
        assert not ag.is_auth_failure_signature("INVOICE\nDry, understated, on register.")


class TestShouldAttempt:
    def test_true_when_no_state_file_exists_yet(self, ag):
        assert ag.should_attempt() is True

    def test_false_immediately_after_a_recorded_failure(self, ag):
        ag.record_failure()
        assert ag.should_attempt() is False

    def test_true_again_once_cooldown_elapses(self, ag, monkeypatch):
        ag.record_failure()
        assert ag.should_attempt() is False
        future = time.time() + ag.PROBE_COOLDOWN_SEC + 1
        monkeypatch.setattr(time, "time", lambda: future)
        assert ag.should_attempt() is True

    def test_true_after_a_recorded_success(self, ag):
        ag.record_failure()
        ag.record_success()
        assert ag.should_attempt() is True


class TestRecordFailure:
    def test_first_call_returns_true(self, ag):
        assert ag.record_failure() is True

    def test_second_call_within_same_outage_returns_false(self, ag):
        assert ag.record_failure() is True
        assert ag.record_failure() is False
        assert ag.record_failure() is False

    def test_alerts_again_after_a_recovery_in_between(self, ag):
        """A fresh outage after recovery is a new transition, not a
        continuation of the old one -- should alert again."""
        assert ag.record_failure() is True
        ag.record_success()
        assert ag.record_failure() is True


class TestRecordSuccess:
    def test_noop_when_already_healthy(self, ag):
        # Should not raise, and should not create a state file just for
        # a routine success -- nothing to coordinate about yet.
        ag.record_success()
        assert ag.should_attempt() is True

    def test_clears_known_bad(self, ag):
        ag.record_failure()
        ag.record_success()
        state = ag._read_state()
        assert state["known_bad"] is False


class TestCrossProcessCoordination:
    """The whole point of a file-backed state instead of an in-memory
    flag: a second 'process' (a fresh import in this test acts as a
    stand-in) sees the same outage another one recorded."""

    def test_second_reader_sees_first_writers_failure(self, ag, monkeypatch, tmp_workspace):
        ag.record_failure()
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        ag2 = import_script("auth_guard", file_path=PACKAGE_ROOT / "bin" / "auth_guard.py")
        assert ag2.should_attempt() is False
