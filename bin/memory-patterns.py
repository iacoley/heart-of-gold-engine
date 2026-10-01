#!/usr/bin/env python3
"""
Memory Patterns — Track 2 (weekly) of the curated memory layer.

See docs/design/curated-memory-layer.md. Track 1 (memory-maintenance.py's
nightly consolidation) promotes plain named-entity facts. This job
promotes the other kind of durable memory: behavioral patterns and rules
*about* an agent — the raw material for `patterns`, another table that's
been fully wired in the schema since day one and never populated.

Evidence-gating, ported from Amos's Mnemosyne design (mnemosyne-agent-spec.md,
relayed and partly firmed up live in #agent-chat 2026-08-31):
  - 1 normal-strength signal alone -> "pending" (not yet a candidate)
  - 2+ supporting episodes, OR one strong explicit signal -> "candidate"
  - 3+ reinforcements -> "established"
  - 30+ days unreinforced -> "deprecated" (regardless of stage reached)

Deliberately separate from Track 1: a wrong pattern is a wrong claim
about how an agent behaves (the exact thing voice.md has drifted on
before, see facts/voice-flattened-immediately-after-recalibration-2026-08-28.md
and friends) — higher blast radius than a stale glossary line, so it
gets its own job, its own weekly cadence (not folded into the nightly
run), and never touches voice.md/MEMORY.md directly. This job only
writes to the `patterns` table and a review file; promoting a pattern
into an actual persona edit is the reflection job (Track 2b, not yet
built — see design doc phasing, deliberately last).

Same citation discipline as Track 1: an episode id is never accepted
from the model's output. It's assigned by this script from a row it
already queried, so there's no path for a hallucinated citation.

Called by scheduler weekly (see bin/scheduler.py).
"""

import json
from claude_bin import claude_bin
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MEMORY_DIR = WORKSPACE / "data" / "memory"
MEMORY_DB = MEMORY_DIR / "memory.db"
HEALTH_FILE = WORKSPACE / "data" / "health" / "memory-patterns.json"
CANDIDATES_DIR = WORKSPACE / "data" / "memory-candidates"

LOOKBACK_DAYS = int(os.environ.get("PATTERNS_LOOKBACK_DAYS", "7"))
DEPRECATE_AFTER_DAYS = int(os.environ.get("PATTERNS_DEPRECATE_DAYS", "30"))
ESTABLISHED_AT = 3
# Cheap token-overlap similarity used to decide "is this candidate
# reinforcing an existing pattern row". Real semantic dedup (embeddings,
# the same way generate_embeddings() already does for episodes) is
# Track 2's dedup phase, not built yet — this is a deliberately crude
# stand-in, not a design decision to skip real dedup permanently.
SIMILARITY_THRESHOLD = float(os.environ.get("PATTERNS_SIMILARITY_THRESHOLD", "0.5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] memory-patterns: %(message)s",
)
log = logging.getLogger(__name__)

import sqlite3


def init_db() -> sqlite3.Connection:
    """Connect to the shared memory.db, ensuring the tables this job
    needs exist. Schema kept in sync with memory-maintenance.py's
    init_db() by hand (see that file's docstring for why these two
    scripts don't share a single schema function — bin/ scripts here
    do bare, non-package imports and hyphenated filenames don't import
    cleanly without importlib gymnastics; duplicating a ~10-line CREATE
    TABLE block is the same tradeoff friction-sensor.py already made by
    staying independent)."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 5.0,
            channel TEXT,
            tags TEXT,
            agents TEXT,
            created_at TIMESTAMP,
            consolidated_at TIMESTAMP DEFAULT NULL,
            embedding BLOB
        );

        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.7,
            reinforcement_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
    """)

    # Migration: status wasn't part of the original patterns schema
    # (the table existed, fully wired, and was never populated — see
    # docs/design/curated-memory-layer.md). Added here rather than in
    # the CREATE TABLE above because that only applies to a table that
    # doesn't exist yet, and this table already does in production.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(patterns)").fetchall()}
    if "status" not in cols:
        conn.execute("ALTER TABLE patterns ADD COLUMN status TEXT DEFAULT 'pending'")

    conn.commit()
    return conn


