"""
Tests for bin/check-hf-offline.sh — task-1788562253's smoke-load
assertion that HF_HUB_OFFLINE=1 is actually honored by huggingface_hub.

No skill calls huggingface_hub directly today, but it's a real dormant
transitive dep (fastembed -> huggingface_hub, used live by
memory-maintenance.py/memory-dedup.py/voice_presence.py) and the
documented pattern for any future ML-heavy skill's own venv. This runs
the real script against the real interpreters available in this
environment rather than mocking python away entirely: the whole point
is catching a real regression (a library update, or a future skill,
silently defeating offline mode), and the actual check is fast and
deterministic -- it never loads a model or touches the network, it just
confirms a lookup for a nonexistent repo raises immediately instead of
attempting a fetch. Same "no live model call" spirit as
test_voice_presence.py's fastembed tests, satisfied here by construction
rather than by mocking, since this specific call path has no model load
to avoid.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

SCRIPT = PACKAGE_ROOT / "bin" / "check-hf-offline.sh"


def run_script(*args, timeout=15):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=timeout,
    )


class TestCheckHfOffline:
    def test_passes_against_an_interpreter_with_huggingface_hub_installed(self):
        """The real regression case this guards: huggingface_hub is a
        genuine dormant dep here (via fastembed, in requirements.txt) --
        confirm offline mode actually blocks it, today, for real.

        Uses sys.executable rather than a hardcoded .venv path: locally
        that's this repo's own .venv, in CI it's the interpreter the
        workflow just ran `pip install -r requirements.txt` into (no
        .venv exists in a fresh CI checkout at all) -- either way it's
        guaranteed to be *some* interpreter with huggingface_hub
        actually installed, which is the only thing this test needs."""
        result = run_script(sys.executable)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout

    def test_skips_cleanly_when_huggingface_hub_not_importable(self):
        """Bare system python3 (the interpreter skill scripts actually
        run under, per skills/README.md) has no huggingface_hub
        installed -- must be a clean no-op pass, not a failure, since
        there's nothing to check there."""
        system_python = "/usr/bin/python3"
        if not Path(system_python).exists():
            pytest.skip(f"{system_python} not present in this environment")
        result = run_script(system_python)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "nothing to check" in result.stdout

    def test_fails_loudly_on_missing_interpreter(self):
        result = run_script("/definitely/not/a/real/python3")
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_defaults_to_workspace_root_venv_when_no_arg_given(self, tmp_workspace):
        """No interpreter arg -- should resolve to $WORKSPACE_ROOT/.venv/
        bin/python3. Exercised against a fake workspace with that path
        symlinked to sys.executable rather than this repo's real .venv,
        since a fresh checkout (CI included) has no .venv at all -- this
        is testing the script's own default-resolution logic, not
        whether a real venv happens to exist here."""
        # A plain symlink to sys.executable breaks a real venv's
        # self-location logic (sys.prefix resolves off the invoked
        # path, not the symlink target, so site-packages silently
        # stops being found) -- exec through a tiny wrapper instead so
        # the real interpreter actually runs as itself.
        venv_python = tmp_workspace / ".venv" / "bin" / "python3"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        venv_python.chmod(0o755)

        result = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "WORKSPACE_ROOT": str(tmp_workspace)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
