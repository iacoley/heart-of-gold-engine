"""Tests for bin/memory_retrieval.py and its wiring (issue #32)."""

import sqlite3
import sys
import struct
import time

import pytest

from conftest import import_script, PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT / "bin"))
import memory_retrieval as mr  # noqa: E402


def _mkdb(path, facts=(), episodes=(), with_embedding=False):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
        content TEXT NOT NULL, confidence REAL DEFAULT 0.8, domain TEXT DEFAULT 'general',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP, embedding BLOB)""")
    conn.execute("""CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL,
        importance REAL DEFAULT 5.0, created_at TIMESTAMP, tags TEXT, embedding BLOB)""")
    for f in facts:
        conn.execute("INSERT INTO facts (subject, content, domain, embedding) VALUES (?,?,?,?)",
                     (f[0], f[1], f[2] if len(f) > 2 else "general", f[3] if len(f) > 3 else None))
    for e in episodes:
        conn.execute("INSERT INTO episodes (summary, created_at, embedding) VALUES (?,?,?)",
                     (e[0], e[1] if len(e) > 1 else "2026-09-01 10:00:00", e[2] if len(e) > 2 else None))
    conn.commit()
    conn.close()
    return path


def vec(*xs):
    return struct.pack(f"{len(xs)}f", *xs)


@pytest.fixture
def db(tmp_path):
    return _mkdb(tmp_path / "memory.db",
        facts=[("Banana Watcher", "Channel referee daemon in the banana stand", "coordination"),
               ("Deploy process", "Dashboard deploys go through the Pi systemd service"),
               ("Coffee", "Ian drinks oat milk lattes")],
        episodes=[("Discussed the referee daemon timeouts and the ceiling", "2026-09-02 09:00:00"),
                  ("Planted tomatoes in the garden", "2026-09-03 09:00:00")])


def test_bm25_ranks_relevant_first(db):
    conn = sqlite3.connect(db)
    hits = mr.search(conn, "how does the referee daemon work?", embed_fn=None)
    assert hits[0].kind in ("fact", "episode")
    assert {h.text for h in hits[:2]} >= {"Channel referee daemon in the banana stand"}
    assert all("tomatoes" not in h.text for h in hits)
    facts = mr.search(conn, "deploy dashboard", kinds=("fact",), embed_fn=None)
    assert [h.subject for h in facts] == ["Deploy process"]


def test_stopword_only_and_empty_query(db):
    conn = sqlite3.connect(db)
    assert mr.search(conn, "the and of", embed_fn=None) == []
    assert mr.search(conn, "", embed_fn=None) == []


def test_fts_operator_chars_are_safe(db):
    conn = sqlite3.connect(db)
    mr.search(conn, 'referee" OR (NEAR AND * ) -- ;', embed_fn=None)


def test_index_rebuilds_when_stale(db):
    conn = sqlite3.connect(db)
    assert mr.search(conn, "zeppelin", embed_fn=None) == []
    conn.execute("INSERT INTO facts (subject, content) VALUES ('Air', 'zeppelin hangar notes')")
    conn.commit()
    assert [h.subject for h in mr.search(conn, "zeppelin", embed_fn=None)] == ["Air"]
    conn.execute("UPDATE facts SET content='dirigible notes', updated_at='2026-10-01' WHERE subject='Air'")
    conn.commit()
    assert mr.search(conn, "zeppelin", embed_fn=None) == []
    conn.execute("DELETE FROM facts WHERE subject='Air'")
    conn.commit()
    assert mr.search(conn, "dirigible", embed_fn=None) == []


def test_rrf_fusion_math():
    fused = mr.rrf_fuse([[("a", 1), ("b", 2)], [("b", 2), ("c", 3)]])
    keys = [k for k, _ in fused]
    assert keys[0] == ("b", 2)  # present in both lists
    assert set(keys) == {("a", 1), ("b", 2), ("c", 3)}
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_embedding_fusion_pulls_in_semantic_match(tmp_path):
    # Fact 2 shares no keyword with the query but its embedding is closest.
    db = _mkdb(tmp_path / "m.db", facts=[
        ("Lexical", "automobile maintenance schedule", "general", vec(0, 1, 0)),
        ("Semantic", "car servicing intervals", "general", vec(1, 0, 0)),
        ("Other", "unrelated pasta recipe", "general", vec(0, 0, 1)),
    ])
    conn = sqlite3.connect(db)
    bm25_only = mr.search(conn, "automobile", kinds=("fact",), embed_fn=None)
    assert [h.subject for h in bm25_only] == ["Lexical"]
    fused = mr.search(conn, "automobile", kinds=("fact",), embed_fn=lambda q: [1.0, 0.0, 0.0])
    assert {h.subject for h in fused[:2]} == {"Lexical", "Semantic"}
    assert "Other" not in [h.subject for h in fused[:2]]


def test_embedding_failure_degrades_to_bm25(db):
    conn = sqlite3.connect(db)

    def boom(q):
        raise RuntimeError("no model")
    hits = mr.search(conn, "referee", embed_fn=boom)
    assert hits and "referee" in hits[0].text


def test_dimension_mismatch_embeddings_ignored(tmp_path):
    db = _mkdb(tmp_path / "m.db", facts=[("A", "alpha beta", "general", vec(1, 0))])
    conn = sqlite3.connect(db)
    hits = mr.search(conn, "alpha", kinds=("fact",), embed_fn=lambda q: [1.0, 0.0, 0.0])
    assert [h.subject for h in hits] == ["A"]


def test_readonly_db_uses_memory_index(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    hits = mr.search(conn, "referee", embed_fn=None)
    assert hits


def test_embed_query_none_when_disabled(monkeypatch):
    monkeypatch.setenv("KARAKOS_RETRIEVAL_EMBEDDINGS", "0")
    assert mr.embed_query("hello") is None


def test_format_block_cap_and_shape():
    hits = [mr.Hit("fact", i, f"Subj{i}", "x" * 400, 1.0, domain="ops") for i in range(10)]
    hits.append(mr.Hit("episode", 1, "", "ep text", 1.0, created_at="2026-09-02 09:00:00"))
    block = mr.format_block(hits, char_cap=1500)
    assert block.startswith("[relevant memory]\n- fact: **Subj0 [ops]:** ")
    assert len(block) <= 1500
    assert mr.format_block([], 1500) == ""
    ep = mr.format_block([hits[-1]])
    assert "- episode 2026-09-02: ep text" in ep


def test_relevant_block_dedups_spawn_facts(db):
    block = mr.relevant_memory_block(db, "referee daemon", exclude_subjects={"banana watcher"}, embed_fn=None)
    assert "Banana Watcher" not in block
    assert "referee daemon timeouts" in block
    block2 = mr.relevant_memory_block(db, "referee daemon", embed_fn=None)
    assert "Banana Watcher" in block2


def test_missing_db_returns_empty(tmp_path):
    assert mr.relevant_memory_block(tmp_path / "nope.db", "anything here") == ""


def test_latency_thousands_of_rows(tmp_path):
    db = _mkdb(tmp_path / "big.db",
               facts=[(f"Subject {i}", f"fact number {i} about topic{i % 50} widget{i % 7} gadget", "general") for i in range(3000)],
               episodes=[(f"episode {i} discussed topic{i % 50} and project{i % 11}",) for i in range(2000)])
    conn = sqlite3.connect(db)
    mr.search(conn, "topic7 widget3", embed_fn=None)  # builds the index
    t = time.perf_counter()
    for _ in range(5):
        mr.search(conn, "what about topic7 and widget3 project4?", embed_fn=None)
    per = (time.perf_counter() - t) / 5
    assert per < 0.25  # generous CI bound; ~ms in practice


# ---------------------------- agent-server wiring ----------------------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    _mkdb(tmp_path / "data" / "memory" / "memory.db",
          facts=[("Banana Watcher", "Channel referee daemon in the banana stand", "coordination"),
                 ("Deploy process", "Dashboard deploys via systemd")],
          episodes=[("Referee daemon ceiling discussion", "2026-09-02 09:00:00")])
    mod.SPAWN_FACT_SUBJECTS.clear()
    return mod


def test_per_turn_block_format_and_dedup(server):
    server.load_stored_facts("Marvin")  # records spawn subjects (both facts)
    assert "banana watcher" in server.SPAWN_FACT_SUBJECTS["Marvin"]
    block = server.build_per_turn_memory_block("Marvin", "tell me about the referee daemon")
    assert block.startswith("[relevant memory]")
    assert "Banana Watcher" not in block          # already in spawn block
    assert "Referee daemon ceiling" in block      # episode is not deduped
    other = server.build_per_turn_memory_block("Other", "tell me about the referee daemon")
    assert "Banana Watcher" in other
    assert len(other) <= server.PER_TURN_MEMORY_CHAR_CAP


def test_per_turn_failure_does_not_raise(server, monkeypatch):
    mr_mod = mr

    def boom(*a, **k):
        raise sqlite3.DatabaseError("corrupt")
    monkeypatch.setattr(mr_mod, "relevant_memory_block", boom)
    assert server.build_per_turn_memory_block("Marvin", "referee") == ""


def test_per_turn_corrupt_db_returns_empty(server, tmp_path):
    (tmp_path / "data" / "memory" / "memory.db").write_bytes(b"not a database" * 100)
    assert server.build_per_turn_memory_block("Marvin", "referee daemon") == ""


def test_per_turn_wired_into_process_queue_with_flag():
    src = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    assert 'config.get("per_turn_memory", True)' in src
    assert "build_per_turn_memory_block" in src


# ------------------------------- memory tool ---------------------------------

def test_tool_search_uses_module(monkeypatch, tmp_path, db):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    tools = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    mem = tmp_path / "data" / "memory"
    mem.mkdir(parents=True)
    (mem / "memory.db").write_bytes(db.read_bytes())

    calls = []
    real = mr.search

    def spy(*a, **k):
        calls.append(k.get("kinds"))
        return real(*a, **k)
    monkeypatch.setattr(tools._retrieval(), "search", spy)

    # LIKE '%referee%' would miss word-order / stemming variants; BM25+porter finds them
    r = tools.handle_core_tool("memory", {"action": "facts", "query": "referees daemon"})
    assert r["facts"] and r["facts"][0]["subject"] == "Banana Watcher"
    assert set(r["facts"][0]) == {"id", "subject", "content", "confidence", "domain"}
    r = tools.handle_core_tool("memory", {"action": "recall", "query": "referee ceiling"})
    assert r["episodes"] and set(r["episodes"][0]) == {"id", "summary", "importance", "created_at"}
    assert calls == [("fact",), ("episode",)]


def test_tool_falls_back_to_like_on_retrieval_error(monkeypatch, tmp_path, db):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    tools = import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")
    mem = tmp_path / "data" / "memory"
    mem.mkdir(parents=True)
    (mem / "memory.db").write_bytes(db.read_bytes())

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(tools._retrieval(), "search", boom)
    r = tools.handle_core_tool("memory", {"action": "facts", "query": "referee"})
    assert r["facts"][0]["subject"] == "Banana Watcher"
