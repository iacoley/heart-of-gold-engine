"""Tests for bin/claude_bin.py — the single resolver for the claude CLI."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import claude_bin  # noqa: E402


def test_unset_uses_which_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(claude_bin.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert claude_bin.resolve_claude_bin() == "/usr/bin/claude"


def test_absolute_path_passthrough(monkeypatch, tmp_path):
    exe = tmp_path / "myclaude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(exe))
    assert claude_bin.resolve_claude_bin() == str(exe)
    assert claude_bin.claude_bin() == str(exe)


def test_bare_name_goes_through_which(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "other-claude")
    monkeypatch.setattr(claude_bin.shutil, "which", lambda n: "/opt/x/" + n)
    assert claude_bin.resolve_claude_bin() == "/opt/x/other-claude"


def test_missing_returns_none_and_message_names_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.setenv("PATH", "/some/path")
    assert claude_bin.resolve_claude_bin() is None
    msg = claude_bin.missing_message()
    assert "CLAUDE_BIN" in msg and "/nonexistent/claude" in msg and "/some/path" in msg
    # Unresolvable: falls back to the configured value (spawn fails where it
    # always did), after printing the legible message once.
    monkeypatch.setattr(claude_bin, "_warned", False)
    assert claude_bin.claude_bin() == "/nonexistent/claude"


def test_missing_on_path(monkeypatch):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(claude_bin.shutil, "which", lambda n: None)
    assert claude_bin.resolve_claude_bin() is None
