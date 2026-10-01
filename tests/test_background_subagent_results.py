"""
Issue #34: multiple `result` events when a background subagent finishes.

Fixture tests/fixtures/bg_subagent_stream.jsonl is a trimmed real capture
(`claude -p --output-format stream-json --verbose`, 2026-10-01): one
invocation emitted result #1 ("I've sent the question to the subagent...")
then, after the task_notification, a system init + a second turn + result
#2 ("4"). Only bulky/irrelevant fields (init payload, thinking signatures,
modelUsage) were stripped.
"""

import asyncio
import json
import pathlib

import pytest

from conftest import import_script

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "bg_subagent_stream.jsonl"


def _load():
    return [json.loads(l) for l in FIXTURE.read_text().splitlines() if l.strip()]


def _enc(events):
    return [(json.dumps(e) + "\n").encode() for e in events]


class FakeStdout:
    def __init__(self, lines, hang_at_end=False):
        self._lines = list(lines)
        self._hang = hang_at_end

    async def readline(self):
        if not self._lines:
            if self._hang:
                await asyncio.sleep(3600)
            return b""
        return self._lines.pop(0)


class FakeProc:
    def __init__(self, lines, hang_at_end=False):
        self.stdout = FakeStdout(lines, hang_at_end)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    mod = import_script("agent-server")
    mod.agent_config["Marvin"] = {"tool_streaming": False, "stream_to_channel": False}
    mod.channels_config = {"channels": {"signals": {"id": "111222333"}}}
    posts = []

    async def fake_post(agent, channel_id, text, *a, **k):
        posts.append(text)
        return f"msg{len(posts)}"

    monkeypatch.setattr(mod, "post_to_discord", fake_post)
    mod._posts = posts
    return mod


def _results(events):
    return [e for e in events if e["type"] == "result"]


@pytest.mark.asyncio
async def test_single_result_unaffected(srv):
    ev = _load()
    # plain turn: two top-level assistants then one result, no background task
    plain = [e for e in ev[:2]] + [_results(ev)[0]]
    srv.agent_processes["Marvin"] = FakeProc(_enc(plain))
    text, meta, pending, _ = await srv.read_agent_response("Marvin", "111222333", [])
    assert text.startswith("I've sent the question to the subagent")
    assert pending == text
    assert srv._posts == []  # caller posts pending_final, not us


@pytest.mark.asyncio
async def test_real_capture_yields_both_segments(srv):
    ev = _load()
    srv.agent_processes["Marvin"] = FakeProc(_enc(ev))
    text, meta, pending, last_id = await srv.read_agent_response("Marvin", "111222333", [])
    r1, r2 = (r["result"] for r in _results(ev))
    # first segment posted exactly once, inline, as plain text
    assert srv._posts == [r1]
    # caller only posts the unposted remainder: the follow-up answer
    assert pending == r2 == "4"
    # full text kept for history, in order, no duplication
    assert text == f"{r1}\n\n{r2}"
    # stream fully consumed: nothing left to be misread by the next message
    assert srv.agent_processes["Marvin"].stdout._lines == []
    # subagent's own assistant text never leaks
    assert "2 + 2" not in text


@pytest.mark.asyncio
async def test_cost_not_double_counted(srv):
    ev = _load()
    srv.agent_processes["Marvin"] = FakeProc(_enc(ev))
    _, meta, _, _ = await srv.read_agent_response("Marvin", "111222333", [])
    r1, r2 = _results(ev)
    # total_cost_usd is session-cumulative: last value, not the sum
    assert meta["total_cost_usd"] == r2["total_cost_usd"]
    assert meta["output_tokens"] == r1["usage"]["output_tokens"] + r2["usage"]["output_tokens"]
    assert meta["duration_ms"] == r1["duration_ms"] + r2["duration_ms"]


@pytest.mark.asyncio
async def test_result_while_task_still_running_waits_for_followup(srv):
    """Result arrives BEFORE the task finishes (the long-running case)."""
    ev = _load()
    idx = [i for i, e in enumerate(ev) if e["type"] == "result"][0]
    notif = [e for e in ev[:idx] if e.get("subtype") in ("task_updated", "task_notification")]
    body = [e for e in ev[:idx] if e not in notif]
    reordered = body + [ev[idx]] + notif + ev[idx + 1:]
    srv.agent_processes["Marvin"] = FakeProc(_enc(reordered))
    text, _, pending, _ = await srv.read_agent_response("Marvin", "111222333", [])
    assert srv._posts == [_results(ev)[0]["result"]]
    assert pending == "4"
    assert srv.agent_processes["Marvin"].stdout._lines == []


@pytest.mark.asyncio
async def test_no_followup_times_out_without_hanging(srv, monkeypatch):
    monkeypatch.setattr(srv, "BG_FOLLOWUP_GRACE_SEC", 0.05)
    ev = _load()
    idx = [i for i, e in enumerate(ev) if e["type"] == "result"][0]
    srv.agent_processes["Marvin"] = FakeProc(_enc(ev[: idx + 1]), hang_at_end=True)
    text, meta, pending, _ = await srv.read_agent_response("Marvin", "111222333", [])
    # interim already posted once; nothing re-posted by the caller
    assert srv._posts == [_results(ev)[0]["result"]]
    assert pending == ""
    assert text == _results(ev)[0]["result"]
    assert meta["total_cost_usd"] == _results(ev)[0]["total_cost_usd"]


@pytest.mark.asyncio
async def test_error_result_does_not_wait(srv):
    ev = _load()
    idx = [i for i, e in enumerate(ev) if e["type"] == "result"][0]
    bad = dict(ev[idx], is_error=True)
    srv.agent_processes["Marvin"] = FakeProc(_enc(ev[:idx] + [bad] + ev[idx + 1:]))
    _, meta, _, _ = await srv.read_agent_response("Marvin", "111222333", [])
    assert meta.get("cli_error_blocked") is True
    assert srv._posts == []
    # the follow-up segment was left unread (error path breaks immediately)
    assert srv.agent_processes["Marvin"].stdout._lines
