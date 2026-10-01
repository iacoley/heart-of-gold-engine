"""
Tests for loading stored facts and memory index into agent prompt context.

Verifies:
1. load_stored_facts returns empty string when memory.db doesn't exist.
2. load_stored_facts queries memory.db and returns formatted facts.
3. load_stored_facts falls back to data/memory-candidates/ if DB has few facts.
4. load_memory_index reads agents/{agent}/memory/MEMORY.md.
"""

import sqlite3
import pytest
from pathlib import Path
from conftest import import_script


def test_load_stored_facts_empty(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    assert mod.load_stored_facts("Marvin") == ""


def test_load_stored_facts_from_db(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    db_dir = tmp_path / "data" / "memory"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "memory.db"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            domain TEXT DEFAULT 'general',
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO facts (subject, content, domain) VALUES (?, ?, ?)",
                 ("Banana Watcher", "Channel referee daemon running in #the-banana-stand", "coordination"))
    conn.execute("INSERT INTO facts (subject, content, domain) VALUES (?, ?, ?)",
                 ("Agora Chapter 11", "Roguelite bankruptcy trading game mechanics", "gaming"))
    conn.commit()
    conn.close()

    result = mod.load_stored_facts("Marvin")
    assert "# Learned Facts & Persistent Memory" in result
    assert "**Banana Watcher [coordination]:** Channel referee daemon running in #the-banana-stand" in result
    assert "**Agora Chapter 11 [gaming]:** Roguelite bankruptcy trading game mechanics" in result


def test_load_stored_facts_candidate_fallback(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    cand_dir = tmp_path / "data" / "memory-candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    (cand_dir / "2026-09-28.md").write_text(
        "# Memory candidates\n- **Consensus Clamping:** Use kind: consensus for terminal envelopes\n"
    )

    result = mod.load_stored_facts("Marvin")
    assert "# Learned Facts & Persistent Memory" in result
    assert "**Consensus Clamping:** Use kind: consensus for terminal envelopes" in result


def test_load_memory_index(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)

    # Empty when missing
    assert mod.load_memory_index("Marvin") == ""

    # Returns content when present
    mem_dir = tmp_path / "agents" / "Marvin" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("# Marvin Memory Index\n- [Topic](facts/topic.md)")

    assert mod.load_memory_index("Marvin") == "# Marvin Memory Index\n- [Topic](facts/topic.md)"


def _make_facts_db(tmp_path, rows, with_confidence=True):
    db_dir = tmp_path / "data" / "memory"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "memory.db")
    conf = "confidence REAL DEFAULT 0.8," if with_confidence else ""
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, "
        f"content TEXT NOT NULL, {conf} domain TEXT DEFAULT 'general')"
    )
    for subject, content, confidence in rows:
        if with_confidence:
            conn.execute("INSERT INTO facts (subject, content, confidence) VALUES (?, ?, ?)",
                         (subject, content, confidence))
        else:
            conn.execute("INSERT INTO facts (subject, content) VALUES (?, ?)", (subject, content))
    conn.commit()
    conn.close()


def test_facts_ranked_by_confidence_then_recency(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    _make_facts_db(tmp_path, [
        ("OldHigh", "a", 0.9),
        ("Low", "b", 0.2),
        ("NewHigh", "c", 0.9),
        ("Mid", "d", 0.5),
    ])
    lines = [l for l in mod.load_stored_facts("x").splitlines() if l.startswith("- **")]
    assert [l.split("**")[1].rstrip(":") for l in lines] == ["NewHigh", "OldHigh", "Mid", "Low"]


def test_facts_without_confidence_column_fall_back_to_recency(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    _make_facts_db(tmp_path, [("First", "a", None), ("Second", "b", None)], with_confidence=False)
    lines = [l for l in mod.load_stored_facts("x").splitlines() if l.startswith("- **")]
    assert "Second" in lines[0] and "First" in lines[1]


def test_facts_char_budget_and_omitted_line(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    _make_facts_db(tmp_path, [(f"S{i}", "x" * 100, 0.8) for i in range(20)])
    result = mod.load_stored_facts("x", char_budget=500)
    shown = [l for l in result.splitlines() if l.startswith("- **")]
    assert 0 < len(shown) < 20
    assert sum(len(l) + 1 for l in shown) <= 500
    assert f"({20 - len(shown)} older facts not shown; use the memory tool to search)" in result


def test_no_omitted_line_when_everything_fits(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    _make_facts_db(tmp_path, [("A", "a", 0.8), ("B", "b", 0.8)])
    assert "not shown" not in mod.load_stored_facts("x")


def test_limit_counts_toward_omitted(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    _make_facts_db(tmp_path, [(f"S{i}", "c", 0.8) for i in range(5)])
    assert "(3 older facts not shown" in mod.load_stored_facts("x", limit=2)


def test_candidate_dedup_on_normalized_subject(monkeypatch, tmp_path):
    mod = import_script("agent-server")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    _make_facts_db(tmp_path, [("Crab Cavern", "The second server", 0.8)])
    cand = tmp_path / "data" / "memory-candidates"
    cand.mkdir(parents=True)
    (cand / "2026-09-30.md").write_text(
        "- **crab  cavern** (general): The second server _(episode 1, facts.id 1)_\n"
        "- **Other Thing** (gaming): something else\n"
    )
    result = mod.load_stored_facts("x")
    assert result.lower().count("crab cavern") == 1
    assert "Other Thing" in result
