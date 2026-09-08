"""
Tests for periodic voice-reminder reinjection (2026-09-08, Ian — #general
05:35: "it wouldn't be for every agent ... but it would be for Marvin").

Root problem: agents/<agent>/persona/*.md (voice.md) only ever gets
loaded once, at subprocess start, via --append-system-prompt — see
start_agent_subprocess/load_persona_files. Over a long session that
one-time block competes with everything accumulated since, and
voice-presence logging showed flat/generic output as the dominant
failure mode despite the persona file being unchanged. This reinjects a
short, separate voice_reminder.md close to generation (appended last in
the formatted turn content sent to send_to_agent) every N turns, where N
is per-agent config (agents.json's "voice_reminder_interval") rather
than a hardcoded agent-name check — so relay (no such key) is
unaffected, matching Ian's correction that this is opt-in, not
system-wide.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server.db")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
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


def _wire_happy_path(agent_server, monkeypatch):
    """Same shape as test_entry_stale_check.py's harness: a turn that
    completes cleanly with send_to_agent's prompt captured."""
    monkeypatch.setattr(
        agent_server, "channels_config",
        {"channels": {"agent-chat": {"id": "chan-1"}}, "server_ids": ["guild-main"]},
    )
    monkeypatch.setattr(agent_server.banana, "in_scope", lambda channel_id, cfg: False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(agent_server, "start_typing", _noop)
    monkeypatch.setattr(agent_server, "stop_typing", _noop)

    sent = []

    async def _fake_send_to_agent(agent, content, message_ids, activity=None):
        sent.append(content)
        return None

    monkeypatch.setattr(agent_server, "send_to_agent", _fake_send_to_agent)

    async def _fake_read_agent_response(agent, channel_id, message_ids):
        return "hello there", {"input_tokens": 1}, "hello there", None

    monkeypatch.setattr(agent_server, "read_agent_response", _fake_read_agent_response)

    async def _fake_post_to_discord(agent, channel_id, content, reply_to=None):
        return "discord-msg-id"

    monkeypatch.setattr(agent_server, "post_to_discord", _fake_post_to_discord)
    monkeypatch.setattr(agent_server, "post_cost_update", _noop)
    monkeypatch.setattr(agent_server, "update_session_tokens", _noop)

    async def _fake_latest_id(agent, channel_id):
        return None

    monkeypatch.setattr(agent_server, "get_latest_channel_message_id", _fake_latest_id)

    async def _fake_recent(agent, channel_id, after_id=None, limit=10):
        return []

    monkeypatch.setattr(agent_server, "get_recent_channel_messages", _fake_recent)

    return sent


def _write_reminder(tmp_path, agent, text="Voice check: stay dry, not sarcastic."):
    reminder_dir = tmp_path / "agents" / agent
    reminder_dir.mkdir(parents=True, exist_ok=True)
    (reminder_dir / "voice_reminder.md").write_text(text)


@pytest.mark.asyncio
async def test_agent_without_interval_key_never_gets_reminder(agent_server, monkeypatch, tmp_path):
    """relay has no voice_reminder_interval key at all — must be a
    silent no-op, not an error, and never inject anything."""
    _write_reminder(tmp_path, "relay")
    monkeypatch.setattr(agent_server, "agent_config", {"relay": {}})
    await _init_db(agent_server, "relay")
    await _queue_message(agent_server, "relay", "chan-1", "msg-1")
    sent = _wire_happy_path(agent_server, monkeypatch)

    await agent_server.process_agent_queue("relay")

    assert "<system-reminder>" not in sent[0]


@pytest.mark.asyncio
async def test_interval_one_injects_every_turn(agent_server, monkeypatch, tmp_path):
    """The Marvin-only config Ian asked for: interval=1 fires every turn."""
    _write_reminder(tmp_path, "Marvin", text="Stay dry, not sarcastic.")
    monkeypatch.setattr(agent_server, "agent_config", {"Marvin": {"voice_reminder_interval": 1}})
    await _init_db(agent_server, "Marvin")
    sent = _wire_happy_path(agent_server, monkeypatch)

    for i in range(3):
        agent_server.agent_states["Marvin"] = "IDLE"
        await _queue_message(agent_server, "Marvin", "chan-1", f"msg-{i}")
        await agent_server.process_agent_queue("Marvin")

    assert len(sent) == 3
    for content in sent:
        assert "<system-reminder>" in content
        assert "Stay dry, not sarcastic." in content


@pytest.mark.asyncio
async def test_interval_three_only_fires_on_the_third_turn(agent_server, monkeypatch, tmp_path):
    _write_reminder(tmp_path, "Marvin")
    monkeypatch.setattr(agent_server, "agent_config", {"Marvin": {"voice_reminder_interval": 3}})
    await _init_db(agent_server, "Marvin")
    sent = _wire_happy_path(agent_server, monkeypatch)

    for i in range(3):
        agent_server.agent_states["Marvin"] = "IDLE"
        await _queue_message(agent_server, "Marvin", "chan-1", f"msg-{i}")
        await agent_server.process_agent_queue("Marvin")

    assert "<system-reminder>" not in sent[0]
    assert "<system-reminder>" not in sent[1]
    assert "<system-reminder>" in sent[2]


@pytest.mark.asyncio
async def test_reminder_is_the_last_part_of_the_prompt(agent_server, monkeypatch, tmp_path):
    """Proximity to generation is the whole point — it must land after
    the channel-routing header and the actual message content, not
    buried near the top where a long batch would dilute it again."""
    _write_reminder(tmp_path, "Marvin", text="UNIQUE_REMINDER_MARKER")
    monkeypatch.setattr(agent_server, "agent_config", {"Marvin": {"voice_reminder_interval": 1}})
    await _init_db(agent_server, "Marvin")
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")
    sent = _wire_happy_path(agent_server, monkeypatch)

    await agent_server.process_agent_queue("Marvin")

    content = sent[0]
    assert content.rstrip().endswith("</system-reminder>")
    assert content.index("UNIQUE_REMINDER_MARKER") > content.index("This turn posts ONLY to")


@pytest.mark.asyncio
async def test_missing_reminder_file_injects_nothing(agent_server, monkeypatch, tmp_path):
    """interval configured but the file doesn't exist (e.g. deleted, or
    the agent directory was never given one) — must not send an empty
    <system-reminder></system-reminder> shell."""
    monkeypatch.setattr(agent_server, "agent_config", {"Marvin": {"voice_reminder_interval": 1}})
    await _init_db(agent_server, "Marvin")
    await _queue_message(agent_server, "Marvin", "chan-1", "msg-1")
    sent = _wire_happy_path(agent_server, monkeypatch)

    await agent_server.process_agent_queue("Marvin")

    assert "<system-reminder>" not in sent[0]
