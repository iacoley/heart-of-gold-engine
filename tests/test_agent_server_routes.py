"""
Smoke tests for bin/agent-server.py routing surface.

The full server is heavy (event loop + sqlite + subprocesses) so we avoid
booting it. These tests parse the source and check that the routes
required by the rest of the system are wired up.
"""

import ast
from pathlib import Path

from conftest import PACKAGE_ROOT

AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"


def test_agent_server_parses():
    ast.parse(AGENT_SERVER.read_text())


def test_register_endpoint_wired():
    """bin/create-agent.sh POSTs to /agents/{name}/register — the server
    must register a handler for that route or the hot-load is dead."""
    src = AGENT_SERVER.read_text()
    assert 'app.router.add_post("/agents/{name}/register", handle_agent_register)' in src
    assert "async def handle_agent_register" in src


def test_register_endpoint_validates_name():
    """Hot-register accepts a path-segment name, so it must reject anything
    outside [a-zA-Z0-9_-] before touching disk."""
    src = AGENT_SERVER.read_text()
    assert "_AGENT_NAME_RE" in src
    assert "Invalid agent name" in src


def test_register_endpoint_reloads_config():
    """The handler must call load_config() so the new agent's entry in
    agents.json actually shows up before we try to spawn it."""
    src = AGENT_SERVER.read_text()
    register_idx = src.index("async def handle_agent_register")
    next_def = src.index("\nasync def ", register_idx + 1)
    body = src[register_idx:next_def]
    assert "await load_config()" in body
    assert "start_agent_subprocess" in body


def test_existing_routes_still_present():
    """Don't accidentally clobber the routes we already had."""
    src = AGENT_SERVER.read_text()
    for route in [
        '"/message"',
        '"/health"',
        '"/agents"',
        '"/agents/{name}/reset"',
        '"/agents/{name}/reload"',
        '"/cost"',
    ]:
        assert route in src, f"missing route: {route}"


def test_create_agent_script_targets_register():
    """The script and the server must agree on the endpoint path."""
    create_agent = PACKAGE_ROOT / "bin" / "create-agent.sh"
    assert create_agent.exists()
    body = create_agent.read_text()
    assert "/agents/$AGENT_NAME/register" in body


def test_crash_recovery_commits_unposted_retry_per_message():
    """Regression test for the duplicate-post race: crash_recovery()'s
    "unposted responses" retry loop must commit right after each
    successful post, not once after the whole loop. A batched commit
    means a crash partway through the loop leaves earlier
    already-posted-to-Discord messages with discord_response_id still
    NULL, so the next startup's sweep finds and reposts them — the
    recovery path duplicating exactly what it exists to prevent.

    Checked structurally (source order + indentation) rather than by
    running the server, matching this file's existing style — booting
    agent-server.py's full event loop + sqlite + subprocess stack is out
    of scope for a unit test.
    """
    src = AGENT_SERVER.read_text()

    marker = "Retry posting messages that completed but weren't posted"
    start = src.index(marker)
    next_section = src.index("\n# ====", start)
    section = src[start:next_section]

    execute_idx = section.index("await db.execute(", section.index("if discord_id:"))
    commit_idx = section.index("await db.commit()", execute_idx)

    # The commit must be the very next statement after the UPDATE call
    # closes — i.e. still nested inside "if discord_id:" and therefore
    # inside "for msg in unposted:", not dedented back out to loop level
    # or function level.
    between = section[execute_idx:commit_idx]
    assert between.count("\n") <= 4, (
        "db.commit() has drifted away from the per-message UPDATE — "
        "if it moved back outside the loop, the duplicate-post race "
        "this test guards against is back"
    )

    execute_line = section[:execute_idx].count("\n")
    commit_line = section[:commit_idx].count("\n")
    execute_indent = len(section.splitlines()[execute_line]) - len(
        section.splitlines()[execute_line].lstrip()
    )
    commit_indent = len(section.splitlines()[commit_line]) - len(
        section.splitlines()[commit_line].lstrip()
    )
    assert commit_indent == execute_indent, (
        "db.commit() must be indented exactly as deep as the 'await "
        "db.execute(' statement it follows — a dedent means it moved "
        "back outside the loop"
    )


def test_handle_agents_reports_config_not_just_runtime_state():
    """GET /agents backs dashboard/app/api/agents/config/route.ts, which
    feeds the settings page (2026-08-08, ported from upstream
    mcarmody/karakos-package#130). The settings page renders
    cfg.model/cfg.max_turns/cfg.timeout — if handle_agents() stops
    reporting any of those, the page silently shows "—" for everything
    again instead of failing loudly, so pin the fields here instead.

    max_turns must fall back to 200 (the same default start_agent
    actually launches with) rather than surfacing a bare
    config.get("max_turns") — an agent configured by omission should
    not read as "not configured" on the settings page.
    """
    src = AGENT_SERVER.read_text()
    start = src.index("async def handle_agents")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]

    assert '"max_turns": config.get("max_turns", 200)' in body
    assert '"timeout": config.get("timeout")' in body


def test_voice_presence_scoring_gated_on_persona():
    """Found 2026-09-09: _voice_presence_log() fired for every agent
    unconditionally, including relay, which has no agents/relay/persona/
    directory and was never meant to have one. Every relay reply got
    judged against JUDGE_VOICE_DESCRIPTION's hardcoded "Marvin's voice"
    rubric and failed 100% of the time (43/43 in the judge-verified
    data) — that's not a voice-presence signal, it's a category error,
    and it single-handedly dragged the aggregate flag rate up enough to
    make a real problem (none) look severe. Pin the guard so scoring an
    agent with no persona to hold it to can't silently regress back in."""
    src = AGENT_SERVER.read_text()
    assert "def _has_persona(agent: str) -> bool:" in src

    start = src.index("async def _voice_presence_log")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    assert "_has_persona(agent)" in body
    assert "return" in body.split("_has_persona(agent)")[1].split("\n")[1]
