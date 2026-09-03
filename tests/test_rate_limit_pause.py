"""
Tests for the rate-limit pause/compaction logic in bin/agent-server.py
(is_rate_limit_paused, maybe_rate_limit_compact).

Unlike test_agent_server_routes.py, these import the real module rather
than parsing source — is_rate_limit_paused()/maybe_rate_limit_compact()
are pure enough (no event loop, no sqlite, no subprocess of their own)
that importing is safe and lets us simulate actual Anthropic
rate_limit_info payloads instead of just checking the source shape.
Nothing in this module starts the server at import time — main()/startup()
are only called from `if __name__ == "__main__"` / app.on_startup, neither
of which fires on import.

2026-09-03 (task-1788454188): agent_rate_limits went from a flat
Dict[agent, info] to Dict[agent, Dict[rate_limit_type, info]] so a
seven_day reading no longer clobbers a five_hour one (see
_rate_limit_primary() in agent-server.py). These tests only ever
populate a single window per agent, so _set_rl() below wraps each
`info` dict under its own rateLimitType (or "unknown" if the payload
doesn't have one) — is_rate_limit_paused/is_rate_limit_warning read
through _rate_limit_primary(), which with only one window present just
returns that window, preserving the exact behavior these tests pin.
"""

import pytest

from conftest import import_script


@pytest.fixture
def agent_server():
    return import_script("agent-server")


def _set_rl(agent_server, agent, info):
    """Populate agent_rate_limits with a single window for `agent`,
    matching the nested Dict[agent, Dict[rate_limit_type, info]] shape
    _rate_limit_primary() reads (task-1788454188)."""
    agent_server.agent_rate_limits[agent] = {info.get("rateLimitType", "unknown"): dict(info)}


# Simulated Anthropic rate_limit_info payloads, matching the real shape
# observed live (see memory fact ratelimit-freeze-2026-08-07: status,
# utilization, overageInUse, surpassedThreshold, isUsingOverage, resetsAt).
#
# 2026-08-08 update (second revision, per Ian): status=="allowed_warning"
# used to hard-pause on its own for rateLimitType=="five_hour". Anthropic
# sets that status around 90% utilization, which jammed a real session
# with plenty of window left — "we shouldn't stop work just at 90%".
# status is now ONLY a warning signal (is_rate_limit_warning) with no
# blocking effect; is_rate_limit_paused is the sole hard stop and is
# utilization-only, threshold 0.95 (lowered from 0.97 2026-08-29, see
# facts/rate-limit-pause-threshold-95pct-2026-08-29.md), regardless of
# status or window type.
@pytest.mark.parametrize("label,info,expected", [
    ("healthy", {"status": "allowed", "utilization": 0.42}, False),
    ("five_hour warning alone does NOT hard-pause anymore",
     {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.91}, False),
    ("seven_day warning does NOT hard-pause",
     {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.77}, False),
    ("warning with no rateLimitType at all does NOT hard-pause",
     {"status": "allowed_warning", "utilization": 0.91}, False),
    ("utilization only, status still allowed",
     {"status": "allowed", "utilization": 0.985, "overageInUse": True}, True),
    ("exactly at the utilization threshold", {"status": "allowed", "utilization": 0.95}, True),
    ("just under the utilization threshold", {"status": "allowed", "utilization": 0.949999}, False),
    ("five_hour warning past utilization threshold pauses on utilization",
     {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.99}, True),
    ("seven_day warning past utilization threshold pauses on utilization",
     {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.99}, True),
    ("empty/missing info (e.g. right after startup)", {}, False),
])
def test_is_rate_limit_paused(agent_server, label, info, expected):
    _set_rl(agent_server, "TestAgent", info)
    assert agent_server.is_rate_limit_paused("TestAgent") is expected, label


@pytest.mark.parametrize("label,info,expected", [
    ("healthy, well under warning", {"status": "allowed", "utilization": 0.42}, False),
    ("anthropic status flag alone warns", {"status": "allowed_warning", "utilization": 0.5}, True),
    ("utilization alone crosses 90%", {"status": "allowed", "utilization": 0.91}, True),
    ("just under the warning threshold", {"status": "allowed", "utilization": 0.899999}, False),
    ("in warning zone but nowhere near the hard stop",
     {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.5}, True),
    ("empty/missing info", {}, False),
])
def test_is_rate_limit_warning(agent_server, label, info, expected):
    _set_rl(agent_server, "TestAgent", info)
    assert agent_server.is_rate_limit_warning("TestAgent") is expected, label


def test_warning_does_not_imply_paused(agent_server):
    """Core of the 2026-08-08 fix: crossing Anthropic's ~90% warning
    status must NOT hold the queue by itself. Only the 97% utilization
    threshold does."""
    _set_rl(agent_server, "TestAgent", {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.91})
    assert agent_server.is_rate_limit_warning("TestAgent") is True
    assert agent_server.is_rate_limit_paused("TestAgent") is False


