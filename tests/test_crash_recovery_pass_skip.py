"""
Regression test for the 2026-10-01 00:48 UTC incident (task-1790732906):
17 bare "PASS" sentinel rows leaked into Discord #agent-chat on an
agent-server restart.

PASS is a sentinel the system strips before anything reaches Discord. The
live-turn posting path (process_agent_queue) correctly gates every post on
is_silence_announcement() before calling post_to_discord(). But
crash_recovery()'s separate "retry unposted responses" loop — which
reposts STATUS_COMPLETE rows that have a response but never got a
discord_response_id recorded (e.g. the process restarted between
generating the response and confirming the post) — did not go through
that gate. A stuck PASS response got blindly reposted.

This mirrors the existing empty-response bucket in crash_recovery():
rows that can never legitimately post get marked STATUS_SKIPPED instead
of being retried, so they don't clutter Discord and don't get
rediscovered on every future startup.
"""

from datetime import datetime

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server.db")
    return mod


async def _init_db(agent_server):
    await agent_server.init_db()


async def _insert_complete_unposted(agent_server, agent, channel_id, message_id, response):
    """Insert a STATUS_COMPLETE row with a response but no
    discord_response_id — the shape crash_recovery()'s retry loop looks
    for: a turn that finished but never got its Discord post confirmed."""
    created = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await agent_server.db.execute(
        """
        INSERT INTO message_queue
            (agent, channel, channel_id, author, content, message_id,
             processed, response, discord_response_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (agent, "agent-chat", channel_id, "someone", "hi", message_id,
         agent_server.STATUS_COMPLETE, response, created),
    )
    await agent_server.db.commit()


def _wire_post_to_discord(agent_server, monkeypatch):
    posted = []

    async def _fake_post_to_discord(agent, channel_id, content, reply_to=None):
        posted.append((agent, channel_id, content))
        return "discord-msg-id"

    monkeypatch.setattr(agent_server, "post_to_discord", _fake_post_to_discord)
    return posted


async def _status_of(agent_server, message_id):
    async with agent_server.db.execute(
        "SELECT processed, discord_response_id FROM message_queue WHERE message_id = ?",
        (message_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row["processed"], row["discord_response_id"]


@pytest.mark.asyncio
async def test_bare_pass_skipped_not_posted(agent_server, monkeypatch):
    """A stuck STATUS_COMPLETE row whose response is a bare 'PASS' must be
    marked STATUS_SKIPPED by crash_recovery() and must never reach
    post_to_discord() — this is the exact incident shape."""
    await _init_db(agent_server)
    await _insert_complete_unposted(agent_server, "Marvin", "chan-1", "msg-pass", "PASS")
    posted = _wire_post_to_discord(agent_server, monkeypatch)

    await agent_server.crash_recovery()

    assert posted == [], "bare PASS must never be reposted by crash recovery"
    status, discord_id = await _status_of(agent_server, "msg-pass")
    assert status == agent_server.STATUS_SKIPPED
    assert discord_id is None


@pytest.mark.asyncio
async def test_trailing_pass_variant_skipped_not_posted(agent_server, monkeypatch):
    """Same as above but for a trailing-PASS variant (a real incident
    shape per test_pass_filter.py's REAL_INCIDENT_VARIANTS) — must also
    be caught, not just the bare-token case."""
    await _init_db(agent_server)
    await _insert_complete_unposted(
        agent_server, "Marvin", "chan-1", "msg-trailing-pass", "Nothing new. PASS"
    )
    posted = _wire_post_to_discord(agent_server, monkeypatch)

    await agent_server.crash_recovery()

    assert posted == [], "trailing-PASS variant must never be reposted by crash recovery"
    status, discord_id = await _status_of(agent_server, "msg-trailing-pass")
    assert status == agent_server.STATUS_SKIPPED
    assert discord_id is None


@pytest.mark.asyncio
async def test_real_response_still_retried_and_posted(agent_server, monkeypatch):
    """A genuinely retryable row (real response text, just never confirmed
    posted) must still go through post_to_discord() and get its
    discord_response_id recorded — the fix must not catch real content in
    the PASS net."""
    await _init_db(agent_server)
    await _insert_complete_unposted(
        agent_server, "Marvin", "chan-1", "msg-real", "The deploy finished, all green."
    )
    posted = _wire_post_to_discord(agent_server, monkeypatch)

    await agent_server.crash_recovery()

    assert posted == [("Marvin", "chan-1", "The deploy finished, all green.")]
    status, discord_id = await _status_of(agent_server, "msg-real")
    assert status == agent_server.STATUS_COMPLETE
    assert discord_id == "discord-msg-id"


@pytest.mark.asyncio
async def test_mixed_batch_only_pass_rows_skipped(agent_server, monkeypatch):
    """A startup sweep with both a stuck PASS and a stuck real response
    must only post the real one, and mark only the PASS row skipped."""
    await _init_db(agent_server)
    await _insert_complete_unposted(agent_server, "Marvin", "chan-1", "msg-pass-2", "PASS")
    await _insert_complete_unposted(
        agent_server, "Marvin", "chan-1", "msg-real-2", "Backup completed successfully."
    )
    posted = _wire_post_to_discord(agent_server, monkeypatch)

    await agent_server.crash_recovery()

    assert posted == [("Marvin", "chan-1", "Backup completed successfully.")]

    pass_status, pass_discord_id = await _status_of(agent_server, "msg-pass-2")
    assert pass_status == agent_server.STATUS_SKIPPED
    assert pass_discord_id is None

    real_status, real_discord_id = await _status_of(agent_server, "msg-real-2")
    assert real_status == agent_server.STATUS_COMPLETE
    assert real_discord_id == "discord-msg-id"
