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
        """Both episodes are inserted with an inserted_at well past the
        default MEMORY_PRUNE_GRACE_DAYS (7d), so the grace period doesn't
        mask which one gets pruned — this test is about the cutoff, not
        the grace period (see TestPruneGracePeriod for that)."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        now = datetime.now(timezone.utc).isoformat()
        old_inserted = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at, inserted_at) VALUES (?, ?, ?, ?)",
            ("Old boring episode", 2.0, now, old_inserted),
        )
        conn.execute(
            "INSERT INTO episodes (summary, importance, created_at, inserted_at) VALUES (?, ?, ?, ?)",
            ("Still relevant episode", 8.0, now, old_inserted),
        )
        conn.commit()

        pruned_count = mm.prune_low_importance(conn)

        remaining = [
            row["summary"] for row in conn.execute("SELECT summary FROM episodes").fetchall()
        ]
        assert pruned_count == 1
        assert remaining == ["Still relevant episode"]
        conn.close()


class TestPruneGracePeriod:
    """Issue: same-run prune. main() used to create episodes and then delete
    everything below MEMORY_CUTOFF in the same run — the Haiku scoring
    prompt rates ordinary interactions 5-6, below the 6.0 default cutoff, so
    almost everything new was deleted the night it was made (measured
    2026-09-28: 96 created, 93 pruned, net 3 kept)."""

    def test_fresh_low_score_episode_survives_prune(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        now = datetime.now(timezone.utc).isoformat()
        now_sqlite = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Ordinary interaction", 5.5, 5.5, now, now_sqlite),
        )
        conn.commit()

        pruned = mm.prune_low_importance(conn)

        assert pruned == 0
        row = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Ordinary interaction'"
        ).fetchone()
        assert row is not None
        conn.close()

    def test_old_low_score_episode_is_pruned(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_PRUNE_GRACE_DAYS", "7")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_inserted = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        old_created = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Old boring episode", 3.0, 3.0, old_created, old_inserted),
        )
        conn.commit()

        pruned = mm.prune_low_importance(conn)

        assert pruned == 1
        row = conn.execute(
            "SELECT id FROM episodes WHERE summary = 'Old boring episode'"
        ).fetchone()
        assert row is None
        conn.close()

    def test_grace_period_is_configurable(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_PRUNE_GRACE_DAYS", "1")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        two_days_ago_inserted = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        two_days_ago_created = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Two days old", 3.0, 3.0, two_days_ago_created, two_days_ago_inserted),
        )
        conn.commit()

        # With a 1-day grace period, a 2-day-old low-score episode is prunable.
        pruned = mm.prune_low_importance(conn)
        assert pruned == 1
        conn.close()

    def test_high_score_old_episode_never_pruned(self, tmp_workspace, monkeypatch):
        """Grace period only protects new episodes — old ones above cutoff
        should never be pruned regardless of age."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_inserted = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        old_created = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Important old thing", 9.0, 9.0, old_created, old_inserted),
        )
        conn.commit()

        pruned = mm.prune_low_importance(conn)
        assert pruned == 0
        conn.close()


class TestScoreImportanceFailure:
    """Issue: scoring failure used to default to 5.0, which sits below the
    6.0 cutoff, so a Haiku hiccup silently discarded the episode. Now it
    retries once with a longer timeout, and a final failure stores a score
    at/above the cutoff and is counted in stats."""

    def test_success_on_first_attempt(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        import subprocess
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, stdout="7\n", stderr="")

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        score = mm.score_importance("some excerpt")

        assert score == 7.0
        assert len(calls) == 1

    def test_retries_once_on_failure_then_succeeds(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        import subprocess
        calls = []

        def fake_run(cmd, **kwargs):
            timeout = kwargs.get("timeout")
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, stdout="8\n", stderr="")

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        score = mm.score_importance("some excerpt")

        assert score == 8.0
        assert len(calls) == 2
        assert calls[1] > calls[0]  # retry uses the longer timeout

    def test_final_failure_stores_cutoff_safe_score_and_counts(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        stats = {"score_failures": 0}
        score = mm.score_importance("some excerpt", stats)

        assert score >= mm.IMPORTANCE_CUTOFF
        assert stats["score_failures"] == 1

    def test_score_failures_not_counted_without_stats_dict(self, tmp_workspace, monkeypatch):
        """Passing no stats dict must not raise — callers that don't care
        about the counter still work."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        mm = import_script("memory-maintenance")

        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(mm.subprocess, "run", fake_run)

        score = mm.score_importance("some excerpt")
        assert score >= mm.IMPORTANCE_CUTOFF


class TestDecayIdempotent:
    """Issue: compounding decay. decay_importance() used to subtract the
    full age-based decay from the already-decayed `importance` every night,
    so loss grew quadratically. Now it decays from `base_importance`, which
    never changes, so repeated runs converge instead of compounding."""

    def test_decay_is_idempotent_across_two_runs(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_DECAY_RATE", "0.25")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_date = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Test episode", 8.0, 8.0, old_date),
        )
        conn.commit()

        mm.decay_importance(conn)
        after_first = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Test episode'"
        ).fetchone()[0]

        mm.decay_importance(conn)
        after_second = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Test episode'"
        ).fetchone()[0]

        assert after_first == pytest.approx(after_second)
        # 8 days old at rate 0.25: (8/4)*0.25 = 0.5 off base_importance 8.0
        assert after_first == pytest.approx(7.5, abs=1e-6)
        conn.close()

    def test_decay_does_not_compound_from_stale_importance(self, tmp_workspace, monkeypatch):
        """Without base_importance, decaying an already-decayed value a
        second time would subtract age-based decay again on top of itself."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        monkeypatch.setenv("MEMORY_DECAY_RATE", "0.25")
        mm = import_script("memory-maintenance")
        conn = mm.init_db()

        old_date = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        # Simulate a row that was already decayed once (importance != base).
        conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("Already decayed once", 7.5, 8.0, old_date),
        )
        conn.commit()

        mm.decay_importance(conn)
        row = conn.execute(
            "SELECT importance FROM episodes WHERE summary = 'Already decayed once'"
        ).fetchone()
        # Recomputed from base_importance (8.0), not from the stale 7.5.
        assert row[0] == pytest.approx(7.5, abs=1e-6)
        conn.close()
