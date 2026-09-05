"""Tests for the discord tool's channel_info action (added 2026-09-05).

Every other Discord read action in tools-server.py is an echo of local
state -- "channels" reads config/channels.json, "history" reads the local
JSONL capture, "online" flatly errors. None of them ever asks Discord
itself anything, which is exactly the gap that came up twice in one
session: no way to resolve a role/member (permissions thread), and no way
to confirm a channel's live name after Ian renamed #agent-chat and asked
"is that the same channel ID" (this thread). channel_info is the one
action that makes a real GET https://discord.com/api/v10/channels/{id}
call, using the same already-authorized bot token every other Discord
action relies on -- a code gap being closed, not a new permission.

IDs below are fake placeholders, not the real snowflakes from config/
channels.json -- this repo is public, and a real ID baked into engine
test source is exactly the leak class the repo-split manifest test
guards against.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from conftest import import_script, PACKAGE_ROOT

FAKE_AGENT_CHAT_ID = "1111111111111111111"
FAKE_LOUNGE_ID = "2222222222222222222"
FAKE_GUILD_ID = "3333333333333333333"


@pytest.fixture
def tools_server(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("DISCORD_BOT_TOKEN_PRIMARY", "fake-token")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "channels.json").write_text(json.dumps({
        "channels": {
            "agent-chat": {"id": FAKE_AGENT_CHAT_ID, "guild_id": FAKE_GUILD_ID},
            "lounge": {"id": FAKE_LOUNGE_ID, "guild_id": FAKE_GUILD_ID},
        }
    }))
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def _fake_response(payload: dict):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    cm.__exit__.return_value = False
    return cm


def test_channel_info_resolves_configured_name_to_live_id_and_name(tools_server):
    """Passing a configured name (not a raw ID) resolves it to that
    channel's ID first, then queries Discord for the live name -- this is
    exactly how the agent-chat/banana-stand rename got confirmed."""
    with patch("urllib.request.urlopen", return_value=_fake_response({
        "id": FAKE_AGENT_CHAT_ID, "name": "the-banana-stand",
        "guild_id": FAKE_GUILD_ID, "type": 0,
    })) as mock_open:
        result = tools_server.handle_core_tool("discord", {
            "action": "channel_info", "channel": "agent-chat",
        })

    assert result == {
        "id": FAKE_AGENT_CHAT_ID,
        "name": "the-banana-stand",
        "guild_id": FAKE_GUILD_ID,
        "type": 0,
    }
    called_url = mock_open.call_args[0][0].full_url
    assert called_url == f"https://discord.com/api/v10/channels/{FAKE_AGENT_CHAT_ID}"


def test_channel_info_sends_user_agent_to_avoid_cloudflare_block(tools_server):
    """Real live bug hit while building this: Cloudflare in front of
    discord.com returns a bare 403 (error code 1010) for requests with
    urllib's default User-Agent, before Discord's own auth check even
    runs -- indistinguishable from a real token/permission failure unless
    you know to look for it. Must always send a real User-Agent."""
    with patch("urllib.request.urlopen", return_value=_fake_response({
        "id": "123", "name": "whatever", "guild_id": "1", "type": 0,
    })) as mock_open:
        tools_server.handle_core_tool("discord", {
            "action": "channel_info", "channel": "123",
        })

    sent_headers = mock_open.call_args[0][0].headers
    # urllib title-cases header keys internally
    assert "User-agent" in sent_headers
    assert sent_headers["User-agent"]


def test_channel_info_raw_id_bypasses_config_lookup(tools_server):
    """A raw ID not in channels.json (e.g. checking an unconfigured
    channel) must still work -- used as-is rather than erroring."""
    with patch("urllib.request.urlopen", return_value=_fake_response({
        "id": "999888777", "name": "some-other-channel",
        "guild_id": FAKE_GUILD_ID, "type": 0,
    })) as mock_open:
        result = tools_server.handle_core_tool("discord", {
            "action": "channel_info", "channel": "999888777",
        })

    assert result["name"] == "some-other-channel"
    called_url = mock_open.call_args[0][0].full_url
    assert called_url == "https://discord.com/api/v10/channels/999888777"


def test_channel_info_missing_channel_arg_errors_cleanly(tools_server):
    result = tools_server.handle_core_tool("discord", {"action": "channel_info"})
    assert "error" in result


def test_channel_info_missing_token_errors_cleanly(tools_server, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN_PRIMARY", raising=False)
    result = tools_server.handle_core_tool("discord", {
        "action": "channel_info", "channel": "agent-chat",
    })
    assert "error" in result
    assert "DISCORD_BOT_TOKEN_PRIMARY" in result["error"]


def test_channel_info_http_error_surfaces_status_not_a_crash(tools_server):
    http_error = urllib.error.HTTPError(
        url=f"https://discord.com/api/v10/channels/{FAKE_AGENT_CHAT_ID}",
        code=403, msg="Forbidden", hdrs=None, fp=None,
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        result = tools_server.handle_core_tool("discord", {
            "action": "channel_info", "channel": "agent-chat",
        })
    assert result["error"] == "Discord API error 403: Forbidden"
    assert result["queried_id"] == FAKE_AGENT_CHAT_ID
