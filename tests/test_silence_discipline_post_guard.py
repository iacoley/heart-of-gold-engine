"""post_to_discord must never let blank content reach the Discord API.

Real incident, 2026-09-04: two visible blank messages (content == "")
landed in #general at 00:03:40 and 00:03:41, caught by Ian, not by any
existing check. This is the same silence-discipline failure class as
task-1788365086 ("a decision to say nothing produced actual text"), but
a distinct mechanism: split_discord_message() already drops a *literal*
empty string (`[text] if text else []` -> `[]`, see
test_discord_split.py::test_empty_text_produces_no_chunks), so a true ""
never reaches the HTTP call. What got through was whitespace-only
content, which is truthy in Python and survives that check to produce a
one-element chunk list.

Four prior fixes for this bug class lived only in persona/voice.md and
all four relapsed (2026-08-29 x3, 2026-09-02, this is the fifth). The
common failure was fixing the model's judgment instead of verifying the
result before it left the process. This guard sits in post_to_discord
itself — every posting path funnels through it, so it can't be bypassed
by a caller nobody thought to check.
"""

import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server.db")
    mod.AGENT_TOKENS = {"Marvin": "test-token"}
    return mod


class _ExplodingSession:
    """Any HTTP call at all is the bug — the guard must return before
    http_session is ever touched."""

    def post(self, *a, **k):
        raise AssertionError("post_to_discord made an HTTP call for blank content")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blank",
    [
        "",
        " ",
        "   ",
        "\n",
        "\n\n\t  \n",
        " ",  # non-breaking space — real-world whitespace variant
    ],
)
async def test_blank_content_is_never_posted(agent_server, monkeypatch, blank):
    monkeypatch.setattr(agent_server, "http_session", _ExplodingSession())
    result = await agent_server.post_to_discord("Marvin", "chan-1", blank)
    assert result is None


@pytest.mark.asyncio
async def test_real_content_still_posts(agent_server, monkeypatch):
    """The guard must not become a new way to silently drop a real reply."""

    class FakeResp:
        status = 200

        async def json(self):
            return {"id": "msg-123"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def post(self, url, headers=None, json=None):
            assert json["content"] == "hello there"
            return FakeResp()

    monkeypatch.setattr(agent_server, "http_session", FakeSession())
    result = await agent_server.post_to_discord("Marvin", "chan-1", "hello there")
    assert result == "msg-123"


@pytest.mark.asyncio
async def test_content_with_real_text_and_surrounding_whitespace_still_posts(
    agent_server, monkeypatch
):
    """Only fully-blank content is suppressed — don't get greedy and eat
    leading/trailing whitespace off a real message too."""

    class FakeResp:
        status = 200

        async def json(self):
            return {"id": "msg-456"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(agent_server, "http_session", FakeSession())
    result = await agent_server.post_to_discord("Marvin", "chan-1", "  hi  ")
    assert result == "msg-456"
