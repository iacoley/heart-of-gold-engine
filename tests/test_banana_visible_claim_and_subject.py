"""
Tests for the two gaps Zero found live in #agent-chat, 2026-09-07
("marvin-banana-protocol-brain-surgery"): the pre-post claim in
agent-server.py was real on the API side but invisible on the wire
(pending_final never got the leading 🍌 a claim conventionally shows),
and claim_self()'s subject was hardcoded to the channel config key
instead of whatever topic this turn's own outbound envelope declared.

Both fixed at the same call site (process_agent_queue's pre-post claim
block) that test_banana_pre_post_claim.py already covers for ordering;
this file covers content instead.
"""

import asyncio
from datetime import datetime

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server-visible-claim.db")
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


def _wire_common(agent_server, monkeypatch, response_text, mock_claim_self):
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

    async def _mock_read_agent_response(agent, channel_id, message_ids):
        return (response_text, {"turn": 1}, response_text, None)

    monkeypatch.setattr(agent_server, "read_agent_response", _mock_read_agent_response)

    posted = {}

    async def _mock_post_to_discord(agent, channel_id, content):
        posted["content"] = content
        return "discord-msg-999"

    async def _mock_release_self(channel):
        return True

    monkeypatch.setattr(agent_server.banana, "claim_self", mock_claim_self)
    monkeypatch.setattr(agent_server, "post_to_discord", _mock_post_to_discord)
    monkeypatch.setattr(agent_server.banana, "release_self", _mock_release_self)

    return posted


@pytest.mark.asyncio
async def test_visible_claim_prepends_emoji_when_held(agent_server, monkeypatch):
    """held_post_banana=True (a real claim) must show up on the wire, not just in the API."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")

    async def _mock_claim_self(channel, subject=None):
        return {"holder": "Marvin", "claimed": True}

    posted = _wire_common(agent_server, monkeypatch, "Plain text, no emoji typed.", _mock_claim_self)

    await agent_server.process_agent_queue("Marvin")

    assert posted["content"].startswith(agent_server.banana.CLAIM_EMOJI), (
        f"expected posted content to start with the claim emoji, got: {posted['content']!r}"
    )
    assert "Plain text, no emoji typed." in posted["content"]


@pytest.mark.asyncio
async def test_visible_claim_not_double_stamped(agent_server, monkeypatch):
    """If the model already typed the emoji itself, don't prepend a second one."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")

    async def _mock_claim_self(channel, subject=None):
        return {"holder": "Marvin", "claimed": True}

    already_stamped = f"{agent_server.banana.CLAIM_EMOJI} already have my own emoji"
    posted = _wire_common(agent_server, monkeypatch, already_stamped, _mock_claim_self)

    await agent_server.process_agent_queue("Marvin")

    assert posted["content"] == already_stamped
    assert posted["content"].count(agent_server.banana.CLAIM_EMOJI) == 1


@pytest.mark.asyncio
async def test_no_emoji_when_claim_blocked(agent_server, monkeypatch):
    """A blocked claim (held_post_banana=False) must not stamp a claim it doesn't hold."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")

    async def _mock_claim_self(channel, subject=None):
        return {"holder": "Amos", "claimed": False, "blocked": True}

    posted = _wire_common(agent_server, monkeypatch, "Text while blocked.", _mock_claim_self)

    await agent_server.process_agent_queue("Marvin")

    assert not posted["content"].startswith(agent_server.banana.CLAIM_EMOJI)
    assert posted["content"] == "Text while blocked."


@pytest.mark.asyncio
async def test_claim_subject_uses_envelope_subject_not_channel_key(agent_server, monkeypatch):
    """claim_self's subject should reflect the actual topic, not the literal channel config key."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")

    response = (
        "Some prose.\n"
        "```handoff\n"
        '{"v": 1, "kind": "status", "reply": "optional", "subject": "agora-action-items-and-tasks"}\n'
        "```"
    )

    captured_subjects = []

    async def _mock_claim_self(channel, subject=None):
        captured_subjects.append(subject)
        return {"holder": "Marvin", "claimed": True}

    _wire_common(agent_server, monkeypatch, response, _mock_claim_self)

    await agent_server.process_agent_queue("Marvin")

    assert captured_subjects == ["agora-action-items-and-tasks"], (
        f"expected the envelope's own subject, got {captured_subjects}"
    )


@pytest.mark.asyncio
async def test_claim_subject_falls_back_to_channel_name_without_envelope(agent_server, monkeypatch):
    """No envelope (or no subject field) must fail open to the old channel_name behavior."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")

    captured_subjects = []

    async def _mock_claim_self(channel, subject=None):
        captured_subjects.append(subject)
        return {"holder": "Marvin", "claimed": True}

    _wire_common(agent_server, monkeypatch, "Plain text, no envelope at all.", _mock_claim_self)

    await agent_server.process_agent_queue("Marvin")

    assert captured_subjects == ["agent-chat"], (
        f"expected fallback to channel_name 'agent-chat', got {captured_subjects}"
    )
