"""
Tests for rate-limit headroom tracking, ported/adapted from
mcarmody/karakos-package#128 (2026-08-09).

`cost_events`/`/cost` track dollars. Dollars are not what stops an agent
mid-sentence — the rate limit is, and until now the only visibility into
it was status/utilization (see test_rate_limit_pause.py), which Amos's
instance confirmed can be entirely absent from the CLI's rate_limit_event
(no utilization field at all, ever). Window *time* progress — computed
from resetsAt + a nominal window length — is tracked as an independent,
complementary signal, and surfaced via GET /usage and /sys usage.

Unlike upstream, this doesn't add a parallel rate_limit_state table —
every field the read side needs is already on the existing `rate_limits`
row per agent (see _record_rate_limit_event in test_rate_limit_pause.py's
module), so this reuses agent_rate_limits (the in-memory mirror of that
table) instead of duplicating storage.

Same import pattern as test_rate_limit_pause.py: rate_limit_window_progress
/ format_usage_report / is_rate_limit_warning are pure enough (no event
loop, no sqlite, no subprocess) that importing the real module is safe.

2026-09-03 (task-1788454188): agent_rate_limits went from a flat
Dict[agent, info] to Dict[agent, Dict[rate_limit_type, info]], and
format_usage_report was rewritten from a single summary line to a
bracketed [SYS]-style block with one Unicode progress bar per known
window — the whole point being a seven_day reading and a five_hour
reading can now both be shown at once instead of one clobbering the
other. _set_rl() wraps a single-window info dict under its rateLimitType
key; the format_usage_report tests below assert on substrings of the new
block rather than the old single-line shape.
"""

import time

import pytest

from conftest import import_script


@pytest.fixture
def agent_server():
    return import_script("agent-server")


def _set_rl(agent_server, agent, info):
    """Populate agent_rate_limits with a single window for `agent`,
    matching the nested Dict[agent, Dict[rate_limit_type, info]] shape
    format_usage_report()/_rate_limit_primary() read (task-1788454188)."""
    agent_server.agent_rate_limits[agent] = {info.get("rateLimitType", "unknown"): dict(info)}


# ---------------------------------------------------------------------------
# rate_limit_window_progress
# ---------------------------------------------------------------------------

def test_window_progress_none_when_info_missing(agent_server):
    assert agent_server.rate_limit_window_progress(None) is None
    assert agent_server.rate_limit_window_progress({}) is None


def test_window_progress_none_when_resets_at_missing(agent_server):
    assert agent_server.rate_limit_window_progress({"rateLimitType": "five_hour"}) is None


def test_window_progress_none_when_rate_limit_type_unrecognised(agent_server):
    now = time.time()
    info = {"resetsAt": now + 100, "rateLimitType": "some_new_window_type"}
    assert agent_server.rate_limit_window_progress(info, now=now) is None


def test_window_progress_none_when_already_past(agent_server):
    """A resetsAt already in the past must render as 'unknown', not as
    100% or 0% — the window is over, the next event describes the new
    one."""
    now = time.time()
    info = {"resetsAt": now - 10, "rateLimitType": "five_hour"}
    assert agent_server.rate_limit_window_progress(info, now=now) is None


def test_window_progress_zero_at_start_of_window(agent_server):
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["five_hour"]
    info = {"resetsAt": now + window, "rateLimitType": "five_hour"}
    assert agent_server.rate_limit_window_progress(info, now=now) == 0.0


def test_window_progress_advances_toward_one_near_reset(agent_server):
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["five_hour"]
    info = {"resetsAt": now + (window * 0.1), "rateLimitType": "five_hour"}
    progress = agent_server.rate_limit_window_progress(info, now=now)
    assert progress == pytest.approx(0.9, abs=0.01)


def test_window_progress_seven_day_uses_its_own_window_length(agent_server):
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["seven_day"]
    info = {"resetsAt": now + (window * 0.75), "rateLimitType": "seven_day"}
    progress = agent_server.rate_limit_window_progress(info, now=now)
    assert progress == pytest.approx(0.25, abs=0.01)


# ---------------------------------------------------------------------------
# is_rate_limit_warning — the window-progress backstop specifically
# (status/utilization triggers are already covered by
# test_rate_limit_pause.py; these pin the addition on top of that)
# ---------------------------------------------------------------------------

def test_warning_fires_on_window_progress_alone(agent_server):
    """The case Amos's instance hits: no utilization field at all, status
    still 'allowed', but the window is almost over. Must still warn."""
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["five_hour"]
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "resetsAt": now + (window * 0.15),  # 85% through
        # no "utilization" key at all
    })
    assert agent_server.is_rate_limit_warning("TestAgent") is True
    # And still must not hard-pause — window progress is not the pause
    # criterion, only status==rejected / utilization >=95% are.
    assert agent_server.is_rate_limit_paused("TestAgent") is False


def test_no_warning_below_window_progress_threshold(agent_server):
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["five_hour"]
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "resetsAt": now + (window * 0.5),  # 50% through
    })
    assert agent_server.is_rate_limit_warning("TestAgent") is False


