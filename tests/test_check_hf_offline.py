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
    def test_passes_against_repo_venv_where_huggingface_hub_is_installed(self):
        """The real regression case this guards: huggingface_hub is a
        genuine dormant dep in this repo's own .venv (via fastembed) --
        confirm offline mode actually blocks it, today, for real."""
        result = run_script()
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

    def test_defaults_to_repo_venv_when_no_arg_given(self):
        """No interpreter arg -- should resolve to $WORKSPACE_ROOT/.venv,
        which does have huggingface_hub installed here."""
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "WORKSPACE_ROOT": str(PACKAGE_ROOT)},
        )
        assert result.returncode == 0
        assert "OK" in result.stdout
