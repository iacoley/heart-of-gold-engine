"""Minimum Claude Code CLI version per model.

A model switched in config/agents.json can need a newer CLI than the one
installed (claude-opus-5-5 needs >= 2.1.280; on 2.1.222 every turn fails
with an API 400 and the agent is down). agent-server consults this before
spawning an agent subprocess.

The table is a plain dict here rather than JSON under config/ because it
is code-adjacent knowledge (it moves with CLI releases, not per-install),
ships with the package, and needs no loader/validation path. To add a
model, add one line to MIN_CLI_VERSION.

Lookup is by model id or id prefix (longest match wins, on a "-" or "["
boundary so `claude-opus-5-5[1m]` matches but `claude-opus-5-50` does not).
Unknown models and aliases (haiku/sonnet/opus) have no requirement.
"""

import os
import re
import subprocess
from typing import Optional, Tuple

import claude_bin

MIN_CLI_VERSION: dict[str, str] = {
    "claude-opus-5-5": "2.1.280",
}

Version = Tuple[int, ...]

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def parse_version(text: str) -> Optional[Version]:
    """First dotted-numeric version in text ('2.1.287 (Claude Code)' ->
    (2, 1, 287)), or None. Components compare numerically."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def version_lt(a: Version, b: Version) -> bool:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) < b + (0,) * (n - len(b))


def required_version(model: Optional[str]) -> Optional[str]:
    """Minimum CLI version string for model, or None if no requirement."""
    if not model:
        return None
    best = None
    for key in MIN_CLI_VERSION:
        if model == key or (
            model.startswith(key) and model[len(key)] in "-["
        ):
            if best is None or len(key) > len(best):
                best = key
    return MIN_CLI_VERSION[best] if best else None


_cache: dict = {}


def get_cli_version() -> Tuple[Optional[str], Optional[Version], str]:
    """(path, version, raw_output). version is None if undeterminable.

    Cached per (path, mtime, size) so turns/reloads don't shell out, but
    upgrading the CLI in place is picked up on the next reload without an
    agent-server restart. Never raises."""
    path = claude_bin.resolve_claude_bin()
    if path is None:
        return None, None, claude_bin.missing_message()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    if key in _cache:
        return _cache[key]
    try:
        raw = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=15).stdout.strip()
    except Exception as e:
        # Not cached: a transient failure shouldn't stick.
        return path, None, f"--version failed: {e}"
    result = (path, parse_version(raw), raw)
    _cache[key] = result
    return result


def check_model(model: Optional[str]) -> Tuple[str, Optional[str]]:
    """Preflight for one model. Returns (status, message):
      ("ok", None)       no requirement, or CLI new enough
      ("unknown", msg)   requirement exists but CLI version undeterminable
                         (warn and allow)
      ("too_old", msg)   CLI older than required (refuse to spawn)
    """
    req = required_version(model)
    if req is None:
        return "ok", None
    _, installed, raw = get_cli_version()
    if installed is None:
        return "unknown", (
            f"cannot determine claude CLI version ({raw!r}) to verify "
            f"model {model} (needs >= {req}); allowing spawn"
        )
    if version_lt(installed, parse_version(req)):
        inst = ".".join(map(str, installed))
        return "too_old", (
            f"model {model} requires claude CLI >= {req} but installed is "
            f"{inst}"
        )
    return "ok", None
