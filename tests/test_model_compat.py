"""Tests for bin/model_compat.py and agent-server's CLI/model preflight."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import model_compat as mc  # noqa: E402
from conftest import import_script  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    mc._cache.clear()
    yield
    mc._cache.clear()


def fake_cli(monkeypatch, tmp_path, output="2.1.287 (Claude Code)", calls=None):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(exe))

    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr(mc.subprocess, "run", run)
    return exe


def test_parse_version():
    assert mc.parse_version("2.1.287 (Claude Code)") == (2, 1, 287)
    assert mc.parse_version("garbage") is None
    assert mc.parse_version("") is None


def test_compare_is_numeric_per_component():
    assert not mc.version_lt((2, 1, 280), (2, 1, 99))
    assert mc.version_lt((2, 1, 99), (2, 1, 280))
    assert mc.version_lt((2, 1, 222), (2, 1, 280))
    assert not mc.version_lt((2, 1, 280), (2, 1, 280))
    assert not mc.version_lt((2, 1, 280, 0), (2, 1, 280))
    assert mc.version_lt((2, 1), (2, 1, 1))


def test_table_lookup():
    assert mc.required_version("claude-opus-5-5") == "2.1.280"
    assert mc.required_version("claude-opus-5-5[1m]") == "2.1.280"
    assert mc.required_version("claude-opus-5-50") is None
    for m in ("haiku", "sonnet", "opus", "claude-sonnet-5-5", "", None):
        assert mc.required_version(m) is None


def test_too_old(monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "2.1.222 (Claude Code)")
    status, msg = mc.check_model("claude-opus-5-5")
    assert status == "too_old"
    assert "claude-opus-5-5" in msg and "2.1.280" in msg and "2.1.222" in msg


def test_new_enough_and_no_requirement(monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "2.1.287 (Claude Code)")
    assert mc.check_model("claude-opus-5-5") == ("ok", None)
    mc._cache.clear()
    fake_cli(monkeypatch, tmp_path, "2.1.1 (Claude Code)")
    assert mc.check_model("sonnet") == ("ok", None)


def test_unparseable_or_missing_warns_and_allows(monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "weird output")
    assert mc.check_model("claude-opus-5-5")[0] == "unknown"
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    assert mc.check_model("claude-opus-5-5")[0] == "unknown"


def test_version_cached(monkeypatch, tmp_path):
    calls = []
    fake_cli(monkeypatch, tmp_path, calls=calls)
    mc.check_model("claude-opus-5-5")
    mc.check_model("claude-opus-5-5")
    assert len(calls) == 1


# --- agent-server integration ------------------------------------------------

@pytest.fixture
def agent_server():
    return import_script("agent-server")


def _setup(agent_server, monkeypatch, model):
    monkeypatch.setitem(agent_server.agent_config, "marvin", {"model": model, "system_prompt": "x"})
    agent_server.agent_spawn_refused.clear()
    agent_server.agent_spawn_refused_alerted.clear()
    posts = []

    async def post(agent, channel, content, reply_to=None):
        posts.append((channel, content))

    async def fail_session(agent):
        raise AssertionError("spawn path reached")

    spawned = []
    monkeypatch.setattr(agent_server, "post_to_discord", post)
    monkeypatch.setattr(agent_server, "get_or_create_session", fail_session)
    monkeypatch.setattr(agent_server, "_spawn", lambda coro: spawned.append(coro))
    monkeypatch.setattr(agent_server, "channels_config", {"channels": {"signals": {"id": "123"}}})
    return posts, spawned


def test_too_old_refuses_spawn_and_alerts_once(agent_server, monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "2.1.222 (Claude Code)")
    posts, spawned = _setup(agent_server, monkeypatch, "claude-opus-5-5")

    asyncio.run(agent_server.start_agent_subprocess("marvin"))
    asyncio.run(agent_server.start_agent_subprocess("marvin"))  # reload: no second alert

    assert "marvin" not in agent_server.agent_processes
    assert agent_server.agent_states["marvin"] == "IDLE"
    assert "2.1.222" in agent_server.agent_spawn_refused["marvin"]
    assert len(spawned) == 1  # one #signals alert
    for c in spawned:
        c.close()


def test_unknown_version_does_not_refuse(agent_server, monkeypatch, tmp_path):
    fake_cli(monkeypatch, tmp_path, "weird")
    _setup(agent_server, monkeypatch, "claude-opus-5-5")
    # Passing preflight proceeds to the spawn path (sentinel raises there).
    with pytest.raises(AssertionError, match="spawn path reached"):
        asyncio.run(agent_server.start_agent_subprocess("marvin"))
    assert "marvin" not in agent_server.agent_spawn_refused
