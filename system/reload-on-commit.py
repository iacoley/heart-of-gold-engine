#!/usr/bin/env python3
"""
reload-on-commit.py — Auto-bounce the relay/scheduler supervisor
processes when a commit touches the files that govern their behavior.

Problem this fixes (2026-08-08): bin/relay.py picked up the 📨
attention-marker gate override in commit fa031a9 earlier tonight, but the
running relay process kept executing the pre-change code until someone
noticed and manually called reload_agent — Amos's own adoption message,
which itself contained the marker, went unnoticed because of exactly
this gap. Same shape of bug as the one bin/outbox.py fixes for
cross-channel relays, just at deploy time instead of message time: code
landing on disk is not the same as code running.

Called from .git/hooks/post-commit, which is installed from
system/install-post-commit-hook.sh by bin/entrypoint.sh — mirrors how
system/install-hooks.sh installs the pre-commit protected-paths check.
Maps files changed in the commit that just happened to the systemd unit
that needs bouncing, and shells out to bin/restart-service.sh (the same
sudoers-allowlisted, unit-name-targeted wrapper used for manual restarts)
to restart it precisely — no process-list pattern matching involved.

Deliberately excludes bin/agent-server.py — restart-service.sh's own
allowlist excludes karakos-agent-server.service for the same reason
(see that script's docstring): self-bouncing needs a deliberate, visible
action (the reload_agent admin tool, or a fresh turn), not a silent side
effect of committing. This script only prints a reminder for that case.

SUPERSEDED 2026-08-18: this used to shell out to bin/safe-pkill.sh with
a filename-shaped pattern like "bin/relay.py", matched against every
process's full command line. That's fine when the caller chain is
intact — safe-pkill.sh refuses to signal its own ancestry — but the
dispatch below deliberately detaches (start_new_session=True, so
`git commit` isn't blocked on the bounce), and a detached process's
ancestry no longer traces back through git -> the Bash tool -> Marvin's
own subprocess. That subprocess's command line (system prompt +
injected memory context) routinely contains the literal text
"bin/relay.py" — this file gets discussed by name constantly, including
in this very docstring — so the pattern matched Marvin's own subprocess
as collateral and killed it mid-session (caught live 2026-08-18, see
agents/Marvin/memory/facts/reload-hook-self-kill-2026-08-18.md).
restart-service.sh targets an exact systemd unit by name via systemctl;
there is no command-line text to accidentally match, so this class of
bug can't recur here regardless of what the calling process's own argv
happens to contain.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
EVENTS_LOG = WORKSPACE_ROOT / "logs" / "git-events.jsonl"
RESTART_SERVICE = WORKSPACE_ROOT / "bin" / "restart-service.sh"

# Changed file -> (systemd unit name, process label). Unit names must be
# in restart-service.sh's own ALLOWED_UNITS allowlist — that script is
# the actual security boundary, this is just the file->unit mapping.
# reply_gate.py has no process of its own; it's imported by relay.py, so
# a change there bounces relay just like a direct relay.py change would.
WATCHED = {
    "bin/relay.py": ("karakos-relay.service", "relay"),
    "bin/reply_gate.py": ("karakos-relay.service", "relay"),
    "bin/scheduler.py": ("karakos-scheduler.service", "scheduler"),
}

SELF_PROCESS_WARN = {
    "bin/agent-server.py": (
        "agent-server.py changed (Marvin's own process) — not "
        "auto-restarted, that needs a deliberate reload_agent call or a "
        "fresh turn, not a silent side effect of a commit."
    ),
}


def commit_toplevel() -> Path | None:
    """Toplevel of the worktree the commit happened in (the hook's own cwd)."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    )
    out = result.stdout.strip()
    return Path(out).resolve() if result.returncode == 0 and out else None


def is_live_checkout(toplevel: Path | None) -> bool:
    return toplevel is not None and toplevel == WORKSPACE_ROOT.resolve()


def get_committed_files() -> list[str]:
    # No cwd override: run in the hook's own cwd so HEAD is the commit that
    # was actually just made (git sets cwd to the committing worktree's
    # toplevel), not the live checkout's HEAD.
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        capture_output=True, text=True,
    )
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


def plan_reloads(changed_files: list[str]) -> tuple[dict[str, str], list[str]]:
    """Pure mapping from changed files to (process label -> systemd unit)
    plus any self-process warnings. Kept side-effect-free for testing."""
    to_bounce: dict[str, str] = {}
    warnings: list[str] = []
    for f in changed_files:
        if f in WATCHED:
            unit, label = WATCHED[f]
            to_bounce[label] = unit
        if f in SELF_PROCESS_WARN:
            warnings.append(SELF_PROCESS_WARN[f])
    return to_bounce, warnings


def _log(event: dict) -> None:
    try:
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def main():
    top = commit_toplevel()
    if not is_live_checkout(top):
        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "auto_reload_skipped_other_worktree",
            "worktree": str(top),
            "note": "commit was in a different worktree than the live checkout; no restart",
        })
        print(f"reload-on-commit: skipped, commit was in a different worktree ({top})")
        return

    changed = get_committed_files()
    to_bounce, warnings = plan_reloads(changed)

    if not to_bounce and not warnings:
        return

    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    for label, unit in to_bounce.items():
        print(f"reload-on-commit: {label} code changed in {commit_sha[:8]} — restarting {unit} (async)")
        # 2026-08-11, per Amos's report of two live outages caused by this
        # same shape of bug on his side: dispatch the bounce and return
        # immediately rather than blocking `git commit` on the outcome.
        # start_new_session detaches the child from git's process group
        # so it isn't affected if git itself exits/is reaped before the
        # restart lands. Unlike the old safe-pkill.sh dispatch, detaching
        # here carries no self-kill risk — sudo+systemctl targets an
        # exact unit name, there's no process-list pattern to accidentally
        # match against Marvin's own subprocess (see module docstring,
        # 2026-08-18).
        bounce_log = WORKSPACE_ROOT / "logs" / f"reload-on-commit-{label}.log"
        bounce_log.parent.mkdir(parents=True, exist_ok=True)
        with open(bounce_log, "a") as logf:
            subprocess.Popen(
                ["sudo", str(RESTART_SERVICE), "restart", unit],
                stdout=logf, stderr=subprocess.STDOUT,
                cwd=str(WORKSPACE_ROOT), start_new_session=True,
            )
        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "auto_reload_dispatched",
            "commit": commit_sha,
            "process": label,
            "note": f"fired async via restart-service.sh, see {bounce_log.name} for output",
        })

    for w in warnings:
        print(f"reload-on-commit: NOTE — {w}")
        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "auto_reload_skipped_self",
            "commit": commit_sha,
            "detail": w,
        })


if __name__ == "__main__":
    main()
