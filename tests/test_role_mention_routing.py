"""Regression test for the 2026-09-05 role-mention routing fix in
bin/relay.py's _on_message_impl.

Before this fix, a channel with no gate_mode and no default_agent (e.g.
#lounge) silently dropped a shared-role mention (@robots) -- there was no
gating step at all for such channels, and the only routing paths were a
direct bot @mention or the channel's default_agent fallback. Setting
default_agent to fix that (tried live, then reverted same session) made the
channel always-on instead: every message, mentioned or not, started
routing.

The actual fix adds a third path: a role mention matching
channels_config["robots_role_id"] routes to *this* bot specifically
(discord_id_to_agent[self.user.id]), without touching the blanket
default_agent fallback that governs unaddressed chatter.
"""

import sys
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture
def relay():
    bin_dir = str(PACKAGE_ROOT / "bin")
    added = bin_dir not in sys.path
    if added:
        sys.path.insert(0, bin_dir)
    try:
        return import_script("relay")
    finally:
        if added:
            sys.path.remove(bin_dir)


class FakeUser:
    def __init__(self, id_, bot=True):
        self.id = id_
        self.bot = bot


class FakeRole:
    def __init__(self, id_):
        self.id = id_


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class FakeMessage:
    def __init__(self, channel_id, guild_id, author, content="",
                 mentions=None, role_mentions=None):
        self.channel = FakeChannel(channel_id)
        self.guild = FakeGuild(guild_id)
        self.author = author
        self.content = content
        self.mentions = mentions or []
        self.role_mentions = role_mentions or []


def make_adapter(relay, monkeypatch, *, self_id=999, self_agent_name="Marvin"):
    """A DiscordAdapter with __init__'s heavy discord.Client setup skipped,
    just enough state wired for _on_message_impl's routing branch."""
    adapter = relay.DiscordAdapter.__new__(relay.DiscordAdapter)
    # discord.Client.user is a read-only property backed by _connection.user
    # -- __init__ is skipped here, so fake the one attribute it reads from.
    adapter._connection = type("FakeConnection", (), {"user": FakeUser(self_id)})()
    adapter.server_ids = {"1111"}
    adapter.gate = None

    sent = []

    async def fake_send(message, agent):
        sent.append(agent)

    async def fake_capture(message):
        pass

    async def fake_sys(message):
        return False

    adapter.send_to_agent_server = fake_send
    adapter.capture_message = fake_capture
    adapter.handle_sys_command = fake_sys

    monkeypatch.setattr(relay, "discord_id_to_agent", {self_id: self_agent_name})
    monkeypatch.setattr(relay.banana, "starts_with_claim", lambda content: False)

    return adapter, sent


@pytest.fixture
def lounge_config(monkeypatch):
    """#lounge-shaped config: no gate_mode, no default_agent -- the exact
    shape that silently dropped role mentions before this fix."""
    def _apply(relay):
        monkeypatch.setattr(relay, "channels_config", {
            "robots_role_id": "555",
            "channels": {
                "lounge": {"id": "2222", "guild_id": "1111"},
            },
        })
    return _apply


@pytest.mark.asyncio
async def test_role_mention_routes_to_self_even_with_no_default_agent(relay, monkeypatch, lounge_config):
    lounge_config(relay)
    adapter, sent = make_adapter(relay, monkeypatch)

    msg = FakeMessage(
        channel_id="2222", guild_id="1111",
        author=FakeUser(1, bot=False), content="<@&555> testing",
        role_mentions=[FakeRole("555")],
    )
    await adapter._on_message_impl(msg)

    assert sent == ["Marvin"], "a robots-role mention must route to this bot even with default_agent unset"


@pytest.mark.asyncio
async def test_plain_unaddressed_message_still_does_not_route(relay, monkeypatch, lounge_config):
    """The regression this test guards against: the 2026-09-05 config-only
    fix made #lounge always-on, so plain chatter with zero mentions started
    getting a reply too. That must NOT happen -- only an explicit role
    mention (or direct bot mention) should route."""
    lounge_config(relay)
    adapter, sent = make_adapter(relay, monkeypatch)

    msg = FakeMessage(
        channel_id="2222", guild_id="1111",
        author=FakeUser(1, bot=False), content="just chatting, no mentions at all",
    )
    await adapter._on_message_impl(msg)

    assert sent == [], "unaddressed chatter in a mention-only channel must not route anywhere"


@pytest.mark.asyncio
async def test_direct_bot_mention_still_takes_priority(relay, monkeypatch, lounge_config):
    """Direct @mention of a specific known bot must still win over the
    role-mention path, same as before this change."""
    lounge_config(relay)
    adapter, sent = make_adapter(relay, monkeypatch)

    msg = FakeMessage(
        channel_id="2222", guild_id="1111",
        author=FakeUser(1, bot=False), content="<@999> and <@&555>",
        mentions=[FakeUser(999)],
        role_mentions=[FakeRole("555")],
    )
    await adapter._on_message_impl(msg)

    assert sent == ["Marvin"]


@pytest.mark.asyncio
async def test_always_on_channel_unaffected_by_role_mention_path(relay, monkeypatch):
    """#general-shaped config (default_agent set, no gate_mode): behavior
    for plain unaddressed messages must be unchanged -- still routes via
    the default_agent fallback regardless of this new branch existing."""
    monkeypatch.setattr(relay, "channels_config", {
        "robots_role_id": "555",
        "channels": {
            "general": {"id": "3333", "guild_id": "1111", "default_agent": "Marvin"},
        },
    })
    adapter, sent = make_adapter(relay, monkeypatch)

    msg = FakeMessage(
        channel_id="3333", guild_id="1111",
        author=FakeUser(1, bot=False), content="no mentions here either",
    )
    await adapter._on_message_impl(msg)

    assert sent == ["Marvin"], "always-on channels must keep working exactly as before"
