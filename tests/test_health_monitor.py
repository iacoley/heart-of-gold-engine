"""
Tests for bin/health-monitor.py — Component health checking.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT


class TestHealthFileChecks:
    """Test health file freshness detection."""

    def _make_monitor(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        return import_script("health-monitor")

    def test_healthy_file_passes(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        now = datetime.now().isoformat()
        (health_dir / "relay.json").write_text(json.dumps({"timestamp": now}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is True
        assert reason == ""

    def test_stale_file_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        old = (datetime.now() - timedelta(minutes=10)).isoformat()
        (health_dir / "relay.json").write_text(json.dumps({"timestamp": old}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is False
        assert "stale" in reason

    def test_missing_file_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        healthy, reason = monitor.check_health_file("nonexistent.json", 300)
        assert healthy is False
        assert "missing" in reason

    def test_empty_timestamp_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        (health_dir / "relay.json").write_text(json.dumps({"timestamp": ""}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is False
        assert "no timestamp" in reason

    def test_malformed_json_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        (health_dir / "relay.json").write_text("not json")

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is False
        assert "error" in reason

    def test_memory_has_longer_threshold(self, tmp_workspace, monkeypatch):
        """Memory maintenance only runs daily — 48h threshold."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        old = (datetime.now() - timedelta(hours=24)).isoformat()
        (health_dir / "memory.json").write_text(json.dumps({"timestamp": old}))

        healthy, _ = monitor.check_health_file("memory.json", 172800)
        assert healthy is True


