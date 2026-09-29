"""Tests for bin/channel-name-check.py — keeping config/channels.json's
cached Discord display name in sync with the live channel name.

Incident (2026-09-29): the channel this repo calls `agent-chat`
internally had been renamed `the-banana-stand` on Discord's side with
nothing here aware of it. These tests exercise the freshness/drift check
without ever making a real network call.
"""

import json

import pytest

from conftest import import_script


@pytest.fixture
def cnc(tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    return import_script("channel-name-check")


def _write_channels(tmp_workspace, channels):
    cfg_dir = tmp_workspace / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "channels.json").write_text(json.dumps({"channels": channels}))


def test_no_cached_name_yet_is_not_drift(cnc, tmp_workspace, monkeypatch):
    """First run: nothing cached to compare against, so populating the
    cache for the first time must not be reported as a drift."""
    _write_channels(tmp_workspace, {
        "agent-chat": {"id": "111", "guild_id": "999"},
    })
    monkeypatch.setattr(cnc, "fetch_channel_name", lambda channel_id, token: "the-banana-stand")

    ok, message = cnc.check_channel_names(token="fake-token")

    assert ok is True
    assert message == ""

    # Cache should now be populated for next time.
    cfg = json.loads((tmp_workspace / "config" / "channels.json").read_text())
    assert cfg["channels"]["agent-chat"]["discord_name"] == "the-banana-stand"


def test_matching_cached_name_is_healthy(cnc, tmp_workspace, monkeypatch):
    _write_channels(tmp_workspace, {
        "agent-chat": {"id": "111", "guild_id": "999", "discord_name": "the-banana-stand"},
    })
    monkeypatch.setattr(cnc, "fetch_channel_name", lambda channel_id, token: "the-banana-stand")

    ok, message = cnc.check_channel_names(token="fake-token")

    assert ok is True
    assert message == ""


def test_renamed_channel_is_flagged_and_cache_updated(cnc, tmp_workspace, monkeypatch):
    """The actual incident shape: cached name says one thing, Discord
    says another. Must be reported AND the cache must self-heal so this
    doesn't fire again next run for the same rename."""
    _write_channels(tmp_workspace, {
        "agent-chat": {"id": "111", "guild_id": "999", "discord_name": "agent-chat"},
    })
    monkeypatch.setattr(cnc, "fetch_channel_name", lambda channel_id, token: "the-banana-stand")

    ok, message = cnc.check_channel_names(token="fake-token")

    assert ok is False
    assert "agent-chat" in message
    assert "the-banana-stand" in message

    cfg = json.loads((tmp_workspace / "config" / "channels.json").read_text())
    assert cfg["channels"]["agent-chat"]["discord_name"] == "the-banana-stand"

    # Re-running against the now-updated cache is healthy again.
    ok2, message2 = cnc.check_channel_names(token="fake-token")
    assert ok2 is True
    assert message2 == ""


def test_channels_without_id_are_skipped(cnc, tmp_workspace, monkeypatch):
    _write_channels(tmp_workspace, {
        "no-id-here": {"guild_id": "999"},
    })
    calls = []
    monkeypatch.setattr(
        cnc, "fetch_channel_name",
        lambda channel_id, token: calls.append(channel_id) or "whatever",
    )

    ok, message = cnc.check_channel_names(token="fake-token")

    assert ok is True
    assert calls == []


def test_fetch_failure_for_one_channel_does_not_abort_the_rest(cnc, tmp_workspace, monkeypatch):
    _write_channels(tmp_workspace, {
        "broken": {"id": "111", "guild_id": "999"},
        "fine": {"id": "222", "guild_id": "999", "discord_name": "fine"},
    })

    def fake_fetch(channel_id, token):
        if channel_id == "111":
            raise RuntimeError("HTTP 404 fetching channel 111: Not Found")
        return "fine"

    monkeypatch.setattr(cnc, "fetch_channel_name", fake_fetch)

    ok, message = cnc.check_channel_names(token="fake-token")

    assert ok is False
    assert "broken" in message
    assert "404" in message
    # The healthy channel's cache is untouched and doesn't appear in the
    # failure message.
    cfg = json.loads((tmp_workspace / "config" / "channels.json").read_text())
    assert cfg["channels"]["fine"]["discord_name"] == "fine"


def test_no_token_is_not_a_failure(cnc, tmp_workspace, monkeypatch):
    """No Discord bot token configured shouldn't make this check itself
    the reason a headless run reports unhealthy -- that's a separate,
    already-covered concern elsewhere."""
    _write_channels(tmp_workspace, {
        "agent-chat": {"id": "111", "guild_id": "999"},
    })
    monkeypatch.delenv("DISCORD_BOT_TOKEN_PRIMARY", raising=False)

    ok, message = cnc.check_channel_names(token=None)

    assert ok is True
    assert message == ""
