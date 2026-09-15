"""
Tests for mid-turn steering, Phase 1 (2026-08-30), added to
bin/agent-server.py following Crab Cavern's Pre-Flight Stale Gate design
(Zero's reference in #agent-chat 2026-08-29/30, prompted by the
ships-passing race where Marvin drafted a question Amos's own commit had
already answered mid-generation).

This closes a real visibility gap: process_agent_queue's own
message_queue only reflects messages that were actually queued for this
agent, which reply-gating can skip entirely. get_latest_channel_message_id
checks the channel itself via the Discord REST API, so staleness can be
detected regardless of who spoke or whether it was ever queued for us.

Deliberately Phase 1 only — detect and log, never abort or edit the
draft. This exact hot path (post_to_discord / process_agent_queue) has a
real history of subtle races (the 2000-char silent drop, the asyncio
fire-and-forget GC bug, the reload-hook self-kill), so these tests pin
down both halves of that contract: the warning fires when the channel
moved, and — just as important — the turn completes and posts normally
either way, moved or not.
"""

import asyncio
import logging
from datetime import datetime

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server.db")
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


def _wire_happy_path(agent_server, monkeypatch, snapshot_ids):
    """A turn that completes cleanly: channel resolves, banana out of
    scope (so the claim/release machinery stays out of the way), typing
    and send_to_agent stubbed, a real-shaped response returned, and the
    Discord post stubbed. snapshot_ids is consumed in order by successive
    get_latest_channel_message_id calls — [before, after]."""
    monkeypatch.setattr(
        agent_server, "channels_config",
        {"channels": {"agent-chat": {"id": "chan-1"}}, "server_ids": ["guild-main"]},
    )
    monkeypatch.setattr(agent_server.banana, "in_scope", lambda channel_id, cfg: False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(agent_server, "start_typing", _noop)
    monkeypatch.setattr(agent_server, "stop_typing", _noop)
    monkeypatch.setattr(agent_server, "send_to_agent", _noop)

    # _voice_presence_gate (task-1788316504) sits right before the Discord
    # post this file's assertions inspect. Unmocked it calls the real
    # voice_presence.gate_and_rewrite() -> a real `claude` subprocess (this
    # fixture's "Marvin" has real persona/*.md on disk, so _has_persona()
    # doesn't skip it) -- pass the text through unchanged rather than pay
    # for or depend on a live judge call none of these tests are about.
    async def _passthrough_gate(agent, channel, text):
        return text

    monkeypatch.setattr(agent_server, "_voice_presence_gate", _passthrough_gate)

    async def _fake_read_agent_response(agent, channel_id, message_ids):
        return "hello there", {"input_tokens": 1}, "hello there", None

    monkeypatch.setattr(agent_server, "read_agent_response", _fake_read_agent_response)

    posted = []

    async def _fake_post_to_discord(agent, channel_id, content, reply_to=None):
        posted.append(content)
        return "discord-msg-id"

    monkeypatch.setattr(agent_server, "post_to_discord", _fake_post_to_discord)

    async def _fake_post_cost_update(*a, **k):
        return None

    monkeypatch.setattr(agent_server, "post_cost_update", _fake_post_cost_update)
    monkeypatch.setattr(agent_server, "update_session_tokens", _noop)

    ids = list(snapshot_ids)

    async def _fake_snapshot(agent, channel_id):
        return ids.pop(0) if ids else None

    monkeypatch.setattr(agent_server, "get_latest_channel_message_id", _fake_snapshot)

    return posted


@pytest.mark.asyncio
async def test_stale_gate_logs_when_channel_moved_during_generation(agent_server, monkeypatch, caplog):
    """Core case: the channel's last message ID advanced between the
    pre-generation snapshot and the post-generation check. Must log a
    warning naming both IDs, and must still post the draft — Phase 1
    never withholds output."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")
    posted = _wire_happy_path(agent_server, monkeypatch, snapshot_ids=["1000", "2000"])

    with caplog.at_level(logging.WARNING):
        await agent_server.process_agent_queue("Marvin")

    assert posted == ["hello there"], "a moved channel must not block the post"
    stale_warnings = [r for r in caplog.records if "stale-gate" in r.message]
    assert len(stale_warnings) == 1
    assert "1000" in stale_warnings[0].message
    assert "2000" in stale_warnings[0].message
    assert "no auto-abort" in stale_warnings[0].message.lower()


@pytest.mark.asyncio
async def test_stale_gate_silent_when_channel_unchanged(agent_server, monkeypatch, caplog):
    """No new content landed while generating — must not log anything,
    so the warning stays meaningful signal rather than noise on every
    ordinary turn."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")
    posted = _wire_happy_path(agent_server, monkeypatch, snapshot_ids=["1000", "1000"])

    with caplog.at_level(logging.WARNING):
        await agent_server.process_agent_queue("Marvin")

    assert posted == ["hello there"]
    assert not [r for r in caplog.records if "stale-gate" in r.message]


