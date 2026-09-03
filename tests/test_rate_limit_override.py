"""
Tests for the rate-limit override in bin/agent-server.py — Ian's ask,
2026-08-10: "bugfixes regardless of session limits, at my discretion."

An owner-set, auto-expiring bypass of is_rate_limit_paused() for one
agent at a time. Deliberately not a permanent setting: every override is
capped at RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC no matter what duration is
requested, and expires lazily (checked on read, no background sweep
needed) so a forgotten override can't quietly disable the circuit
breaker forever.

Same import pattern as test_rate_limit_pause.py: db is None at import
(init_db/startup never run), so set_rate_limit_override() /
clear_rate_limit_override() exercise their in-memory-cache path only —
the `if db is not None` guards skip the persistence half here. That's
intentional; the DB round-trip itself is just two INSERT/DELETE
statements against a table already covered by init_db's own
CREATE TABLE IF NOT EXISTS, not logic worth a fake DB to test.
"""

import json

import pytest

from conftest import import_script


@pytest.fixture
def agent_server():
    return import_script("agent-server")


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request — the handlers under test
    only touch .headers, .match_info, .can_read_body, and await .json(),
    so a full aiohttp TestClient (which needs the pytest-aiohttp plugin,
    not installed here) is more machinery than this needs."""

    def __init__(self, headers=None, match_info=None, body=None):
        self.headers = headers or {}
        self.match_info = match_info or {}
        self._body = body
        self.can_read_body = body is not None

    async def json(self):
        return self._body


# -- is_rate_limit_override_active -------------------------------------------

def test_no_override_is_inactive(agent_server):
    assert agent_server.is_rate_limit_override_active("TestAgent") is False


def test_active_override_is_active(agent_server):
    agent_server.agent_rate_limit_overrides["TestAgent"] = {
        "enabled_by": "Ian", "reason": "test", "expires_at": agent_server.time.time() + 60,
    }
    assert agent_server.is_rate_limit_override_active("TestAgent") is True


def test_expired_override_is_inactive_and_evicted(agent_server):
    agent_server.agent_rate_limit_overrides["TestAgent"] = {
        "enabled_by": "Ian", "reason": "test", "expires_at": agent_server.time.time() - 1,
    }
    assert agent_server.is_rate_limit_override_active("TestAgent") is False
    # Lazy eviction: a second call must not find a stale entry either.
    assert "TestAgent" not in agent_server.agent_rate_limit_overrides


def test_override_is_per_agent(agent_server):
    agent_server.agent_rate_limit_overrides["AgentA"] = {
        "enabled_by": "Ian", "reason": "", "expires_at": agent_server.time.time() + 60,
    }
    assert agent_server.is_rate_limit_override_active("AgentA") is True
    assert agent_server.is_rate_limit_override_active("AgentB") is False


# -- is_rate_limit_paused with an override active ----------------------------

def test_override_bypasses_rejected_status(agent_server):
    # Nested Dict[agent, Dict[rate_limit_type, info]] shape (task-1788454188)
    # — see _rate_limit_primary() in agent-server.py.
    agent_server.agent_rate_limits["TestAgent"] = {"unknown": {"status": "rejected"}}
    assert agent_server.is_rate_limit_paused("TestAgent") is True  # sanity: paused without override
    agent_server.agent_rate_limit_overrides["TestAgent"] = {
        "enabled_by": "Ian", "reason": "urgent fix", "expires_at": agent_server.time.time() + 60,
    }
    assert agent_server.is_rate_limit_paused("TestAgent") is False


def test_override_bypasses_high_utilization(agent_server):
    agent_server.agent_rate_limits["TestAgent"] = {"unknown": {"status": "allowed", "utilization": 0.99}}
    assert agent_server.is_rate_limit_paused("TestAgent") is True  # sanity
    agent_server.agent_rate_limit_overrides["TestAgent"] = {
        "enabled_by": "Ian", "reason": "", "expires_at": agent_server.time.time() + 60,
    }
    assert agent_server.is_rate_limit_paused("TestAgent") is False


def test_expired_override_no_longer_bypasses(agent_server):
    agent_server.agent_rate_limits["TestAgent"] = {"unknown": {"status": "rejected"}}
    agent_server.agent_rate_limit_overrides["TestAgent"] = {
        "enabled_by": "Ian", "reason": "", "expires_at": agent_server.time.time() - 1,
    }
    assert agent_server.is_rate_limit_paused("TestAgent") is True


def test_override_on_one_agent_does_not_affect_another(agent_server):
    agent_server.agent_rate_limits["AgentA"] = {"unknown": {"status": "rejected"}}
    agent_server.agent_rate_limits["AgentB"] = {"unknown": {"status": "rejected"}}
    agent_server.agent_rate_limit_overrides["AgentA"] = {
        "enabled_by": "Ian", "reason": "", "expires_at": agent_server.time.time() + 60,
    }
    assert agent_server.is_rate_limit_paused("AgentA") is False
    assert agent_server.is_rate_limit_paused("AgentB") is True


# -- set_rate_limit_override / clear_rate_limit_override ---------------------

@pytest.mark.asyncio
async def test_set_override_caches_in_memory(agent_server):
    before = agent_server.time.time()
    expires_at = await agent_server.set_rate_limit_override("TestAgent", "Ian", 300, "fix a bug")
    assert expires_at > before
    assert agent_server.agent_rate_limit_overrides["TestAgent"]["enabled_by"] == "Ian"
    assert agent_server.agent_rate_limit_overrides["TestAgent"]["reason"] == "fix a bug"
    assert agent_server.is_rate_limit_paused("TestAgent") is False


@pytest.mark.asyncio
async def test_set_override_caps_duration(agent_server):
    """The actual safety mechanism: a caller asking for a week-long
    override must not get one — silently capped, not rejected outright,
    so the command still does *something* useful rather than erroring."""
    before = agent_server.time.time()
    requested = agent_server.RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC * 100
    expires_at = await agent_server.set_rate_limit_override("TestAgent", "Ian", requested, "")
    actual_duration = expires_at - before
    assert actual_duration <= agent_server.RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC + 1  # +1s test slack
    assert actual_duration > agent_server.RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC - 5


@pytest.mark.asyncio
async def test_set_override_negative_duration_floors_at_zero(agent_server):
    before = agent_server.time.time()
    expires_at = await agent_server.set_rate_limit_override("TestAgent", "Ian", -100, "")
    assert expires_at >= before  # never expires in the past


@pytest.mark.asyncio
async def test_clear_override_removes_it(agent_server):
    await agent_server.set_rate_limit_override("TestAgent", "Ian", 300, "")
    assert agent_server.is_rate_limit_override_active("TestAgent") is True
    existed = await agent_server.clear_rate_limit_override("TestAgent")
    assert existed is True
    assert agent_server.is_rate_limit_override_active("TestAgent") is False


@pytest.mark.asyncio
async def test_clear_override_on_agent_with_none_is_a_noop(agent_server):
    existed = await agent_server.clear_rate_limit_override("NeverOverridden")
    assert existed is False


@pytest.mark.asyncio
async def test_set_override_replaces_an_existing_one(agent_server):
    """A second /sys override for the same agent must update, not stack
    (this is a single-row-per-agent table, INSERT-or-replace)."""
    await agent_server.set_rate_limit_override("TestAgent", "Ian", 60, "first reason")
    await agent_server.set_rate_limit_override("TestAgent", "Ian", 300, "second reason")
    assert agent_server.agent_rate_limit_overrides["TestAgent"]["reason"] == "second reason"


# -- HTTP endpoints ------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_override_endpoint_requires_auth(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    req = _FakeRequest(
        headers={},  # no Authorization header at all
        match_info={"name": "TestAgent"},
        body={"enabled_by": "Ian"},
    )
    resp = await agent_server.handle_rate_limit_override_set(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_rate_limit_override_endpoint_wrong_token(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    req = _FakeRequest(
        headers={"Authorization": "Bearer wrong-token"},
        match_info={"name": "TestAgent"},
        body={"enabled_by": "Ian"},
    )
    resp = await agent_server.handle_rate_limit_override_set(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_rate_limit_override_endpoint_sets_override(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    req = _FakeRequest(
        headers={"Authorization": "Bearer secret-token"},
        match_info={"name": "TestAgent"},
        body={"enabled_by": "Ian", "duration_sec": 120, "reason": "test"},
    )
    resp = await agent_server.handle_rate_limit_override_set(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["status"] == "override_set"
    assert data["capped"] is False
    assert agent_server.is_rate_limit_override_active("TestAgent") is True


@pytest.mark.asyncio
async def test_rate_limit_override_endpoint_reports_capped(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    huge = agent_server.RATE_LIMIT_OVERRIDE_MAX_DURATION_SEC * 100
    req = _FakeRequest(
        headers={"Authorization": "Bearer secret-token"},
        match_info={"name": "TestAgent"},
        body={"enabled_by": "Ian", "duration_sec": huge},
    )
    resp = await agent_server.handle_rate_limit_override_set(req)
    data = json.loads(resp.text)
    assert data["capped"] is True


@pytest.mark.asyncio
async def test_rate_limit_override_endpoint_requires_enabled_by(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    req = _FakeRequest(
        headers={"Authorization": "Bearer secret-token"},
        match_info={"name": "TestAgent"},
        body={"duration_sec": 120},
    )
    resp = await agent_server.handle_rate_limit_override_set(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_rate_limit_override_endpoint_unknown_agent(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {}
    req = _FakeRequest(
        headers={"Authorization": "Bearer secret-token"},
        match_info={"name": "Nonexistent"},
        body={"enabled_by": "Ian"},
    )
    resp = await agent_server.handle_rate_limit_override_set(req)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_rate_limit_override_clear_endpoint(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    agent_server.agent_rate_limit_overrides["TestAgent"] = {
        "enabled_by": "Ian", "reason": "", "expires_at": agent_server.time.time() + 60,
    }
    req = _FakeRequest(
        headers={"Authorization": "Bearer secret-token"},
        match_info={"name": "TestAgent"},
    )
    resp = await agent_server.handle_rate_limit_override_clear(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["status"] == "override_cleared"
    assert agent_server.is_rate_limit_override_active("TestAgent") is False


@pytest.mark.asyncio
async def test_rate_limit_override_clear_endpoint_noop_when_none_active(agent_server):
    agent_server.AGENT_SERVER_TOKEN = "secret-token"
    agent_server.agent_config = {"TestAgent": {}}
    req = _FakeRequest(
        headers={"Authorization": "Bearer secret-token"},
        match_info={"name": "TestAgent"},
    )
    resp = await agent_server.handle_rate_limit_override_clear(req)
    data = json.loads(resp.text)
    assert data["status"] == "no_active_override"
