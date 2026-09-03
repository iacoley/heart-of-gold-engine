"""
Tests for Zero's parity PR: deterministic pre-post Banana claim and symmetric release.
Ensures claim_self() fires strictly before post_to_discord() and release_self() fires in finally.
"""

import asyncio
from datetime import datetime

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server-pre-post.db")
    return mod


async def _init_db(agent_server, agent="Marvin"):
    await agent_server.init_db()
    agent_server.agent_locks[agent] = asyncio.Lock()
    agent_server.agent_states[agent] = "IDLE"


async def _queue_message(agent_server, agent, channel_id, message_id):
    created = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await agent_server.db.execute(
        """
        INSERT INTO message_queue
            (agent, channel, channel_id, author, content, message_id, processed, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (agent, "agent-chat", channel_id, "someone", "hi", message_id,
         agent_server.STATUS_QUEUED, created),
    )
    await agent_server.db.commit()


@pytest.mark.asyncio
async def test_pre_post_claim_and_release_order(agent_server, monkeypatch):
    """Verify that claim_self executes before post_to_discord, and release_self executes after."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")

    monkeypatch.setattr(
        agent_server, "channels_config",
        {"channels": {"agent-chat": {"id": "chan-1"}}, "server_ids": ["guild-main"]},
    )
    monkeypatch.setattr(agent_server.banana, "in_scope", lambda channel_id, cfg: True)
    monkeypatch.setattr(
        agent_server.banana, "get_status",
        lambda channel: {"active": False, "holder": None},
    )

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(agent_server, "start_typing", _noop)
    monkeypatch.setattr(agent_server, "stop_typing", _noop)
    monkeypatch.setattr(agent_server, "send_to_agent", _noop)

    # Return a response WITHOUT leading 🍌 to prove deterministic harness behavior
    async def _mock_read_agent_response(agent, channel_id, message_ids):
        return ("Plain text response with no leading emoji", {"turn": 1}, "Plain text response with no leading emoji", None)

    monkeypatch.setattr(agent_server, "read_agent_response", _mock_read_agent_response)

    call_order = []

    async def _mock_claim_self(channel, subject=None):
        call_order.append("claim_self")
        return {"holder": "Marvin", "claimed": True}

    async def _mock_post_to_discord(agent, channel_id, content):
        call_order.append("post_to_discord")
        return "discord-msg-999"

    async def _mock_release_self(channel):
        call_order.append("release_self")
        return True

    monkeypatch.setattr(agent_server.banana, "claim_self", _mock_claim_self)
    monkeypatch.setattr(agent_server, "post_to_discord", _mock_post_to_discord)
    monkeypatch.setattr(agent_server.banana, "release_self", _mock_release_self)

    await agent_server.process_agent_queue("Marvin")

    assert call_order == ["claim_self", "post_to_discord", "release_self"], (
        f"Expected ['claim_self', 'post_to_discord', 'release_self'], got {call_order}"
    )
