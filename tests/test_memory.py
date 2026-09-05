"""
Tests for bin/memory-maintenance.py — Memory consolidation and decay.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import import_script


class TestMemoryDatabaseInit:
    """Test memory database initialization."""

    def test_creates_tables(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn = mm.init_db()

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        assert "episodes" in tables
        assert "facts" in tables
        assert "patterns" in tables
        conn.close()

    def test_tables_have_expected_columns(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn = mm.init_db()

        cursor = conn.execute("PRAGMA table_info(episodes)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "summary" in columns
        assert "importance" in columns
        assert "created_at" in columns
        assert "embedding" in columns
        conn.close()

    def test_init_is_idempotent(self, tmp_workspace, monkeypatch):
        """Calling init_db twice should not error."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        conn1 = mm.init_db()
        conn1.close()
        conn2 = mm.init_db()
        conn2.close()


class TestMemoryDecay:
    """Test episode importance decay.

    Rewritten 2026-09-05 (debloat pass, task from Ian): both tests here used
    to insert a row and then re-derive the decay/cutoff arithmetic inline in
    the test itself, without ever calling the real decay_importance()/
    prune_low_importance() in bin/memory-maintenance.py. That meant the two
    actual functions had zero test coverage anywhere in the suite (confirmed
    via grep) despite tests existing with their names in the docstrings.
    Rewritten to call the real functions against a real mm.init_db()
    connection, same pattern TestMemoryDatabaseInit already uses correctly.
    """

    def test_decay_reduces_importance_by_the_documented_formula(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_date = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Test episode", 8.0, old_date),
        )
        conn.commit()

        decayed_count = mm.decay_importance(conn)

        row = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Test episode'"
        ).fetchone()
        assert decayed_count == 1
        # docstring formula: effective = importance - (days_old / 4 * DECAY_RATE)
        # 4 days old, default DECAY_RATE=0.25 -> lose exactly 0.25
        assert row["importance"] == pytest.approx(7.75)
        conn.close()

    def test_decay_does_not_touch_episodes_at_or_below_cutoff(self, tmp_workspace, monkeypatch):
        """decay_importance's own query is `WHERE importance > cutoff` --
        an episode already at/below the cutoff is left for prune_low_importance
        instead, not decayed further."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Old boring episode", 2.0, old_date),
        )
        conn.commit()

        decayed_count = mm.decay_importance(conn)

        row = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Old boring episode'"
        ).fetchone()
        assert decayed_count == 0
        assert row["importance"] == 2.0
        conn.close()

    def test_prune_low_importance_removes_only_episodes_below_cutoff(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Old boring episode", 2.0, now),
        )
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at) VALUES (?, ?, ?)",
            ("Still relevant episode", 8.0, now),
        )
        conn.commit()

        pruned_count = mm.prune_low_importance(conn)

        remaining = [
            row["summary"] for row in conn.execute("SELECT summary FROM episodes").fetchall()
        ]
        assert pruned_count == 1
        assert remaining == ["Still relevant episode"]
        conn.close()
