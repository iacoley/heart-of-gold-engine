"""
Tests for the queued-ack dedup fix in bin/agent-server.py
(channel_last_acked_message_id / check_queued_acks), added 2026-08-18.

Real incident this closes: during the 05:56-08:27 UTC monthly spend-limit
outage, the same stuck queued message in #agent-chat got a fresh
"-# ⏳ queued — paused — five-hour rate limit window..." post every time
QUEUED_ACK_COOLDOWN_SEC (10min) elapsed, for as long as that message
stayed at the head of the queue — ten near-identical posts over 2.5
hours. The cooldown was designed to space out acks about *different*
queued messages (the back-to-back-busy-turns case, see the comment above
QUEUED_ACK_WAIT_THRESHOLD_SEC), not to gate repeats of the same message.

Fix: track which message_id a channel's ack was actually for, and skip
re-sending once that exact message has already been acked, regardless of
how much time has passed. A genuinely different queued message still
gets its own ack.
"""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server.db")
    return mod


async def _init_db(agent_server):
    """pytest-asyncio here runs in strict mode with no async fixtures
    configured elsewhere in this suite, so db setup happens explicitly
    inside each test rather than via an async fixture."""
    await agent_server.init_db()
    agent_server.agent_locks["TestAgent"] = asyncio.Lock()


async def _queue_message(agent_server, agent, channel_id, message_id, age_sec):
    created = (datetime.utcnow() - timedelta(seconds=age_sec)).strftime("%Y-%m-%d %H:%M:%S")
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


def _fake_post(posts):
    async def _post(agent, channel_id, content, reply_to=None):
        posts.append(content)
        return "fake-msg-id"
    return _post


@pytest.mark.asyncio
async def test_same_stuck_message_only_acked_once(agent_server, monkeypatch):
    """Core regression: a message that's still queued after the cooldown
    elapses again (e.g. stuck behind a rate-limit pause) must not get
    re-acked just because time passed — only a *different* queued
    message should trigger a new post."""
    await _init_db(agent_server)
    posts = []
    monkeypatch.setattr(agent_server, "post_to_discord", _fake_post(posts))
    monkeypatch.setattr(agent_server, "is_rate_limit_paused", lambda agent: True)
    # Nested Dict[agent, Dict[rate_limit_type, info]] shape (task-1788454188)
    # — see _rate_limit_primary() in agent-server.py, which the ack-reason
    # path in check_queued_acks() reads through.
    agent_server.agent_rate_limits["TestAgent"] = {"unknown": {"resetsAt": None}}

    await _queue_message(agent_server, "TestAgent", "chan-1", "msg-1", age_sec=100)

    await agent_server.check_queued_acks()
    assert len(posts) == 1
    assert "queued" in posts[0]

    # Simulate the cooldown having elapsed again with the SAME message
    # still sitting at the head of the queue (still STATUS_QUEUED).
    agent_server.channel_last_ack["chan-1"] = (
        time.time() - agent_server.QUEUED_ACK_COOLDOWN_SEC - 1
    )

    await agent_server.check_queued_acks()
    assert len(posts) == 1, (
        "same message_id must not be re-acked just because the cooldown elapsed"
    )


@pytest.mark.asyncio
async def test_new_message_in_same_channel_still_gets_its_own_ack(agent_server, monkeypatch):
    """The fix must not over-correct into 'one ack per channel ever' — a
    genuinely different queued message (the case the cooldown was
    actually designed for: back-to-back busy turns) still needs its own
    notice once the wait threshold and cooldown both clear."""
    await _init_db(agent_server)
    posts = []
    monkeypatch.setattr(agent_server, "post_to_discord", _fake_post(posts))
    monkeypatch.setattr(agent_server, "is_rate_limit_paused", lambda agent: False)

    await _queue_message(agent_server, "TestAgent", "chan-1", "msg-1", age_sec=100)
    await agent_server.check_queued_acks()
    assert len(posts) == 1

    # First message finally drains; a new, different message queues up
    # behind a fresh busy turn and ages past the wait threshold.
    await agent_server.db.execute(
        "UPDATE message_queue SET processed = 1 WHERE message_id = ?", ("msg-1",)
    )
    await agent_server.db.commit()
    await _queue_message(agent_server, "TestAgent", "chan-1", "msg-2", age_sec=100)

    agent_server.channel_last_ack["chan-1"] = (
        time.time() - agent_server.QUEUED_ACK_COOLDOWN_SEC - 1
    )

    await agent_server.check_queued_acks()
    assert len(posts) == 2, "a distinct new queued message should still get its own ack"


@pytest.mark.asyncio
async def test_message_under_wait_threshold_not_acked(agent_server, monkeypatch):
    """Unrelated to the dedup fix, but guards against breaking the
    existing 45s anti-spam gate while touching this function."""
    await _init_db(agent_server)
    posts = []
    monkeypatch.setattr(agent_server, "post_to_discord", _fake_post(posts))
    monkeypatch.setattr(agent_server, "is_rate_limit_paused", lambda agent: False)

    await _queue_message(agent_server, "TestAgent", "chan-1", "msg-1", age_sec=5)
    await agent_server.check_queued_acks()
    assert posts == []
