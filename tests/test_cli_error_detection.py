"""
Tests for the generic CLI-error guard added to bin/agent-server.py
2026-09-10 (agent_cli_error_blocked, _notify_cli_error, and the result-
event handling in read_agent_response).

Real incident this closes: the Claude CLI subprocess's own login/OAuth
session ("Failed to authenticate: OAuth session expired and could not be
refreshed" — unrelated to the API/session auth this harness otherwise
manages) expired, and every subsequent turn's flat `result`/`error`
string went straight through the normal reply path and got posted to
Discord verbatim, in #signals, #general, and #agent-chat alike, as though
it were the agent talking — for about 6.5 hours before anyone caught it.

This is the same failure shape test_spend_limit_detection.py already
covers for one specific string (the monthly spend-cap message), but that
fix only ever matched that one signature. This guard is deliberately
generic: any is_error result with no real assistant content gets caught,
not just known strings, so a third differently-worded CLI failure doesn't
repeat this a third time.
"""

import json

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


def _result_event(**overrides) -> bytes:
    base = {
        "type": "result",
        "usage": {},
        "total_cost_usd": 0.0,
        "duration_ms": 100,
        "is_error": False,
        "session_id": "test-session",
    }
    base.update(overrides)
    return _line(base)


REAL_LEAKED_MESSAGE = "Failed to authenticate: OAuth session expired and could not be refreshed"


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    mod = import_script("agent-server")
    mod.agent_config["TestAgent"] = {"tool_streaming": False, "stream_to_channel": False}
    mod.channels_config = {"channels": {"signals": {"id": "999888777"}}}
    return mod


@pytest.mark.asyncio
async def test_auth_failure_message_never_becomes_a_reply(agent_server):
    """The core of the fix: a real is_error flat string must not come back
    as final_text, which is what process_agent_queue posts to Discord as
    though it were a real answer."""
    agent_server.agent_processes["TestAgent"] = FakeProc(
        [_result_event(result=REAL_LEAKED_MESSAGE, is_error=True)]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "TestAgent", "999888777", []
    )
    assert final_text == ""
    assert pending_final == ""
    assert metadata["cli_error_blocked"] is True
    assert metadata["cli_error_text"] == REAL_LEAKED_MESSAGE


@pytest.mark.asyncio
async def test_detected_via_error_field_too(agent_server):
    """Same `result` OR `error` fallback ambiguity as the spend-limit
    guard — has to cover both fields."""
    agent_server.agent_processes["TestAgent"] = FakeProc(
        [_result_event(result="", error=REAL_LEAKED_MESSAGE, is_error=True)]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "TestAgent", "999888777", []
    )
    assert final_text == ""
    assert metadata["cli_error_blocked"] is True


@pytest.mark.asyncio
async def test_arbitrary_different_error_text_still_caught(agent_server):
    """The whole point of going generic instead of another hardcoded
    signature: a differently-worded CLI failure we've never seen before
    still gets caught, as long as is_error is set and there's no real
    assistant content."""
    agent_server.agent_processes["TestAgent"] = FakeProc(
        [_result_event(result="some future CLI failure nobody's seen yet", is_error=True)]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "TestAgent", "999888777", []
    )
    assert final_text == ""
    assert metadata["cli_error_blocked"] is True


@pytest.mark.asyncio
async def test_ordinary_result_unaffected(agent_server):
    """A normal terse reply (no assistant content, just a flat `result`
    string, is_error False) must NOT get flagged or suppressed."""
    agent_server.agent_processes["TestAgent"] = FakeProc(
        [_result_event(result="All done, no changes needed.")]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "TestAgent", "999888777", []
    )
    assert final_text == "All done, no changes needed."
    assert pending_final == "All done, no changes needed."
    assert "cli_error_blocked" not in metadata


@pytest.mark.asyncio
async def test_spend_limit_signature_takes_precedence_not_double_flagged(agent_server):
    """The spend-limit path and this generic path both trigger on
    is_error-with-no-content; the spend-limit branch runs first (elif
    chain) so a spend-limit message gets its own specific, more actionable
    notification instead of the generic one."""
    spend_msg = (
        "You've hit your monthly spend limit · raise it at "
        "claude.ai/settings/usage?from=cc_cli_limit_message"
    )
    agent_server.agent_processes["TestAgent"] = FakeProc(
        [_result_event(result=spend_msg, is_error=True)]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "TestAgent", "999888777", []
    )
    assert final_text == ""
    assert metadata["spend_limit_blocked"] is True
    assert "cli_error_blocked" not in metadata


@pytest.mark.asyncio
async def test_is_error_with_no_text_at_all_is_not_flagged(agent_server):
    """Edge case: is_error true but result/error both empty (final_text
    stays ""). The `elif event.get("is_error") and final_text` check
    requires actual content to flag — nothing to suppress, nothing to
    report, and the metadata block shouldn't claim there was a caught
    error string when there wasn't one."""
    agent_server.agent_processes["TestAgent"] = FakeProc(
        [_result_event(result="", error="", is_error=True)]
    )
    final_text, metadata, pending_final, _ = await agent_server.read_agent_response(
        "TestAgent", "999888777", []
    )
    assert final_text == ""
    assert "cli_error_blocked" not in metadata


class TestNotifyCliError:
    """_notify_cli_error — the #signals alert, mirroring
    _notify_spend_limit's shape."""

    @pytest.mark.asyncio
    async def test_blocked_message_pings_owner_and_includes_raw_error(self, agent_server, monkeypatch):
        posted = []

        async def fake_post(agent, channel_id, content, reply_to=None):
            posted.append((agent, channel_id, content))
            return "msg-id"

        monkeypatch.setattr(agent_server, "post_to_discord", fake_post)
        agent_server.OWNER_DISCORD_ID = "123456789"

        await agent_server._notify_cli_error("TestAgent", blocked=True, error_text=REAL_LEAKED_MESSAGE)

        assert len(posted) == 1
        agent, channel_id, content = posted[0]
        assert channel_id == "999888777"
        assert "<@123456789>" in content
        assert "OAuth session expired" in content

    @pytest.mark.asyncio
    async def test_resumed_message_has_no_ping(self, agent_server, monkeypatch):
        posted = []

        async def fake_post(agent, channel_id, content, reply_to=None):
            posted.append(content)
            return "msg-id"

        monkeypatch.setattr(agent_server, "post_to_discord", fake_post)
        agent_server.OWNER_DISCORD_ID = "123456789"

        await agent_server._notify_cli_error("TestAgent", blocked=False)

        assert len(posted) == 1
        assert "<@123456789>" not in posted[0]

    @pytest.mark.asyncio
    async def test_noop_when_no_signals_channel_configured(self, agent_server, monkeypatch):
        posted = []

        async def fake_post(agent, channel_id, content, reply_to=None):
            posted.append(content)
            return "msg-id"

        monkeypatch.setattr(agent_server, "post_to_discord", fake_post)
        agent_server.channels_config = {"channels": {}}

        await agent_server._notify_cli_error("TestAgent", blocked=True, error_text=REAL_LEAKED_MESSAGE)

        assert posted == []

    @pytest.mark.asyncio
    async def test_never_raises_on_post_failure(self, agent_server, monkeypatch):
        async def fake_post(agent, channel_id, content, reply_to=None):
            raise RuntimeError("discord is down")

        monkeypatch.setattr(agent_server, "post_to_discord", fake_post)

        await agent_server._notify_cli_error("TestAgent", blocked=True, error_text=REAL_LEAKED_MESSAGE)