class TestMcpToolsLiveProbe:
    """task-1788926556: mcp-tools.json's file-age check false-fired on a
    plain usage gap (nobody happened to call an MCP tool in the window),
    not a real tools-server outage — same bug class relay.py already
    fixed for its own heartbeat judgment (task-1788216451). Replaced
    with a synthetic --test-tool invocation; these tests fake
    subprocess.run rather than actually spawning tools-server.py."""

    def _make_monitor(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        return import_script("health-monitor")

    class Result:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def test_healthy_when_probe_returns_expected_payload(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(args, **kwargs):
            assert args[:3] == ["python3", str(monitor.WORKSPACE_ROOT / "mcp" / "tools-server.py"), "--test-tool"]
            return self.Result(stdout=json.dumps({
                "system_name": "Karakos", "version": "1.0.0",
                "owner": "User", "workspace": str(monitor.WORKSPACE_ROOT),
            }))

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_mcp_tools_live()
        assert healthy is True
        assert reason == ""

    def test_does_not_false_fire_on_a_plain_usage_gap(self, tmp_workspace, monkeypatch):
        """The actual regression: no mcp-tools.json activity in 10+
        minutes used to alert on its own. A successful live probe must
        report healthy regardless of any file stale in the background —
        this function never even looks at the health file."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        # No data/health/mcp-tools.json written at all -- old code would
        # have treated this as "missing" (suppressed) or, once it did
        # exist, stale-by-timestamp. New code doesn't touch the file.
        monkeypatch.setattr(
            monitor.subprocess, "run",
            lambda args, **kw: self.Result(stdout=json.dumps({"workspace": "/opt/karakos"})),
        )
        healthy, reason = monitor.check_mcp_tools_live()
        assert healthy is True

    def test_unhealthy_on_nonzero_exit(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        monkeypatch.setattr(
            monitor.subprocess, "run",
            lambda args, **kw: self.Result(stderr="Traceback...", returncode=1),
        )
        healthy, reason = monitor.check_mcp_tools_live()
        assert healthy is False
        assert "exited 1" in reason

    def test_unhealthy_on_timeout(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(args, **kwargs):
            raise monitor.subprocess.TimeoutExpired(cmd=args, timeout=monitor.MCP_TOOLS_PROBE_TIMEOUT)

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)
        healthy, reason = monitor.check_mcp_tools_live()
        assert healthy is False
        assert "timed out" in reason

    def test_unhealthy_on_malformed_output(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        monkeypatch.setattr(
            monitor.subprocess, "run",
            lambda args, **kw: self.Result(stdout="not json"),
        )
        healthy, reason = monitor.check_mcp_tools_live()
        assert healthy is False
        assert "non-JSON" in reason

    def test_unhealthy_on_unexpected_payload_shape(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        monkeypatch.setattr(
            monitor.subprocess, "run",
            lambda args, **kw: self.Result(stdout=json.dumps({"unrelated": "shape"})),
        )
        healthy, reason = monitor.check_mcp_tools_live()
        assert healthy is False
        assert "unexpected payload" in reason


class TestGitSyncCheck:
    """Test the local-main-vs-origin/main drift check.

    Incident 2026-08-29: local main silently drifted 43 commits / 18 days
    ahead of origin/main because a GITHUB_TOKEN missing the 'workflow'
    scope made every `git push` fail, and nothing read the exit code or
    stderr. These tests exercise check_git_sync()'s command sequence
    (fetch, rev-list x2, push-if-ahead) by faking subprocess.run rather
    than touching a real remote.
    """

    def _make_monitor(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        return import_script("health-monitor")

    def test_in_sync_passes_without_pushing(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        calls = []

        class Result:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:2] == ["git", "fetch"]:
                return Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                return Result(stdout="0\n")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is True
        assert reason == ""
        assert not any(c[:2] == ["git", "push"] for c in calls), (
            "must not push when already in sync"
        )

    def test_ahead_pushes_and_self_heals(self, tmp_workspace, monkeypatch):
        """Same-session ahead-count >0 right after a commit is normal —
        if the push succeeds, no alert should fire."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        push_calls = []

        class Result:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "fetch"]:
                return Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                if args[3] == "origin/main..main":
                    return Result(stdout="3\n")
                return Result(stdout="0\n")
            if args[:2] == ["git", "push"]:
                push_calls.append(args)
                return Result()
            if args[:2] == ["git", "rev-parse"]:
                return Result(stdout="deadbeef\n")
            if args[:3] == ["gh", "run", "list"]:
                return Result(stdout=json.dumps(
                    [{"status": "completed", "conclusion": "success",
                      "url": "https://github.com/x/y/actions/runs/1"}]
                ))
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is True
        assert reason == ""
        assert len(push_calls) == 1
        assert push_calls[0] == ["git", "push", "origin", "main"], (
            "must be a plain fast-forward push, never --force"
        )

    def test_ahead_with_failed_push_alerts_with_git_stderr(self, tmp_workspace, monkeypatch):
        """The actual regression: push fails because the token lacks
        'workflow' scope. The alert must surface git's real stderr so a
        human doesn't have to re-derive the diagnosis from scratch."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        class Result:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""

        workflow_error = (
            "refusing to allow a Personal Access Token to create or "
            "update workflow `.github/workflows/ci.yml` without "
            "`workflow` scope"
        )

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "fetch"]:
                return Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                if args[3] == "origin/main..main":
                    return Result(stdout="43\n")
                return Result(stdout="0\n")
            if args[:2] == ["git", "push"]:
                raise monitor.subprocess.CalledProcessError(
                    1, args, output="", stderr=f"! [remote rejected] main -> main ({workflow_error})"
                )
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is False
        assert "43 commit(s) ahead" in reason
        assert "workflow" in reason and "scope" in reason

    def test_ahead_push_failure_alerts_every_run_not_just_once(self, tmp_workspace, monkeypatch):
        """A failing push is worth flagging every time the check runs
        until it's fixed — no debouncing beyond the self-heal above."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        class Result:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "fetch"]:
                return Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                if args[3] == "origin/main..main":
                    return Result(stdout="1\n")
                return Result(stdout="0\n")
            if args[:2] == ["git", "push"]:
                raise monitor.subprocess.CalledProcessError(1, args, output="", stderr="still broken")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        first = monitor.check_git_sync()
        second = monitor.check_git_sync()
        assert first[0] is False
        assert second[0] is False
        assert first[1] == second[1]

    def test_behind_is_informational_and_does_not_push_or_merge(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        calls = []

        class Result:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:2] == ["git", "fetch"]:
                return Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                if args[3] == "origin/main..main":
                    return Result(stdout="0\n")
                return Result(stdout="2\n")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is False
        assert "2 commit(s) behind" in reason
        assert "informational" in reason
        assert not any(c[:2] in (["git", "push"], ["git", "merge"], ["git", "pull"]) for c in calls)

    def test_fetch_failure_does_not_raise(self, tmp_workspace, monkeypatch):
        """A network hiccup during fetch must be reported as an unhealthy
        result, not an unhandled exception that crashes the health-monitor
        run before other checks execute."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "fetch"]:
                raise monitor.subprocess.TimeoutExpired(cmd=args, timeout=monitor.GIT_FETCH_TIMEOUT)
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is False
        assert "timed out" in reason


class TestCiStatusAfterPush:
    """Test the post-push CI status poll.

    Incident 2026-08-11 through ~2026-08-29: CI on GitHub Actions had been
    red for weeks and nobody noticed, because pytest aborted the whole run
    on the first collection error and nothing ever surfaced the failures
    hiding underneath. check_git_sync() now polls `gh run list` for the
    run triggered by its own push and alerts #signals if it didn't pass.
    These tests fake subprocess.run rather than hitting real `gh`.
    """

    def _make_monitor(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        return import_script("health-monitor")

    class Result:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""

    def test_ci_success_reports_success_with_no_message(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "run", "list"]:
                return self.Result(stdout=json.dumps(
                    [{"status": "completed", "conclusion": "success",
                      "url": "https://github.com/x/y/actions/runs/1"}]
                ))
            raise AssertionError(f"unexpected call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        status, message = monitor.check_ci_status_after_push("deadbeef")
        assert status == "success"
        assert message == ""

    def test_ci_failure_alerts_with_sha_url_and_conclusion(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "run", "list"]:
                return self.Result(stdout=json.dumps(
                    [{"status": "completed", "conclusion": "failure",
                      "url": "https://github.com/x/y/actions/runs/2"}]
                ))
            raise AssertionError(f"unexpected call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        status, message = monitor.check_ci_status_after_push("deadbeef")
        assert status == "failure"
        assert "deadbeef" in message
        assert "https://github.com/x/y/actions/runs/2" in message
        assert "failure" in message

    def test_ci_never_found_reports_pending_not_failure(self, tmp_workspace, monkeypatch):
        """If GitHub hasn't surfaced a run for the pushed SHA within the
        poll window, that's informational — not treated as a hard
        failure, since the workflow may simply not have triggered yet."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        monkeypatch.setattr(monitor, "CI_POLL_TIMEOUT", 0)
        calls = []

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "run", "list"]:
                calls.append(args)
                return self.Result(stdout="[]")
            raise AssertionError(f"unexpected call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        status, message = monitor.check_ci_status_after_push("deadbeef")
        assert status == "pending"
        assert "deadbeef" in message
        assert len(calls) >= 1

    def test_git_sync_alerts_when_pushed_commits_ci_fails(self, tmp_workspace, monkeypatch):
        """Integration: check_git_sync() itself surfaces the CI failure of
        the commit it just pushed, through its normal (bool, str) return
        so main()'s existing poke_signals routing picks it up."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "fetch"]:
                return self.Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                if args[3] == "origin/main..main":
                    return self.Result(stdout="1\n")
                return self.Result(stdout="0\n")
            if args[:2] == ["git", "push"]:
                return self.Result()
            if args[:2] == ["git", "rev-parse"]:
                return self.Result(stdout="cafef00d\n")
            if args[:3] == ["gh", "run", "list"]:
                return self.Result(stdout=json.dumps(
                    [{"status": "completed", "conclusion": "failure",
                      "url": "https://github.com/x/y/actions/runs/3"}]
                ))
            raise AssertionError(f"unexpected call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is False
        assert "cafef00d" in reason
        assert "https://github.com/x/y/actions/runs/3" in reason
        assert "failure" in reason

    def test_git_sync_stays_healthy_when_ci_still_pending(self, tmp_workspace, monkeypatch):
        """A push succeeding but CI not showing up yet must not fail the
        overall git-sync check — only a confirmed non-success conclusion
        should."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        monkeypatch.setattr(monitor, "CI_POLL_TIMEOUT", 0)

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "fetch"]:
                return self.Result()
            if args[:3] == ["git", "rev-list", "--count"]:
                if args[3] == "origin/main..main":
                    return self.Result(stdout="1\n")
                return self.Result(stdout="0\n")
            if args[:2] == ["git", "push"]:
                return self.Result()
            if args[:2] == ["git", "rev-parse"]:
                return self.Result(stdout="cafef00d\n")
            if args[:3] == ["gh", "run", "list"]:
                return self.Result(stdout="[]")
            raise AssertionError(f"unexpected call: {args}")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)

        healthy, reason = monitor.check_git_sync()
        assert healthy is True
        assert reason == ""
