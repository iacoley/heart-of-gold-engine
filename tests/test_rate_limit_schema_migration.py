"""
Tests for the rate_limits table's PK migration in bin/agent-server.py
(task-1788454188, 2026-09-03).

Real bug this closes: `rate_limits` used to be `agent TEXT PRIMARY KEY` —
one row per agent, period. _record_rate_limit_event() upserted on
ON CONFLICT(agent), so a seven_day rate_limit_event unconditionally
clobbered a five_hour reading and vice versa. The two windows could never
both be known at once, which was the actual blocker on ever showing a
dual-bar usage report (Amos-style [SYS] Account usage block) — see
format_usage_report() in agent-server.py.

SQLite can't ALTER a table's PRIMARY KEY in place, so init_db() detects
the old single-column-PK shape at startup and rebuilds the table under a
composite (agent, rate_limit_type) key, carrying every existing row
forward. These tests simulate a pre-migration install (hand-build the old
schema + insert rows) and confirm init_db() migrates it correctly and
non-destructively, then confirm a fresh install just gets the new schema
directly with no old-shape detour.
"""

import aiosqlite
import pytest

from conftest import import_script


@pytest.fixture
def agent_server(tmp_path, monkeypatch):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "test-agent-server.db")
    return mod


async def _pk_columns(db, table):
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        cols = await cursor.fetchall()
    return {c["name"] for c in cols if c["pk"]}


@pytest.mark.asyncio
async def test_fresh_install_gets_composite_pk_directly(agent_server):
    """No pre-existing table at all — init_db() should create the
    composite-key shape straight away, no migration detour needed."""
    await agent_server.init_db()
    pk_cols = await _pk_columns(agent_server.db, "rate_limits")
    assert pk_cols == {"agent", "rate_limit_type"}


