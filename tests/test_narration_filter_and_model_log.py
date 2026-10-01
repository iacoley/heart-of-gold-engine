"""
Tests for narration filtering, parent_tool_use_id suppression, and active model observability.
Covering PR 1a (heart-of-gold-engine).
"""

import json
import logging
import pytest
from conftest import import_script


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)


class FakeProc:
    def __init__(self, lines):
        self.stdout = FakeStdout(lines)


def _line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


def _assistant_event(content: list, parent_tool_use_id: str = None, model: str = None) -> bytes:
    payload = {
        "type": "assistant",
        "message": {
            "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }
    if parent_tool_use_id:
        payload["parent_tool_use_id"] = parent_tool_use_id
    if model:
        payload["message"]["model"] = model
    return _line(payload)


def _result_event(result: str = "", model: str = None, **overrides) -> bytes:
    base = {
        "type": "result",
        "result": result,
        "usage": {},
        "total_cost_usd": 0.0,
        "duration_ms": 100,
        "is_error": False,
        "session_id": "test-session",
    }
    if model:
        base["model"] = model
    base.update(overrides)
    return _line(base)


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    mod = import_script("agent-server")
    mod.agent_config["Marvin"] = {"tool_streaming": False, "stream_to_channel": False}
    mod.channels_config = {"channels": {"signals": {"id": "111222333"}}}
    return mod


@pytest.mark.asyncio
async def test_subagent_events_with_parent_tool_use_id_are_ignored(agent_server):
    """Subagent assistant events tagged with parent_tool_use_id must not leak into final_text."""
    agent_server.agent_processes["Marvin"] = FakeProc(
        [
            _assistant_event([{"type": "text", "text": "Top-level plan."}]),
            # Nested subagent output from Task or Agent tool:
            _assistant_event(
                [{"type": "text", "text": "Subagent internal monologue."}],
                parent_tool_use_id="toolu_subagent_123",
            ),
            _result_event(result="Top-level plan completed."),
        ]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "Marvin", "111222333", []
    )

    assert "Subagent internal monologue." not in final_text
    assert final_text == "Top-level plan completed."
    assert pending_final == "Top-level plan completed."


@pytest.mark.asyncio
async def test_result_string_supersedes_interstitial_narration(agent_server):
    """When result event provides a clean result string, it supersedes accumulated interim text."""
    agent_server.agent_processes["Marvin"] = FakeProc(
        [
            _assistant_event([{"type": "text", "text": "I'll start by examining the files..."}]),
            _assistant_event([{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
            _assistant_event([{"type": "text", "text": "Files look good."}]),
            _result_event(result="Here is the final verified answer."),
        ]
    )
    final_text, _, pending_final, _ = await agent_server.read_agent_response(
        "Marvin", "111222333", []
    )

    assert final_text == "Here is the final verified answer."
    assert pending_final == "Here is the final verified answer."
    assert "I'll start by examining the files..." not in final_text


@pytest.mark.asyncio
async def test_active_model_logged_and_captured_in_metadata(agent_server, caplog):
    """The model actually running in the assistant event is captured in metadata and logged."""
    agent_server.agent_processes["Marvin"] = FakeProc(
        [
            _assistant_event(
                [{"type": "text", "text": "All set."}],
                model="claude-opus-4-6",
            ),
            _result_event(result="All set."),
        ]
    )
    with caplog.at_level(logging.INFO):
        _, metadata, _, _ = await agent_server.read_agent_response(
            "Marvin", "111222333", []
        )

    assert metadata.get("model") == "claude-opus-4-6"
    assert "Marvin running active model: claude-opus-4-6" in caplog.text
