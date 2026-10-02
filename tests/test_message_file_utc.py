"""Daily message capture files are named by UTC date, in every process.

Regression: relay (UTC) wrote messages-<utc date>.jsonl while the tools-server
MCP process (America/Los_Angeles) read messages-<local date>.jsonl, so from
17:00-24:00 Pacific the `history` action read the previous UTC day's file.
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import import_script, PACKAGE_ROOT

# 2026-10-01 22:17 PDT == 2026-10-02 05:17 UTC
FROZEN_UTC = datetime(2026, 10, 2, 5, 17, tzinfo=timezone.utc)
UTC_DAY = "2026-10-02"
LOCAL_DAY = "2026-10-01"


@pytest.fixture
def pacific_frozen(monkeypatch):
    """Process TZ = America/Los_Angeles, clock frozen at FROZEN_UTC."""
    bin_dir = str(PACKAGE_ROOT / "bin")
    monkeypatch.syspath_prepend(bin_dir)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()

    import capture

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return FROZEN_UTC.astimezone().replace(tzinfo=None)
            return FROZEN_UTC.astimezone(tz)

    monkeypatch.setattr(capture, "datetime", FrozenDatetime)
    # Sanity: local date really differs from UTC date in this setup.
    assert FrozenDatetime.now().strftime("%Y-%m-%d") == LOCAL_DAY
    yield capture
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def tools_server(tmp_path, monkeypatch, pacific_frozen):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def _write_day(tmp_path, day, rows):
    d = tmp_path / "data" / "messages"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"messages-{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )


def _row(content, channel="general"):
    return {"ts": "t", "channel_name": channel, "author_name": "a", "content": content}


def test_utc_date_str_ignores_local_tz(pacific_frozen):
    assert pacific_frozen.utc_date_str() == UTC_DAY
    assert pacific_frozen.utc_date_str(-1) == "2026-10-01"


def test_relay_writer_and_history_reader_agree(tmp_path, monkeypatch, pacific_frozen):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    relay = import_script("relay")
    tools = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")

    message = SimpleNamespace(
        id=1,
        channel=SimpleNamespace(id=5),
        author=SimpleNamespace(id=2, name="u", display_name="u", bot=False),
        content="fresh message",
        attachments=[],
        guild=None,
    )
    adapter = SimpleNamespace(get_channel_name=lambda _id: "general")
    asyncio.run(relay.DiscordAdapter.capture_message(adapter, message))

    written = list((tmp_path / "data" / "messages").glob("messages-*.jsonl"))
    assert [p.name for p in written] == [f"messages-{UTC_DAY}.jsonl"]

    result = tools.handle_core_tool("discord", {"action": "history", "channel": "general"})
    assert [m["content"] for m in result["messages"]] == ["fresh message"]


def test_capture_py_writer_matches_history_reader(tmp_path, tools_server):
    capture = sys.modules["capture"]
    path = capture.log_path_for_date(capture.utc_date_str())
    assert path.name == f"messages-{UTC_DAY}.jsonl"


def test_history_reads_utc_today_not_local_today(tmp_path, tools_server):
    _write_day(tmp_path, UTC_DAY, [_row("current")])
    _write_day(tmp_path, LOCAL_DAY, [_row("stale")] * 3)
    result = tools_server.handle_core_tool(
        "discord", {"action": "history", "channel": "general", "limit": 1}
    )
    assert [m["content"] for m in result["messages"]] == ["current"]


def test_history_falls_back_to_yesterday_newest_last(tmp_path, tools_server):
    _write_day(tmp_path, "2026-10-01", [_row("old1"), _row("old2")])
    _write_day(tmp_path, UTC_DAY, [_row("new1")])
    result = tools_server.handle_core_tool(
        "discord", {"action": "history", "channel": "general", "limit": 3}
    )
    assert [m["content"] for m in result["messages"]] == ["old1", "old2", "new1"]


def test_history_yesterday_only_when_today_missing(tmp_path, tools_server):
    _write_day(tmp_path, "2026-10-01", [_row("old1")])
    result = tools_server.handle_core_tool(
        "discord", {"action": "history", "channel": "general"}
    )
    assert [m["content"] for m in result["messages"]] == ["old1"]