@pytest.mark.asyncio
async def test_old_single_row_per_agent_table_is_migrated(agent_server, tmp_path):
    """Simulate a pre-2026-09-03 install: `agent TEXT PRIMARY KEY`, one
    row per agent, utilization column already present (2026-08-08
    migration). init_db() must detect this shape, rebuild under the
    composite key, and carry the row forward rather than losing it."""
    db_path = tmp_path / "test-agent-server.db"
    pre = await aiosqlite.connect(str(db_path))
    await pre.execute("""
        CREATE TABLE rate_limits (
            agent TEXT PRIMARY KEY,
            status TEXT,
            rate_limit_type TEXT,
            resets_at INTEGER,
            overage_status TEXT,
            overage_resets_at INTEGER,
            is_using_overage INTEGER DEFAULT 0,
            utilization REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await pre.execute(
        "INSERT INTO rate_limits (agent, status, rate_limit_type, resets_at, utilization) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Marvin", "allowed", "seven_day", 1999999999, 0.34),
    )
    await pre.commit()
    await pre.close()

    await agent_server.init_db()

    pk_cols = await _pk_columns(agent_server.db, "rate_limits")
    assert pk_cols == {"agent", "rate_limit_type"}

    async with agent_server.db.execute(
        "SELECT agent, rate_limit_type, status, resets_at, utilization FROM rate_limits WHERE agent = ?",
        ("Marvin",),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "pre-migration row must survive the rebuild"
    assert row["rate_limit_type"] == "seven_day"
    assert row["status"] == "allowed"
    assert row["resets_at"] == 1999999999
    assert row["utilization"] == pytest.approx(0.34)


@pytest.mark.asyncio
async def test_old_row_with_null_rate_limit_type_lands_under_unknown(agent_server, tmp_path):
    """An install that predates rate_limit_type ever being populated (or
    a row that just never got a typed event) must not produce a NULL in
    the new composite PK — NULL != NULL in SQL, so duplicate (agent, NULL)
    rows could otherwise pile up instead of upserting cleanly."""
    db_path = tmp_path / "test-agent-server.db"
    pre = await aiosqlite.connect(str(db_path))
    await pre.execute("""
        CREATE TABLE rate_limits (
            agent TEXT PRIMARY KEY,
            status TEXT,
            rate_limit_type TEXT,
            resets_at INTEGER,
            overage_status TEXT,
            overage_resets_at INTEGER,
            is_using_overage INTEGER DEFAULT 0,
            utilization REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await pre.execute(
        "INSERT INTO rate_limits (agent, status, rate_limit_type) VALUES (?, ?, NULL)",
        ("Amos", "allowed"),
    )
    await pre.commit()
    await pre.close()

    await agent_server.init_db()

    async with agent_server.db.execute(
        "SELECT rate_limit_type FROM rate_limits WHERE agent = ?", ("Amos",)
    ) as cursor:
        row = await cursor.fetchone()
    assert row["rate_limit_type"] == "unknown"


@pytest.mark.asyncio
async def test_old_table_missing_utilization_column_is_still_migrated(agent_server, tmp_path):
    """An even older install that predates the 2026-08-08 utilization
    column entirely. init_db() must add it before the copy, not crash on
    a SELECT against a column that was never there."""
    db_path = tmp_path / "test-agent-server.db"
    pre = await aiosqlite.connect(str(db_path))
    await pre.execute("""
        CREATE TABLE rate_limits (
            agent TEXT PRIMARY KEY,
            status TEXT,
            rate_limit_type TEXT,
            resets_at INTEGER,
            overage_status TEXT,
            overage_resets_at INTEGER,
            is_using_overage INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await pre.execute(
        "INSERT INTO rate_limits (agent, status, rate_limit_type) VALUES (?, ?, ?)",
        ("Marvin", "allowed", "five_hour"),
    )
    await pre.commit()
    await pre.close()

    await agent_server.init_db()  # must not raise

    async with agent_server.db.execute(
        "SELECT utilization FROM rate_limits WHERE agent = ?", ("Marvin",)
    ) as cursor:
        row = await cursor.fetchone()
    assert row["utilization"] is None


@pytest.mark.asyncio
async def test_rerunning_init_db_on_already_migrated_table_is_a_noop(agent_server):
    """A second startup after the migration already ran must not blow
    away real data — the composite-PK table already matches, so init_db()
    should just leave it alone."""
    await agent_server.init_db()
    await agent_server._record_rate_limit_event("Marvin", {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.5,
    })

    await agent_server.init_db()  # simulate a restart

    async with agent_server.db.execute(
        "SELECT utilization FROM rate_limits WHERE agent = ? AND rate_limit_type = ?",
        ("Marvin", "five_hour"),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["utilization"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_record_event_stores_both_windows_independently(agent_server):
    """The actual regression this whole migration exists to fix: a
    five_hour event followed by a seven_day event for the same agent must
    both persist, not clobber each other."""
    await agent_server.init_db()
    await agent_server._record_rate_limit_event("Marvin", {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.62,
    })
    await agent_server._record_rate_limit_event("Marvin", {
        "status": "allowed", "rateLimitType": "seven_day", "utilization": 0.34,
    })

    async with agent_server.db.execute(
        "SELECT rate_limit_type, utilization FROM rate_limits WHERE agent = ? ORDER BY rate_limit_type",
        ("Marvin",),
    ) as cursor:
        rows = await cursor.fetchall()
    by_type = {r["rate_limit_type"]: r["utilization"] for r in rows}
    assert by_type == {"five_hour": pytest.approx(0.62), "seven_day": pytest.approx(0.34)}
    assert agent_server.agent_rate_limits["Marvin"]["five_hour"]["utilization"] == pytest.approx(0.62)
    assert agent_server.agent_rate_limits["Marvin"]["seven_day"]["utilization"] == pytest.approx(0.34)


@pytest.mark.asyncio
async def test_load_rate_limits_from_db_restores_both_windows(agent_server):
    """_load_rate_limits_from_db() (startup preload) must restore every
    live window per agent, not just one."""
    await agent_server.init_db()
    await agent_server._record_rate_limit_event("Marvin", {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.7,
        "resetsAt": 9999999999,
    })
    await agent_server._record_rate_limit_event("Marvin", {
        "status": "allowed", "rateLimitType": "seven_day", "utilization": 0.2,
        "resetsAt": 9999999999,
    })
    agent_server.agent_rate_limits.clear()  # simulate a fresh process

    await agent_server._load_rate_limits_from_db()

    windows = agent_server.agent_rate_limits["Marvin"]
    assert set(windows) == {"five_hour", "seven_day"}
    assert windows["five_hour"]["utilization"] == pytest.approx(0.7)
    assert windows["seven_day"]["utilization"] == pytest.approx(0.2)