def load_recent_episodes(conn: sqlite3.Connection, days: int = LOOKBACK_DAYS) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT id, summary, channel, created_at FROM episodes WHERE created_at >= ? ORDER BY created_at",
        (cutoff,),
    ).fetchall()


def extract_candidate_pattern(episode_id: int, summary: str, agent: str = "Marvin") -> dict | None:
    """Ask Haiku whether this episode describes a recurring behavioral
    pattern, rule, correction, or standing instruction *about* an
    agent — not a plain fact about a person/place/thing (that's
    Track 1's job, not this one).

    strength="strong" is reserved for an explicit, direct correction or
    instruction stated outright by a human (Ian/Mike) — not an inferred
    behavior. That distinction is what lets one strong signal alone
    reach "candidate" status without waiting for a second occurrence.
    """
    prompt = f"""Read this conversation excerpt about an AI agent named {agent}.

Does it describe a behavioral pattern, rule, correction, or standing
instruction about how {agent} should act — NOT a plain fact about a
person, place, or thing (that's a different category, ignore those)?

If yes, respond with ONLY a single-line JSON object:
{{"pattern_type": "short category", "content": "one sentence stating the rule", "strength": "normal"}}

Use "strength": "strong" instead of "normal" only if a human states the
rule explicitly and directly (a correction or instruction given
outright), not if you're inferring a pattern from behavior alone.

If no, respond with ONLY: NONE

Excerpt: {summary}"""

    try:
        result = subprocess.run(
            [claude_bin(), "-p", prompt, "--model", "haiku", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = result.stdout.strip()
        if not raw or raw.upper() == "NONE":
            return None

        data = json.loads(raw)
        pattern_type = str(data.get("pattern_type", "")).strip()
        content = str(data.get("content", "")).strip()
        if not pattern_type or not content:
            return None

        strength = str(data.get("strength", "normal")).strip().lower()
        if strength not in ("normal", "strong"):
            strength = "normal"

        return {
            "agent": agent,
            "pattern_type": pattern_type,
            "content": content,
            "strength": strength,
            "episode_id": episode_id,
        }
    except Exception as e:
        log.warning(f"Failed to extract candidate pattern for episode {episode_id}: {e}")
        return None


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _similarity(a: str, b: str) -> float:
    """Jaccard token overlap. Crude on purpose — see module docstring
    on why real embedding-based dedup is a separate, later phase."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_matching_pattern(conn: sqlite3.Connection, candidate: dict):
    """Best-match an existing pattern row for the same agent+type,
    above SIMILARITY_THRESHOLD. Returns the row or None."""
    rows = conn.execute(
        "SELECT * FROM patterns WHERE agent = ? AND pattern_type = ? AND status != 'deprecated'",
        (candidate["agent"], candidate["pattern_type"]),
    ).fetchall()

    best, best_score = None, 0.0
    for row in rows:
        score = _similarity(row["content"], candidate["content"])
        if score > best_score:
            best, best_score = row, score

    if best is not None and best_score >= SIMILARITY_THRESHOLD:
        return best
    return None


def _status_for(reinforcement_count: int, ever_strong: bool) -> str:
    if reinforcement_count >= ESTABLISHED_AT:
        return "established"
    if reinforcement_count >= 2 or ever_strong:
        return "candidate"
    return "pending"


def upsert_pattern(conn: sqlite3.Connection, candidate: dict) -> dict:
    """Apply evidence-gating: reinforce a matching existing pattern, or
    insert a new one. Returns {"action": "reinforced"|"inserted", "id": ...}."""
    now = datetime.now(timezone.utc).isoformat()
    match = find_matching_pattern(conn, candidate)

    if match is not None:
        new_count = match["reinforcement_count"] + 1
        ever_strong = candidate["strength"] == "strong"  # this occurrence; prior strength not tracked per-row
        new_status = _status_for(new_count, ever_strong or match["status"] in ("candidate", "established"))
        content = f"{match['content']} [+episode {candidate['episode_id']}]"
        conn.execute(
            "UPDATE patterns SET content = ?, reinforcement_count = ?, status = ?, updated_at = ? WHERE id = ?",
            (content, new_count, new_status, now, match["id"]),
        )
        conn.commit()
        return {"action": "reinforced", "id": match["id"], "status": new_status, "reinforcement_count": new_count}

    status = _status_for(1, candidate["strength"] == "strong")
    content = f"{candidate['content']} [source: episode {candidate['episode_id']}]"
    cursor = conn.execute(
        """INSERT INTO patterns (agent, pattern_type, content, confidence, reinforcement_count, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (candidate["agent"], candidate["pattern_type"], content, 0.6, 1, status, now, now),
    )
    conn.commit()
    return {"action": "inserted", "id": cursor.lastrowid, "status": status, "reinforcement_count": 1}


def deprecate_stale_patterns(conn: sqlite3.Connection) -> int:
    """30+ days unreinforced -> deprecated, regardless of stage reached
    (Amos's rule, taken as stated — an established pattern that stops
    showing up in 30 days is exactly the kind of drift worth flagging,
    not exempting from the clock)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEPRECATE_AFTER_DAYS)).isoformat()
    cursor = conn.execute(
        "UPDATE patterns SET status = 'deprecated' WHERE status != 'deprecated' AND updated_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


def write_candidates_file(results: list) -> Path | None:
    if not results:
        return None
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CANDIDATES_DIR / f"patterns-{date_str}.md"

    lines = [
        f"# Pattern candidates — {date_str}",
        "",
        "Track 2 weekly pattern promotion (see docs/design/curated-memory-layer.md). "
        "Evidence-gated: pending (1 normal signal) -> candidate (2+ or 1 strong) -> "
        "established (3+). Nothing here edits voice.md/MEMORY.md directly — that's "
        "the separate, not-yet-built reflection job.",
        "",
    ]
    for r in results:
        lines.append(
            f"- **[{r['status']}]** pattern id {r['id']} ({r['action']}, "
            f"reinforcement_count={r['reinforcement_count']})"
        )
    lines.append("")
    out_path.write_text("\n".join(lines))
    return out_path


def write_health(success: bool, stats: dict) -> None:
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy" if success else "error",
        "stats": stats,
    }))


def run(conn: sqlite3.Connection, agent: str = "Marvin") -> dict:
    episodes = load_recent_episodes(conn)
    results = []

    for ep in episodes:
        candidate = extract_candidate_pattern(ep["id"], ep["summary"], agent=agent)
        if candidate is None:
            continue
        results.append(upsert_pattern(conn, candidate))

    deprecated = deprecate_stale_patterns(conn)
    candidates_file = write_candidates_file(results)

    return {
        "episodes_scanned": len(episodes),
        "patterns_touched": len(results),
        "reinforced": sum(1 for r in results if r["action"] == "reinforced"),
        "inserted": sum(1 for r in results if r["action"] == "inserted"),
        "established": sum(1 for r in results if r["status"] == "established"),
        "deprecated": deprecated,
        "candidates_file": str(candidates_file) if candidates_file else None,
    }


def main():
    log.info("Memory patterns job starting")
    start = time.time()
    try:
        conn = init_db()
        stats = run(conn)
        conn.close()
        stats["duration_s"] = round(time.time() - start, 2)
        log.info(f"Patterns job complete: {json.dumps(stats)}")
        write_health(True, stats)
    except Exception as e:
        log.error(f"Patterns job failed: {e}")
        write_health(False, {"error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
