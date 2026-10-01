"""reload-on-commit.py — auto-bounce relay/scheduler when a commit
changes the files that govern their behavior.

Code landing on disk isn't the same as code running: bin/relay.py picked
up the attention-marker gate override in commit fa031a9, but the running
process kept the old behavior until someone noticed and manually
restarted it. These tests exercise the pure changed-files -> bounce-plan
mapping (plan_reloads) without touching real processes or git.
"""

import json

from conftest import import_script

reload_on_commit = import_script("reload-on-commit")


def test_relay_change_bounces_relay():
    to_bounce, warnings = reload_on_commit.plan_reloads(["bin/relay.py"])
    assert to_bounce == {"relay": "karakos-relay.service"}
    assert warnings == []


def test_reply_gate_change_bounces_relay():
    """reply_gate.py has no process of its own — it's imported by
    relay.py, so a change there should bounce relay too."""
    to_bounce, warnings = reload_on_commit.plan_reloads(["bin/reply_gate.py"])
    assert to_bounce == {"relay": "karakos-relay.service"}


def test_scheduler_change_bounces_scheduler():
    to_bounce, warnings = reload_on_commit.plan_reloads(["bin/scheduler.py"])
    assert to_bounce == {"scheduler": "karakos-scheduler.service"}
    assert warnings == []


def test_agent_server_change_warns_but_does_not_bounce():
    """agent-server.py is very likely this script's own ancestry (a
    commit made from within Marvin's own process) — must never be an
    auto-bounce target, only a printed warning."""
    to_bounce, warnings = reload_on_commit.plan_reloads(["bin/agent-server.py"])
    assert to_bounce == {}
    assert len(warnings) == 1
    assert "agent-server.py" in warnings[0]


def test_unrelated_file_change_is_a_noop():
    to_bounce, warnings = reload_on_commit.plan_reloads(["docs/README.md", "tests/test_foo.py"])
    assert to_bounce == {}
    assert warnings == []


def test_multiple_watched_files_bounce_each_process_once():
    to_bounce, warnings = reload_on_commit.plan_reloads(
        ["bin/relay.py", "bin/reply_gate.py", "bin/scheduler.py"]
    )
    assert to_bounce == {"relay": "karakos-relay.service", "scheduler": "karakos-scheduler.service"}


def test_mixed_watched_and_self_process_change():
    to_bounce, warnings = reload_on_commit.plan_reloads(
        ["bin/relay.py", "bin/agent-server.py"]
    )
    assert to_bounce == {"relay": "karakos-relay.service"}
    assert len(warnings) == 1


def test_empty_changeset_is_a_noop():
    to_bounce, warnings = reload_on_commit.plan_reloads([])
    assert to_bounce == {}
    assert warnings == []


def test_main_dispatches_bounce_asynchronously(tmp_path, monkeypatch):
    """2026-08-11: per Amos's report of two live outages from this same
    shape of bug on his side, the fix moved to relay.py's own graceful
    drain on SIGTERM (see _graceful_shutdown) — but the old synchronous
    subprocess.run in this hook (blocking the post-commit hook, and
    therefore `git commit` itself, on the bounce completing) was still
    worth removing on its own merits. Confirms main() now uses Popen
    (fire-and-forget, detached) rather than run (blocking-until-complete)
    for the actual signal dispatch, and logs an 'auto_reload_dispatched'
    event without waiting on a returncode."""
    monkeypatch.setattr(reload_on_commit, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(reload_on_commit, "EVENTS_LOG", tmp_path / "logs" / "git-events.jsonl")
    monkeypatch.setattr(reload_on_commit, "get_committed_files", lambda: ["bin/relay.py"])
    monkeypatch.setattr(reload_on_commit, "commit_toplevel", lambda: tmp_path.resolve())

    popen_calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    def fake_run(args, **kwargs):
        # Only subprocess.run left in main() is `git rev-parse HEAD`.
        class Result:
            stdout = "deadbeef\n"
        return Result()

    monkeypatch.setattr(reload_on_commit.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(reload_on_commit.subprocess, "run", fake_run)

    reload_on_commit.main()

    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert kwargs.get("start_new_session") is True, (
        "must detach from git's process group so the hook returning "
        "doesn't affect the restart landing"
    )
    cmd = args[0]
    assert cmd[:2] == ["sudo", str(reload_on_commit.RESTART_SERVICE)], (
        "must go through the allowlisted restart-service.sh wrapper, not "
        "a raw systemctl call or a pattern-matching pkill — 2026-08-18: "
        "the old safe-pkill.sh dispatch pattern-matched and killed "
        "Marvin's own subprocess as collateral, see module docstring"
    )
    assert cmd[2:] == ["restart", "karakos-relay.service"], (
        "must target the exact unit by name — no cmdline substring for "
        "an unrelated process to accidentally match"
    )

    events_log = tmp_path / "logs" / "git-events.jsonl"
    assert events_log.exists()
    events = [json.loads(line) for line in events_log.read_text().splitlines()]
    assert any(e.get("event") == "auto_reload_dispatched" for e in events)
    assert not any(e.get("event") == "auto_reload" for e in events), (
        "old synchronous event name should be gone, not just supplemented"
    )


def _run_main(monkeypatch, workspace, toplevel):
    monkeypatch.setattr(reload_on_commit, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(reload_on_commit, "EVENTS_LOG", workspace / "logs" / "git-events.jsonl")
    monkeypatch.setattr(reload_on_commit, "get_committed_files", lambda: ["bin/relay.py"])
    monkeypatch.setattr(reload_on_commit, "commit_toplevel", lambda: toplevel)
    popen_calls = []
    monkeypatch.setattr(
        reload_on_commit.subprocess, "Popen", lambda *a, **k: popen_calls.append(a)
    )

    class Result:
        stdout = "deadbeef\n"
        returncode = 0

    monkeypatch.setattr(reload_on_commit.subprocess, "run", lambda *a, **k: Result())
    reload_on_commit.main()
    return popen_calls


def test_commit_in_other_worktree_does_not_restart(tmp_path, monkeypatch):
    live = tmp_path / "live"
    other = live / ".claude" / "worktrees" / "agent-x"
    other.mkdir(parents=True)
    calls = _run_main(monkeypatch, live, other.resolve())
    assert calls == []
    lines = (live / "logs" / "git-events.jsonl").read_text().splitlines()
    assert [json.loads(l)["event"] for l in lines] == ["auto_reload_skipped_other_worktree"]


def test_commit_in_live_root_restarts(tmp_path, monkeypatch):
    calls = _run_main(monkeypatch, tmp_path, tmp_path.resolve())
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["restart", "karakos-relay.service"]


def test_symlinked_workspace_root_counts_as_live(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    calls = _run_main(monkeypatch, link, real.resolve())
    assert len(calls) == 1


def test_git_calls_use_hook_cwd(monkeypatch):
    seen = []

    def fake_run(args, **kwargs):
        seen.append(kwargs)

        class R:
            stdout = "bin/relay.py\n"
            returncode = 0

        return R()

    monkeypatch.setattr(reload_on_commit.subprocess, "run", fake_run)
    assert reload_on_commit.get_committed_files() == ["bin/relay.py"]
    reload_on_commit.commit_toplevel()
    assert all("cwd" not in k for k in seen)