@pytest.mark.asyncio
async def test_stale_gate_silent_when_snapshot_unavailable(agent_server, monkeypatch, caplog):
    """The Discord REST call for the snapshot can fail (network hiccup,
    rate limit) — get_latest_channel_message_id already swallows that and
    returns None. process_agent_queue must treat that as "nothing to
    compare against" rather than a false positive or a crash."""
    await _init_db(agent_server)
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")
    posted = _wire_happy_path(agent_server, monkeypatch, snapshot_ids=[None, "2000"])

    with caplog.at_level(logging.WARNING):
        await agent_server.process_agent_queue("Marvin")

    assert posted == ["hello there"]
    assert not [r for r in caplog.records if "stale-gate" in r.message]


@pytest.mark.asyncio
async def test_get_latest_channel_message_id_happy_path(agent_server, monkeypatch):
    agent_server.AGENT_TOKENS = {"Marvin": "test-token"}

    class FakeResp:
        status = 200

        async def json(self):
            return [{"id": "999"}]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, headers=None, params=None):
            return FakeResp()

    monkeypatch.setattr(agent_server, "http_session", FakeSession())

    result = await agent_server.get_latest_channel_message_id("Marvin", "chan-1")
    assert result == "999"


@pytest.mark.asyncio
async def test_get_latest_channel_message_id_returns_none_on_error_status(agent_server, monkeypatch):
    agent_server.AGENT_TOKENS = {"Marvin": "test-token"}

    class FakeResp:
        status = 403

        async def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, headers=None, params=None):
            return FakeResp()

    monkeypatch.setattr(agent_server, "http_session", FakeSession())

    result = await agent_server.get_latest_channel_message_id("Marvin", "chan-1")
    assert result is None


@pytest.mark.asyncio
async def test_get_latest_channel_message_id_never_raises(agent_server, monkeypatch):
    """A transport-level blowup (timeout, DNS failure) must degrade to
    None, the same posture as every other best-effort call in this file
    — this is instrumentation, it must never be the reason a turn dies."""
    agent_server.AGENT_TOKENS = {"Marvin": "test-token"}

    class FakeSession:
        def get(self, url, headers=None, params=None):
            raise ConnectionError("boom")

    monkeypatch.setattr(agent_server, "http_session", FakeSession())

    result = await agent_server.get_latest_channel_message_id("Marvin", "chan-1")
    assert result is None


@pytest.mark.asyncio
async def test_get_latest_channel_message_id_skips_silent_channel(agent_server, monkeypatch):
    """channel_id '0' is the dashboard's non-Discord silent chat — no
    channel exists to query, and this must not even attempt the call."""
    called = []

    class FakeSession:
        def get(self, *a, **k):
            called.append(True)
            raise AssertionError("must not call Discord for channel_id '0'")

    monkeypatch.setattr(agent_server, "http_session", FakeSession())

    result = await agent_server.get_latest_channel_message_id("Marvin", "0")
    assert result is None
    assert called == []