def test_window_progress_does_not_override_utilization_result(agent_server):
    """Sabotage check: a low window progress must not suppress a warning
    that utilization alone already earns."""
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["five_hour"]
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "resetsAt": now + (window * 0.9),  # only 10% through
        "utilization": 0.95,
    })
    assert agent_server.is_rate_limit_warning("TestAgent") is True


# ---------------------------------------------------------------------------
# format_usage_report
# ---------------------------------------------------------------------------

def test_usage_report_no_reading_yet(agent_server):
    agent_server.agent_rate_limits.pop("Ghost", None)
    report = agent_server.format_usage_report("Ghost")
    assert "No rate-limit reading yet" in report


def test_usage_report_includes_status_and_reset_time(agent_server):
    """2026-09-03 (task-1788454188): format_usage_report() is now a
    bracketed [SYS]-style block, one line per known window, rather than a
    single "status `x`, y% utilization, ..." line. The window-progress
    percentage was already dropped in favor of a real reset time back on
    2026-08-29 (see facts/usage-report-percentage-fix-2026-08-29.md) —
    that's unchanged here, just re-pinned against the new block shape."""
    now = time.time()
    window = agent_server.RATE_LIMIT_WINDOW_SECONDS["five_hour"]
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "resetsAt": now + (window * 0.4),
    })
    report = agent_server.format_usage_report("TestAgent", now=now)
    assert "[SYS] Account usage — TestAgent" in report
    assert "5-hour" in report
    assert "resets" in report


def test_usage_report_unknown_utilization_not_rendered_as_zero(agent_server):
    """The invariant upstream's PR specifically calls out: 'no reading' and
    '0% consumed' are opposite answers and must never render the same.
    2026-09-03: with the dual-bar rewrite, a window with no utilization
    reading renders its bar as all '?' and its percentage as '? %'
    instead of a filled-in 0%-looking bar."""
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed",
        "rateLimitType": "five_hour",
        # no resetsAt / utilization at all
    })
    report = agent_server.format_usage_report("TestAgent")
    assert "reset time unknown" in report
    assert "? %" in report
    assert "0%" not in report


def test_usage_report_mentions_overage(agent_server):
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed_warning",
        "rateLimitType": "five_hour",
        "isUsingOverage": True,
    })
    report = agent_server.format_usage_report("TestAgent")
    assert "extra usage" in report.lower()
    assert "active" in report.lower()


def test_usage_report_no_overage_says_none(agent_server):
    _set_rl(agent_server, "TestAgent", {"status": "allowed", "rateLimitType": "five_hour"})
    report = agent_server.format_usage_report("TestAgent")
    assert "Extra usage: none" in report


def test_usage_report_includes_utilization_when_present(agent_server):
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.42,
    })
    report = agent_server.format_usage_report("TestAgent")
    assert "42%" in report


def test_usage_report_renders_both_windows_at_once(agent_server):
    """The actual point of task-1788454188: a five_hour reading and a
    seven_day reading must both show up in the same report — the bug
    this fixed was the single-row-per-agent table letting one clobber
    the other, so only one bar could ever be shown."""
    now = time.time()
    agent_server.agent_rate_limits["TestAgent"] = {
        "five_hour": {
            "status": "allowed", "rateLimitType": "five_hour",
            "utilization": 0.62, "resetsAt": now + 3600,
        },
        "seven_day": {
            "status": "allowed", "rateLimitType": "seven_day",
            "utilization": 0.34, "resetsAt": now + 86400,
        },
    }
    report = agent_server.format_usage_report("TestAgent", now=now)
    assert "62%" in report
    assert "34%" in report
    assert "5-hour" in report
    assert "7-day" in report
    # five_hour listed before seven_day regardless of dict insertion order.
    assert report.index("5-hour") < report.index("7-day")


def test_usage_report_flags_rejected_window(agent_server):
    _set_rl(agent_server, "TestAgent", {
        "status": "rejected", "rateLimitType": "five_hour", "utilization": 0.99,
    })
    report = agent_server.format_usage_report("TestAgent")
    assert "[REJECTED]" in report


# ---------------------------------------------------------------------------
# _rate_limit_primary
# ---------------------------------------------------------------------------

def test_rate_limit_primary_none_when_nothing_recorded(agent_server):
    agent_server.agent_rate_limits.pop("Ghost", None)
    assert agent_server._rate_limit_primary("Ghost") is None


def test_rate_limit_primary_single_window_passthrough(agent_server):
    _set_rl(agent_server, "TestAgent", {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.3,
    })
    primary = agent_server._rate_limit_primary("TestAgent")
    assert primary["rateLimitType"] == "five_hour"


# ---------------------------------------------------------------------------
# Structural checks — route registration and relay wiring, matching this
# suite's existing convention (test_agent_server_routes.py) for anything
# that needs the event loop / sqlite / a real Discord client.
# ---------------------------------------------------------------------------

def test_usage_route_registered():
    from conftest import PACKAGE_ROOT
    src = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    assert 'app.router.add_get("/usage", handle_usage)' in src


def test_relay_sys_usage_command_wired():
    from conftest import PACKAGE_ROOT
    src = (PACKAGE_ROOT / "bin" / "relay.py").read_text()
    assert 'cmd == "usage"' in src
    assert "/usage" in src
