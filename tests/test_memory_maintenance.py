"""
Tests for bin/memory-maintenance.py's Track 1 curated-memory work
(task-1788154168 / docs/design/curated-memory-layer.md).

Before this, memory-maintenance.py's `facts` table was fully wired in
init_db() — schema, indexes — and never once inserted into. The manual
MEMORY.md/facts/*.md layer was the only place facts ever landed, which
is why a repeatedly-mentioned term (Crab Cavern) never got indexed:
nothing promoted episodes into facts automatically.

These tests cover the new pipeline: extract_candidate_fact() (mocked,
no real Haiku calls), the deterministic citation_is_valid() gate,
insert_candidate_fact(), write_candidates_file(), and the
extract_facts_from_episodes() driver that ties them together. Also
covers the process_messages_to_episodes() signature change (now
returns (count, new_episodes) instead of just count) since that's a
breaking change to an existing function other callers rely on.
"""

import json

import pytest

from conftest import import_script, PACKAGE_ROOT


@pytest.fixture
def mm(monkeypatch, tmp_workspace):
    """Import memory-maintenance.py with WORKSPACE_ROOT pointed at a
    scratch dir, and MEMORY_DIR/MEMORY_DB re-derived to match (the
    module computes these at import time from WORKSPACE_ROOT)."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    module = import_script("memory-maintenance", file_path=PACKAGE_ROOT / "bin" / "memory-maintenance.py")
    return module


@pytest.fixture
def conn(mm):
    c = mm.init_db()
    yield c
    c.close()


def _insert_episode(conn, summary="Ian: Crab Cavern is the second Discord server."):
    cursor = conn.execute(
        "INSERT INTO episodes (summary, importance, channel, created_at) VALUES (?, ?, ?, ?)",
        (summary, 6.0, "general", "2026-08-31T00:00:00+00:00"),
    )
    conn.commit()
    return cursor.lastrowid


class TestCitationIsValid:
    def test_real_episode_id_is_valid(self, mm, conn):
        episode_id = _insert_episode(conn)
        assert mm.citation_is_valid(conn, episode_id) is True

    def test_nonexistent_episode_id_is_invalid(self, mm, conn):
        assert mm.citation_is_valid(conn, 999999) is False


class TestExtractCandidateFact:
    def test_none_response_yields_no_candidate(self, mm, monkeypatch):
        fake = type("R", (), {"stdout": "NONE", "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)
        assert mm.extract_candidate_fact(1, "just chatting, nothing definitional") is None

    def test_valid_json_response_yields_candidate_stamped_with_real_episode_id(self, mm, monkeypatch):
        payload = json.dumps({
            "subject": "Crab Cavern",
            "content": "The second Discord server, home to #agent-chat and #lounge.",
            "domain": "glossary",
        })
        fake = type("R", (), {"stdout": payload, "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)

        candidate = mm.extract_candidate_fact(42, "Ian: Crab Cavern is the second Discord server.")

        assert candidate is not None
        assert candidate["subject"] == "Crab Cavern"
        assert candidate["episode_id"] == 42, "episode_id must come from the caller, never the model's output"

    def test_malformed_json_is_treated_as_no_candidate_not_a_crash(self, mm, monkeypatch):
        fake = type("R", (), {"stdout": "not json at all {{{", "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)
        assert mm.extract_candidate_fact(1, "whatever") is None

    def test_empty_subject_or_content_is_rejected(self, mm, monkeypatch):
        payload = json.dumps({"subject": "", "content": "", "domain": "general"})
        fake = type("R", (), {"stdout": payload, "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)
        assert mm.extract_candidate_fact(1, "whatever") is None

    def test_subprocess_exception_is_swallowed_not_raised(self, mm, monkeypatch):
        def boom(*a, **k):
            raise TimeoutError("simulated hang")
        monkeypatch.setattr(mm.subprocess, "run", boom)
        assert mm.extract_candidate_fact(1, "whatever") is None


class TestInsertCandidateFact:
    def test_inserts_row_with_citation_in_content(self, mm, conn):
        episode_id = _insert_episode(conn)
        candidate = {
            "subject": "Crab Cavern",
            "content": "The second Discord server.",
            "domain": "glossary",
            "episode_id": episode_id,
        }
        fact_id = mm.insert_candidate_fact(conn, candidate)

        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        assert row["subject"] == "Crab Cavern"
        assert f"episode {episode_id}" in row["content"]
        assert row["domain"] == "glossary"


class TestWriteCandidatesFile:
    def test_empty_list_writes_nothing(self, mm):
        assert mm.write_candidates_file([]) is None

    def test_nonempty_list_writes_readable_file(self, mm, tmp_workspace):
        candidates = [{
            "subject": "Crab Cavern",
            "content": "The second Discord server.",
            "domain": "glossary",
            "episode_id": 7,
            "fact_id": 1,
        }]
        path = mm.write_candidates_file(candidates)
        assert path is not None
        assert path.exists()
        text = path.read_text()
        assert "Crab Cavern" in text
        assert "episode 7" in text


class TestExtractFactsFromEpisodes:
    def test_accepted_candidate_is_inserted_and_counted(self, mm, conn, monkeypatch):
        episode_id = _insert_episode(conn)
        payload = json.dumps({
            "subject": "Crab Cavern",
            "content": "The second Discord server.",
            "domain": "glossary",
        })
        fake = type("R", (), {"stdout": payload, "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)

        stats = mm.extract_facts_from_episodes(
            conn, [{"id": episode_id, "summary": "Ian: Crab Cavern is...", "channel": "general"}]
        )

        assert stats["facts_extracted"] == 1
        assert stats["facts_rejected_citation"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"] == 1

    def test_no_candidate_extracted_is_not_an_error(self, mm, conn, monkeypatch):
        fake = type("R", (), {"stdout": "NONE", "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)
        episode_id = _insert_episode(conn)

        stats = mm.extract_facts_from_episodes(
            conn, [{"id": episode_id, "summary": "just chatting", "channel": "general"}]
        )

        assert stats["facts_extracted"] == 0
        assert stats["candidates_file"] is None

    def test_bad_citation_is_rejected_not_inserted(self, mm, conn, monkeypatch):
        """Defense in depth: even if something upstream fed a candidate
        citing an episode id that doesn't exist, it must not reach the
        facts table."""
        payload = json.dumps({"subject": "X", "content": "Y", "domain": "general"})
        fake = type("R", (), {"stdout": payload, "returncode": 0})()
        monkeypatch.setattr(mm.subprocess, "run", lambda *a, **k: fake)

        # episode "999" was never inserted into the episodes table
        stats = mm.extract_facts_from_episodes(
            conn, [{"id": 999, "summary": "whatever", "channel": "general"}]
        )

        assert stats["facts_extracted"] == 0
        assert stats["facts_rejected_citation"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"] == 0


class TestProcessMessagesToEpisodesReturnShape:
    def test_returns_tuple_of_count_and_episode_records(self, mm, tmp_workspace, monkeypatch):
        """process_messages_to_episodes() used to return just an int
        count; Track 1 needs the new episode ids too, so the signature
        changed to (count, [{"id", "summary", "channel"}, ...])."""
        monkeypatch.setattr(mm, "score_importance", lambda summary, stats=None: 6.0)

        import datetime as _dt
        yesterday = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        messages_dir = tmp_workspace / "data" / "messages"
        messages_dir.mkdir(parents=True, exist_ok=True)
        msg_file = messages_dir / f"messages-{yesterday}.jsonl"
        msg_file.write_text(json.dumps({
            "ts": f"{yesterday}T12:00:00+00:00",
            "author_name": "Ian",
            "content": "Crab Cavern is the second Discord server.",
            "is_bot": False,
            "channel_name": "general",
        }) + "\n")

        conn = mm.init_db()
        try:
            count, new_episodes = mm.process_messages_to_episodes(conn)
            assert count == 1
            assert len(new_episodes) == 1
            assert isinstance(new_episodes[0]["id"], int)
            assert new_episodes[0]["id"] > 0
        finally:
            conn.close()

    def test_no_messages_returns_empty_tuple(self, mm):
        conn = mm.init_db()
        try:
            count, new_episodes = mm.process_messages_to_episodes(conn)
            assert count == 0
            assert new_episodes == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Own messages in summaries, multi-day lookback, decay and prune
# ---------------------------------------------------------------------------

import datetime as _dt


def _day(offset):
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=offset)).strftime("%Y-%m-%d")


def _write_day(tmp_workspace, offset, text="hello", is_bot=False):
    d = tmp_workspace / "data" / "messages"
    d.mkdir(parents=True, exist_ok=True)
    day = _day(offset)
    (d / f"messages-{day}.jsonl").write_text(json.dumps({
        "ts": f"{day}T12:00:00+00:00", "author_name": "Ian", "content": text,
        "is_bot": is_bot, "channel_name": "general",
    }) + "\n")
    return day


class TestOwnMessagesInSummary:
    def test_bot_messages_included_and_labelled(self, mm):
        summary = mm.create_episode_summary([
            {"author_name": "Ian", "content": "Ship it?", "is_bot": False},
            {"author_name": "Marvin", "content": "Decision: shipping.", "is_bot": True},
        ])
        assert "Ian: Ship it?" in summary
        assert "[agent] Marvin: Decision: shipping." in summary

    def test_empty_content_skipped(self, mm):
        assert mm.create_episode_summary([{"author_name": "Bot", "content": "", "is_bot": True}]) == ""


class TestMultiDayLookback:
    def test_processes_missed_days_once(self, mm, tmp_workspace, monkeypatch):
        monkeypatch.setattr(mm, "score_importance", lambda s, stats=None: 7.0)
        _write_day(tmp_workspace, 1, "day one")
        _write_day(tmp_workspace, 2, "day two")
        conn = mm.init_db()
        try:
            # init_db seeds day-2/day-3 as processed on a brand-new table; clear
            # to simulate a missed nightly run with tracking already in place.
            conn.execute("DELETE FROM processed_days")
            conn.commit()
            count, new = mm.process_messages_to_episodes(conn)
            assert count == 2
            assert [r[0] for r in conn.execute("SELECT summary FROM episodes ORDER BY id")] == [
                "Ian: day two", "Ian: day one"]
            count2, new2 = mm.process_messages_to_episodes(conn)
            assert (count2, new2) == (0, [])
            assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2
        finally:
            conn.close()

    def test_ignores_today_and_days_beyond_window(self, mm, tmp_workspace, monkeypatch):
        monkeypatch.setattr(mm, "score_importance", lambda s, stats=None: 7.0)
        _write_day(tmp_workspace, 0, "today")
        _write_day(tmp_workspace, 4, "too old")
        conn = mm.init_db()
        try:
            assert mm.process_messages_to_episodes(conn) == (0, [])
        finally:
            conn.close()

    def test_first_run_seeds_older_days_to_avoid_double_insert(self, mm, tmp_workspace, monkeypatch):
        monkeypatch.setattr(mm, "score_importance", lambda s, stats=None: 7.0)
        _write_day(tmp_workspace, 2, "already handled by old code")
        _write_day(tmp_workspace, 1, "yesterday")
        conn = mm.init_db()
        try:
            count, _ = mm.process_messages_to_episodes(conn)
            assert count == 1
        finally:
            conn.close()


class TestDecayAndPrune:
    def _add(self, conn, importance, age_days, inserted_days_ago, base=None):
        created = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=age_days)).isoformat()
        inserted = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=inserted_days_ago)
                    ).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO episodes (summary, importance, base_importance, created_at, inserted_at) "
            "VALUES ('s', ?, ?, ?, ?)",
            (importance, base if base is not None else importance, created, inserted))
        conn.commit()
        return cur.lastrowid

    def test_decay_is_linear_from_base_and_idempotent(self, mm, conn):
        eid = self._add(conn, 9.0, age_days=8, inserted_days_ago=8)
        mm.decay_importance(conn)
        imp = conn.execute("SELECT importance FROM episodes WHERE id=?", (eid,)).fetchone()[0]
        assert imp == pytest.approx(9.0 - (8 / 4.0) * mm.DECAY_RATE, abs=0.01)
        mm.decay_importance(conn)
        again = conn.execute("SELECT importance FROM episodes WHERE id=?", (eid,)).fetchone()[0]
        assert again == pytest.approx(imp, abs=0.01)

    def test_decay_skips_rows_at_or_below_cutoff(self, mm, conn):
        eid = self._add(conn, mm.IMPORTANCE_CUTOFF, age_days=40, inserted_days_ago=40)
        assert mm.decay_importance(conn) == 0
        assert conn.execute("SELECT importance FROM episodes WHERE id=?", (eid,)).fetchone()[0] == mm.IMPORTANCE_CUTOFF

    def test_decay_floors_at_zero(self, mm, conn):
        eid = self._add(conn, 7.0, age_days=4000, inserted_days_ago=1)
        mm.decay_importance(conn)
        assert conn.execute("SELECT importance FROM episodes WHERE id=?", (eid,)).fetchone()[0] == 0.0

    def test_prune_deletes_old_low_importance(self, mm, conn):
        eid = self._add(conn, 2.0, age_days=30, inserted_days_ago=30)
        assert mm.prune_low_importance(conn, grace_days=7) == 1
        assert conn.execute("SELECT COUNT(*) FROM episodes WHERE id=?", (eid,)).fetchone()[0] == 0

    def test_prune_grace_period_protects_fresh_rows(self, mm, conn):
        eid = self._add(conn, 2.0, age_days=30, inserted_days_ago=1)
        assert mm.prune_low_importance(conn, grace_days=7) == 0
        assert conn.execute("SELECT COUNT(*) FROM episodes WHERE id=?", (eid,)).fetchone()[0] == 1

    def test_prune_keeps_rows_at_or_above_cutoff(self, mm, conn):
        self._add(conn, mm.IMPORTANCE_CUTOFF, age_days=30, inserted_days_ago=30)
        assert mm.prune_low_importance(conn, grace_days=7) == 0

    def test_prune_default_grace_from_module_constant(self, mm, conn, monkeypatch):
        monkeypatch.setattr(mm, "MEMORY_PRUNE_GRACE_DAYS", 100)
        self._add(conn, 2.0, age_days=30, inserted_days_ago=30)
        assert mm.prune_low_importance(conn) == 0

    def test_prune_skips_null_inserted_at(self, mm, conn):
        conn.execute("INSERT INTO episodes (summary, importance, inserted_at) VALUES ('s', 1.0, NULL)")
        conn.commit()
        assert mm.prune_low_importance(conn, grace_days=0) == 0
