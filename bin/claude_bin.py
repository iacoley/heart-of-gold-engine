"""Single resolver for the Claude Code CLI path.

Every spawn of the `claude` CLI goes through here so the dependency is
explicit (CLAUDE_BIN, set in native/systemd/karakos-agent-server.service)
and a missing binary fails with a legible message instead of a bare
`[Errno 2] No such file or directory: 'claude'`.
"""

import os
import shutil


def configured_value() -> str:
    return os.environ.get("CLAUDE_BIN") or "claude"


def resolve_claude_bin() -> str | None:
    """Absolute path of the claude CLI, or None if it can't be found."""
    value = configured_value()
    if "/" in value:
        return value if os.path.isfile(value) and os.access(value, os.X_OK) else None
    return shutil.which(value)


def missing_message() -> str:
    return (
        f"claude CLI not found: CLAUDE_BIN={configured_value()!r} "
        f"(PATH searched: {os.environ.get('PATH', '')!r}). "
        "The claude on systemd's default PATH is load-bearing -- "
        "see native/systemd/karakos-agent-server.service"
    )


def claude_bin() -> str:
    """Like resolve_claude_bin() but raises FileNotFoundError with a clear message."""
    path = resolve_claude_bin()
    if path is None:
        raise FileNotFoundError(missing_message())
    return path