def test_utilization_key_present_but_none_does_not_crash(agent_server):
    """Regression for the 2026-08-08 deploy: dict.get(key, default) only
    applies the default when the key is ABSENT, not when it's present
    with value None. A DB row restored right after the utilization
    column migration (or before any live event has populated it) has
    exactly this shape and briefly took down check_queued_acks() /
    process_agent_queue() with 'None >= float' on the first restart after
    this fix shipped. Must resolve to falsy, not raise."""
    _set_rl(agent_server, "TestAgent", {"status": "allowed", "utilization": None})
    assert agent_server.is_rate_limit_warning("TestAgent") is False
    assert agent_server.is_rate_limit_paused("TestAgent") is False


def test_rejected_status_hard_pauses_even_without_utilization(agent_server):
    """Live 2026-08-08: Marvin and relay both hit status=="rejected" right
    at the tail of a five_hour window, with utilization absent from the
    payload both times (confirmed against Anthropic's own CLI schema —
    utilization is optional and often missing). "rejected" means a
    request was ALREADY denied — stronger than "allowed_warning" — so it
    must hard-pause on its own, not rely on a utilization number that
    frequently isn't there."""
    _set_rl(agent_server, "TestAgent", {
        "status": "rejected", "rateLimitType": "five_hour",
        "overageStatus": "rejected", "isUsingOverage": False,
    })
    assert agent_server.is_rate_limit_warning("TestAgent") is True
    assert agent_server.is_rate_limit_paused("TestAgent") is True


def test_rejected_status_pauses_regardless_of_low_utilization(agent_server):
    """Even if a stray/incorrect low utilization number ever showed up
    alongside a rejection, the rejection itself is definitive — it must
    still pause."""
    _set_rl(agent_server, "TestAgent", {"status": "rejected", "utilization": 0.1})
    assert agent_server.is_rate_limit_paused("TestAgent") is True


def test_rejected_status_wins_over_a_higher_utilization_window():
    """New in 2026-09-03 (task-1788454188): with two windows tracked at
    once, _rate_limit_primary() must pick a rejected window over a
    merely-high-utilization one, even though the selection rule
    otherwise picks by max utilization — 'already denied' is a stronger
    signal than any utilization number."""
    agent_server = import_script("agent-server")
    agent_server.agent_rate_limits["TestAgent"] = {
        "five_hour": {"status": "rejected", "rateLimitType": "five_hour", "utilization": 0.5},
        "seven_day": {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.99},
    }
    assert agent_server.is_rate_limit_paused("TestAgent") is True
    primary = agent_server._rate_limit_primary("TestAgent")
    assert primary["rateLimitType"] == "five_hour"


def test_primary_picks_higher_utilization_window_absent_rejection():
    """Two windows, neither rejected — the one closer to the pause
    threshold governs the single-value gates."""
    agent_server = import_script("agent-server")
    agent_server.agent_rate_limits["TestAgent"] = {
        "five_hour": {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.2},
        "seven_day": {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.6},
    }
    primary = agent_server._rate_limit_primary("TestAgent")
    assert primary["rateLimitType"] == "seven_day"


@pytest.mark.asyncio
async def test_maybe_rate_limit_compact_fires_at_threshold(agent_server, monkeypatch):
    calls = []

    async def fake_compact_session(agent, reason):
        calls.append((agent, reason))
        return True

    monkeypatch.setattr(agent_server, "compact_session", fake_compact_session)

    _set_rl(agent_server, "TestAgent", {"status": "allowed", "utilization": 0.42})
    assert await agent_server.maybe_rate_limit_compact("TestAgent", already_compacted=False) is False
    assert calls == []

    _set_rl(agent_server, "TestAgent", {"status": "allowed", "utilization": 0.98})
    assert await agent_server.maybe_rate_limit_compact("TestAgent", already_compacted=False) is True
    assert calls == [("TestAgent", "rate-limit utilization")]


@pytest.mark.asyncio
async def test_maybe_rate_limit_compact_skips_if_already_compacted(agent_server, monkeypatch):
    """No point paying for a second finalize+restart in the same turn if
    the token-target trigger already compacted this session."""
    calls = []

    async def fake_compact_session(agent, reason):
        calls.append((agent, reason))
        return True

    monkeypatch.setattr(agent_server, "compact_session", fake_compact_session)

    _set_rl(agent_server, "TestAgent", {"status": "allowed", "utilization": 0.99})
    result = await agent_server.maybe_rate_limit_compact("TestAgent", already_compacted=True)
    assert result is False
    assert calls == []
