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
    module = import_script("auth_guard", file_path=PACKAGE_ROOT / "bin" / "auth_guard.py")
    # record_failure() now fires trigger_relogin_relay() on the first
    # transition (spawns `claude auth login`, sleeps 8s, shells out to
    # discord-notify.sh) -- exactly what every test in this file except
    # TestTriggerRelogin itself needs stubbed out. That real behavior
    # gets its own coverage below with subprocess/time properly mocked.
    monkeypatch.setattr(module, "trigger_relogin_relay", lambda: True)
    return module


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


class _FakePopen:
    """Stands in for subprocess.Popen(...claude auth login...): writes
    fixed text to the same file handle the real CLI's stdout would have
    landed in, so trigger_relogin_relay() has something to capture."""

    def __init__(self, *args, stdout=None, **kwargs):
        if stdout is not None:
            stdout.write("Visit https://claude.ai/device?code=ABCD-EFGH to sign in\n")
            stdout.flush()


@pytest.fixture
def agr(monkeypatch, tmp_workspace):
    """Unstubbed auth_guard for exercising trigger_relogin_relay() itself
    -- real subprocess.Popen and time.sleep replaced so tests neither
    spawn a genuine device-flow login nor actually wait 8 seconds."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    monkeypatch.setenv("OWNER_DISCORD_ID", "999")
    module = import_script("auth_guard", file_path=PACKAGE_ROOT / "bin" / "auth_guard.py")
    monkeypatch.setattr(module.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


class TestTriggerRelogin:
    def test_captures_link_and_relays_it(self, agr, monkeypatch):
        calls = []
        monkeypatch.setattr(
            agr.subprocess,
            "run",
            lambda args, **kw: calls.append(args) or type("R", (), {"returncode": 0})(),
        )
        assert agr.trigger_relogin_relay() is True
        assert len(calls) == 1
        notify_args = calls[0]
        assert notify_args[0] == str(agr.NOTIFY_SCRIPT)
        assert notify_args[1] == agr.RELOGIN_CHANNEL
        assert "<@999>" in notify_args[2]
        assert "https://claude.ai/device?code=ABCD-EFGH" in notify_args[2]

    def test_respects_relogin_channel_override(self, monkeypatch, tmp_workspace):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("AUTH_GUARD_RELOGIN_CHANNEL", "signals")
        module = import_script("auth_guard", file_path=PACKAGE_ROOT / "bin" / "auth_guard.py")
        assert module.RELOGIN_CHANNEL == "signals"

    def test_false_when_spawn_raises(self, agr, monkeypatch):
        def boom(*a, **kw):
            raise OSError("no such binary")
        monkeypatch.setattr(agr.subprocess, "Popen", boom)
        assert agr.trigger_relogin_relay() is False

    def test_false_when_nothing_captured(self, agr, monkeypatch):
        class _EmptyPopen(_FakePopen):
            def __init__(self, *args, stdout=None, **kwargs):
                pass  # writes nothing

        monkeypatch.setattr(agr.subprocess, "Popen", _EmptyPopen)
        run_calls = []
        monkeypatch.setattr(agr.subprocess, "run", lambda *a, **kw: run_calls.append(1))
        assert agr.trigger_relogin_relay() is False
        assert run_calls == []  # never even tries to relay an empty capture

    def test_false_when_discord_relay_fails(self, agr, monkeypatch):
        def raise_notify(*a, **kw):
            raise agr.subprocess.CalledProcessError(1, "discord-notify.sh")
        monkeypatch.setattr(agr.subprocess, "run", raise_notify)
        assert agr.trigger_relogin_relay() is False


class TestRecordFailureTriggersRelogin:
    """record_failure() itself is covered by the stubbed `ag` fixture
    everywhere else in this file; this class checks the wiring, not the
    relogin mechanics (already covered above)."""

    def test_fires_relogin_on_first_transition(self, ag, monkeypatch):
        calls = []
        monkeypatch.setattr(ag, "trigger_relogin_relay", lambda: calls.append(1) or True)
        ag.record_failure()
        assert calls == [1]

    def test_does_not_refire_within_the_same_outage(self, ag, monkeypatch):
        calls = []
        monkeypatch.setattr(ag, "trigger_relogin_relay", lambda: calls.append(1) or True)
        ag.record_failure()
        ag.record_failure()
        ag.record_failure()
        assert calls == [1]

    def test_refires_after_a_recovery_in_between(self, ag, monkeypatch):
        calls = []
        monkeypatch.setattr(ag, "trigger_relogin_relay", lambda: calls.append(1) or True)
        ag.record_failure()
        ag.record_success()
        ag.record_failure()
        assert calls == [1, 1]

    def test_a_relogin_exception_does_not_break_bookkeeping(self, ag, monkeypatch):
        def boom():
            raise RuntimeError("discord is down too, apparently")
        monkeypatch.setattr(ag, "trigger_relogin_relay", boom)
        assert ag.record_failure() is True
        state = ag._read_state()
        assert state["known_bad"] is True
        assert state["relogin_triggered"] is True
