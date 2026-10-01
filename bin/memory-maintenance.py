#!/usr/bin/env python3
"""
Memory Maintenance — Episodic consolidation and embedding generation.

Processes recent messages from JSONL files:
1. Reads previous day's messages from JSONL
2. Scores importance (using Claude Haiku for cheap importance scoring)
3. Creates episodes in SQLite episodes table
4. Decays existing episode scores (configurable decay rate)
5. Applies cutoff to prune low-importance episodes

Called by scheduler daily at 3 AM.
"""

import json
from claude_bin import claude_bin
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MEMORY_DIR = WORKSPACE / "data" / "memory"
MEMORY_DB = MEMORY_DIR / "memory.db"
MESSAGES_DIR = WORKSPACE / "data" / "messages"
HEALTH_FILE = WORKSPACE / "data" / "health" / "memory-maintenance.json"

DECAY_RATE = float(os.environ.get("MEMORY_DECAY_RATE", "0.25"))
IMPORTANCE_CUTOFF = float(os.environ.get("MEMORY_CUTOFF", "6.0"))
MAX_EPISODES = int(os.environ.get("MEMORY_MAX_EPISODES", "15"))
# Episodes younger than this are never pruned, regardless of score — the
# scoring pass and the prune pass used to run back to back in the same
# main() invocation, so a freshly created episode scored in the 5-6 range
# (which is where the Haiku scoring prompt rates ordinary interactions) was
# deleted the same night it was written. Measured 2026-09-28: 96 episodes
# created, 93 pruned in the same run, net 3 kept.
MEMORY_PRUNE_GRACE_DAYS = float(os.environ.get("MEMORY_PRUNE_GRACE_DAYS", "7"))
# How many past UTC days (yesterday included) the nightly run will look back
# for message files that have not been processed yet, so a missed run does
# not permanently lose a day.
LOOKBACK_DAYS = int(os.environ.get("MEMORY_LOOKBACK_DAYS", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] memory-maintenance: %(message)s",
)
log = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
    """Initialize the memory database with required tables."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(MEMORY_DB))
    conn.row_factory = sqlite3.Row
    had_processed_days = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processed_days'"
    ).fetchone() is not None

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 5.0,
            base_importance REAL,
            channel TEXT,
            tags TEXT,
            agents TEXT,
            created_at TIMESTAMP,
            inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consolidated_at TIMESTAMP DEFAULT NULL,
            embedding BLOB
        );

        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.8,
            domain TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS processed_days (
            day TEXT PRIMARY KEY,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        CREATE INDEX IF NOT EXISTS idx_episodes_importance ON episodes(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
        CREATE INDEX IF NOT EXISTS idx_facts_domain ON facts(domain);
    """)

    _migrate_episode_columns(conn)

    if not had_processed_days:
        # First run with day tracking: earlier versions processed only
        # "yesterday" each night, so every day before yesterday inside the
        # lookback window was already handled. Seed those as processed or
        # the first lookback run would insert their episodes a second time.
        today = datetime.now(timezone.utc).date()
        for offset in range(2, LOOKBACK_DAYS + 1):
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            conn.execute("INSERT OR IGNORE INTO processed_days (day) VALUES (?)", (day,))

    conn.commit()
    return conn


def _migrate_episode_columns(conn: sqlite3.Connection) -> None:
    """Guarded migration: add `base_importance` and `inserted_at` to
    `episodes` on a database created before either existed, and backfill
    any row missing a value for either — unconditionally, every call, not
    only right after the `ALTER TABLE` — so a row written some other way
    (or one that predates a column added by an even older version of this
    migration) never sits with a NULL that would otherwise make it either
    un-prunable forever (`inserted_at`) or double-decay (`base_importance`
    falling back to the already-decayed `importance`).

    `base_importance` is the score `decay_importance()` subtracts age
    from — without it, decay was applied to the already-decayed
    `importance` column every night, so loss compounded quadratically
    instead of growing linearly with age. Backfilled from the current
    `importance` so existing rows don't jump on the next run; this does
    mean a pre-existing row's already-decayed value becomes its new
    `base_importance`, i.e. one extra round of decay gets baked in at
    migration time, and then ages normally from there.

    `inserted_at` records when the row was written to the DB, as opposed
    to `created_at` (the source message's own timestamp, still used for
    recall ranking). It is what the prune grace period measures against,
    so a backfilled default of "now" is deliberately conservative: it
    protects pre-existing rows for a full grace period rather than
    silently making them immediately prunable.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()}

    if "base_importance" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN base_importance REAL")

    if "inserted_at" not in cols:
        # No DEFAULT clause here: SQLite refuses ADD COLUMN with a
        # non-constant default (CURRENT_TIMESTAMP) on a table that already
        # has rows ("Cannot add a column with non-constant default"). The
        # fresh-DB CREATE TABLE above still carries the real column default;
        # this bare ALTER only runs against a pre-existing table.
        conn.execute("ALTER TABLE episodes ADD COLUMN inserted_at TIMESTAMP")

    conn.execute(
        "UPDATE episodes SET base_importance = importance WHERE base_importance IS NULL"
    )
    conn.execute(
        "UPDATE episodes SET inserted_at = CURRENT_TIMESTAMP WHERE inserted_at IS NULL"
    )


def read_day_messages(date_str: str) -> list | None:
    """Read one day's JSONL message file. Returns None if the file does not
    exist (distinct from an existing-but-empty file, which returns [])."""
    messages_file = MESSAGES_DIR / f"messages-{date_str}.jsonl"
    if not messages_file.exists():
        log.info(f"No messages file for {date_str}")
        return None

    messages = []
    with open(messages_file, 'r') as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
                messages.append(msg)
            except json.JSONDecodeError:
                continue

    log.info(f"Read {len(messages)} messages from {date_str}")
    return messages


def read_previous_day_messages() -> list:
    """Read messages from previous day's JSONL files."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    return read_day_messages(yesterday.strftime("%Y-%m-%d")) or []


def unprocessed_days(conn: sqlite3.Connection) -> list:
    """UTC date strings within the lookback window (yesterday back
    `LOOKBACK_DAYS` days, oldest first) that have a message file and are not
    yet recorded in `processed_days`. Today is excluded (still being written)."""
    today = datetime.now(timezone.utc).date()
    days = []
    for offset in range(LOOKBACK_DAYS, 0, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        if not (MESSAGES_DIR / f"messages-{day}.jsonl").exists():
            continue
        done = conn.execute(
            "SELECT 1 FROM processed_days WHERE day = ?", (day,)
        ).fetchone()
        if not done:
            days.append(day)
    return days


SCORE_TIMEOUT_FIRST = float(os.environ.get("MEMORY_SCORE_TIMEOUT", "20"))
SCORE_TIMEOUT_RETRY = float(os.environ.get("MEMORY_SCORE_RETRY_TIMEOUT", "60"))
# On a final scoring failure the episode is kept rather than defaulted to a
# below-cutoff score — 5.0 used to sit under the 6.0 cutoff, so a Haiku
# hiccup silently pruned the episode that same night regardless of its
# actual importance.
SCORE_FAILURE_IMPORTANCE = IMPORTANCE_CUTOFF + 1.0


def _score_importance_once(prompt: str, timeout: float) -> float | None:
    try:
        result = subprocess.run(
            [claude_bin(), "-p", prompt, "--model", "haiku", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        score_str = result.stdout.strip()
        score = float(score_str)
        return max(1.0, min(10.0, score))
    except Exception:
        return None


def score_importance(summary: str, stats: dict | None = None) -> float:
    """Score episode importance using Claude Haiku (cheap).

    Retries once with a longer timeout before giving up. A final failure
    returns a score at/above the cutoff (never a value that would make the
    episode immediately prunable) and, when `stats` is passed, increments
    `stats["score_failures"]` so a run of persistent scoring failures shows
    up in the health file instead of silently degrading recall quality.
    """
    prompt = f"""Score the importance of this conversation excerpt on a scale of 1-10.

Consider:
- 9-10: Major decisions, critical events, important personal information
- 7-8: Meaningful conversations, useful information, preferences
- 5-6: Normal interactions, routine tasks
- 3-4: Minor updates, simple acknowledgments
- 1-2: Trivial chatter, noise

Excerpt: {summary}

Respond with ONLY a number 1-10."""

    score = _score_importance_once(prompt, SCORE_TIMEOUT_FIRST)
    if score is not None:
        return score

    log.warning("Failed to score importance on first attempt, retrying with longer timeout")
    score = _score_importance_once(prompt, SCORE_TIMEOUT_RETRY)
    if score is not None:
        return score

    log.warning(
        f"Failed to score importance after retry, defaulting to "
        f"{SCORE_FAILURE_IMPORTANCE} (cutoff-safe, not discarded)"
    )
    if stats is not None:
        stats["score_failures"] = stats.get("score_failures", 0) + 1
    return SCORE_FAILURE_IMPORTANCE


def segment_messages_into_episodes(messages: list) -> list:
    """Segment messages into conversation episodes."""
    if not messages:
        return []

    episodes = []
    current_episode = []
    last_ts = None

    # Group messages with <5 minute gaps into episodes
    for msg in messages:
        try:
            ts = datetime.fromisoformat(msg["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue

        if last_ts and (ts - last_ts).total_seconds() > 300:  # 5 min gap
            if current_episode:
                episodes.append(current_episode)
                current_episode = []

        current_episode.append(msg)
        last_ts = ts

    if current_episode:
        episodes.append(current_episode)

    return episodes


def create_episode_summary(messages: list) -> str:
    """Create a 2-3 sentence summary of an episode."""
    # Simple implementation: just concatenate the messages
    texts = []
    for msg in messages[:10]:  # Limit to first 10 messages
        author = msg.get("author_name", "User")
        content = msg.get("content", "")
        if not content:
            continue
        # Bot messages are the agents' own replies and decisions; keep them
        # (labelled) so the episode records what the agent said, not only
        # what it was told.
        if msg.get("is_bot", False):
            texts.append(f"[agent] {author}: {content}")
        else:
            texts.append(f"{author}: {content}")

    return " | ".join(texts)[:500]  # Cap at 500 chars


def decay_importance(conn: sqlite3.Connection) -> int:
    """Apply time-based decay to episode importance scores.

    Decay formula: importance = base_importance - (days_since_creation * DECAY_RATE / 4)
    Default DECAY_RATE=0.25 means 0.25 points lost per 4 days.

    Computed from `base_importance` (the score at creation, never modified
    after `_migrate_episode_columns()` backfills or an insert sets it) rather
    than the current `importance` column. The old version subtracted the
    day's decay from whatever `importance` already was, so a run every night
    compounded the loss quadratically with age instead of linearly. Keying
    off `base_importance` every time makes this idempotent: running it twice
    in a row, or nightly for a year, converges on the same value for a given
    age rather than drifting further each time.
    """
    # Calculate decay for each episode based on age
    rows = conn.execute(
        "SELECT id, base_importance, importance, created_at FROM episodes WHERE importance > ?"
        , (IMPORTANCE_CUTOFF,)
    ).fetchall()

    decayed = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        try:
            base = row["base_importance"]
            if base is None:
                base = row["importance"]
            created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            days_old = (now - created_at).total_seconds() / 86400
            decay_amount = (days_old / 4.0) * DECAY_RATE
            new_importance = max(0.0, base - decay_amount)

            if new_importance != row["importance"]:
                conn.execute(
                    "UPDATE episodes SET importance = ? WHERE id = ?",
                    (new_importance, row["id"])
                )
                decayed += 1
        except Exception as e:
            log.warning(f"Failed to decay episode {row['id']}: {e}")
            continue

    conn.commit()
    log.info(f"Decayed importance on {decayed} episodes (rate={DECAY_RATE} per 4 days)")
    return decayed


def prune_low_importance(conn: sqlite3.Connection, grace_days: float | None = None) -> int:
    """Remove episodes below the importance cutoff that are also older than
    `MEMORY_PRUNE_GRACE_DAYS` (measured from `inserted_at`, not `created_at`).

    Without the grace period, a same-run episode scored 5-6 by
    `score_importance()` (the range the Haiku prompt gives ordinary
    interactions) was deleted the same night it was created — measured
    2026-09-28: 96 episodes created, 93 pruned in the same run, net 3 kept.
    """
    if grace_days is None:
        grace_days = MEMORY_PRUNE_GRACE_DAYS

    grace_cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)

    rows = conn.execute(
        "SELECT id, inserted_at FROM episodes WHERE importance < ?",
        (IMPORTANCE_CUTOFF,)
    ).fetchall()

    to_delete = []
    for row in rows:
        inserted_at = row["inserted_at"]
        if not inserted_at:
            # No inserted_at at all (shouldn't happen post-migration) — be
            # conservative and leave it rather than delete blind.
            continue
        inserted_dt = None
        for parser in (
            lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
            lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        ):
            try:
                inserted_dt = parser(inserted_at)
                break
            except (TypeError, ValueError):
                continue
        if inserted_dt is None:
            log.warning(f"Could not parse inserted_at for episode {row['id']}: {inserted_at!r}")
            continue
        if inserted_dt.tzinfo is None:
            inserted_dt = inserted_dt.replace(tzinfo=timezone.utc)
        if inserted_dt <= grace_cutoff:
            to_delete.append(row["id"])

    pruned = 0
    if to_delete:
        conn.executemany(
            "DELETE FROM episodes WHERE id = ?", [(i,) for i in to_delete]
        )
        pruned = len(to_delete)

    conn.commit()
    if pruned:
        log.info(f"Pruned {pruned} low-importance episodes older than {grace_days}d")
    return pruned


def consolidate_episodes(conn: sqlite3.Connection) -> int:
    """Mark short recent episodes for consolidation."""
    # Find episodes with short summaries that haven't been consolidated
    rows = conn.execute(
        "SELECT id, summary FROM episodes "
        "WHERE consolidated_at IS NULL AND LENGTH(summary) < 200 "
        "ORDER BY created_at DESC LIMIT ?",
        (MAX_EPISODES,)
    ).fetchall()

    if len(rows) < 3:
        return 0

    # Group short episodes and mark them consolidated
    consolidated = 0
    for row in rows:
        conn.execute(
            "UPDATE episodes SET consolidated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row["id"],)
        )
        consolidated += 1

    conn.commit()
    log.info(f"Marked {consolidated} episodes as consolidated")
    return consolidated


def generate_embeddings(conn: sqlite3.Connection) -> int:
    """Generate embeddings for episodes that don't have them yet."""
    try:
        from fastembed import TextEmbedding
    except ImportError:
        log.warning("fastembed not installed — skipping embedding generation")
        return 0

    rows = conn.execute(
        "SELECT id, summary FROM episodes WHERE embedding IS NULL LIMIT 50"
    ).fetchall()

    if not rows:
        return 0

    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    texts = [row["summary"] for row in rows]
    embeddings = list(model.embed(texts))

    import numpy as np
    for row, emb in zip(rows, embeddings):
        emb_bytes = np.array(emb, dtype=np.float32).tobytes()
        conn.execute(
            "UPDATE episodes SET embedding = ? WHERE id = ?",
            (emb_bytes, row["id"])
        )

    conn.commit()
    log.info(f"Generated embeddings for {len(rows)} episodes")
    return len(rows)


def write_health(success: bool, stats: dict) -> None:
    """Write health heartbeat."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy" if success else "error",
        "stats": stats,
    }))


def process_messages_to_episodes(conn: sqlite3.Connection, stats: dict | None = None) -> tuple[int, list]:
    """Process unprocessed message days (last LOOKBACK_DAYS) into episodes.

    Returns (created_count, new_episodes) where new_episodes is a list of
    {"id", "summary", "channel"} dicts for episodes created *this run* —
    needed by extract_candidate_facts() below so fact extraction only
    looks at what's actually new, and so every extracted fact can cite a
    real, just-inserted episode id rather than a model-guessed one.

    `stats`, when passed, is forwarded to `score_importance()` so a scoring
    failure that falls all the way back to a default gets counted in the
    run's health stats (`score_failures`).
    """
    created = 0
    new_episodes = []

    for day in unprocessed_days(conn):
        messages = read_day_messages(day) or []
        for episode_msgs in segment_messages_into_episodes(messages):
            if not episode_msgs:
                continue

            summary = create_episode_summary(episode_msgs)
            if not summary:
                continue
            importance = score_importance(summary, stats)

            channel = episode_msgs[0].get("channel_name", "unknown")
            created_at = episode_msgs[0].get("ts", datetime.now(timezone.utc).isoformat())

            cursor = conn.execute(
                """INSERT INTO episodes
                   (summary, importance, base_importance, channel, created_at, inserted_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (summary, importance, importance, channel, created_at)
            )
            new_episodes.append({"id": cursor.lastrowid, "summary": summary, "channel": channel})
            created += 1

        # Episodes and the processed marker commit together, so a crash
        # mid-day re-processes that day instead of double-inserting.
        conn.execute("INSERT OR IGNORE INTO processed_days (day) VALUES (?)", (day,))
        conn.commit()

    conn.commit()
    log.info(f"Created {created} episodes from messages")
    return created, new_episodes


def extract_candidate_fact(episode_id: int, summary: str) -> dict | None:
    """Ask Haiku whether this episode contains a durable, plain
    named-entity/glossary fact — a definition ("X is the name of Y"),
    not a preference, correction, or behavioral rule about an agent.

    Track 1 only, per docs/design/curated-memory-layer.md: behavioral
    pattern promotion (Track 2) is explicitly out of scope for this
    job and must not be extracted here even if the model is tempted to.

    The episode_id is never taken from the model's output — it's the
    real id of the episode being examined, passed in by the caller and
    stamped onto the result. That's the citation-integrity guarantee:
    there's no path for a hallucinated citation because the model is
    never asked to produce one.
    """
    prompt = f"""Read this conversation excerpt. Does it contain a durable,
plain named-entity or glossary-style fact — a definition of a person,
place, channel, system, or thing ("X is the name of Y")?

Do NOT extract: preferences, corrections, behavioral rules, opinions,
in-progress work, or anything that's really about how someone should
act rather than what something IS.

If yes, respond with ONLY a single-line JSON object:
{{"subject": "short name", "content": "one-sentence definition", "domain": "one word category"}}

If no, respond with ONLY: NONE

Excerpt: {summary}"""

    try:
        result = subprocess.run(
            [claude_bin(), "-p", prompt, "--model", "haiku", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=20
        )
        raw = result.stdout.strip()
        if not raw or raw.upper() == "NONE":
            return None

        data = json.loads(raw)
        subject = str(data.get("subject", "")).strip()
        content = str(data.get("content", "")).strip()
        if not subject or not content:
            return None

        return {
            "subject": subject,
            "content": content,
            "domain": str(data.get("domain", "general")).strip() or "general",
            "episode_id": episode_id,
        }
    except Exception as e:
        log.warning(f"Failed to extract candidate fact for episode {episode_id}: {e}")
        return None


def citation_is_valid(conn: sqlite3.Connection, episode_id: int) -> bool:
    """Deterministic check: does this episode id actually exist?

    Cheap on purpose — the point isn't sophistication, it's that this
    check runs against the real table instead of trusting the model's
    own claim that it checked. See the citation-integrity discussion in
    docs/design/curated-memory-layer.md.
    """
    row = conn.execute("SELECT id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return row is not None


def insert_candidate_fact(conn: sqlite3.Connection, candidate: dict) -> int:
    """Insert a validated candidate into the (previously always-empty)
    facts table, citation baked into the content so the audit trail
    survives even if someone only ever reads the facts table directly."""
    now = datetime.now(timezone.utc).isoformat()
    content = f"{candidate['content']} [source: episode {candidate['episode_id']}]"
    cursor = conn.execute(
        """INSERT INTO facts (subject, content, confidence, domain, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (candidate["subject"], content, 0.7, candidate["domain"], now, now)
    )
    conn.commit()
    return cursor.lastrowid


def write_candidates_file(candidates: list) -> Path | None:
    """Human-readable audit trail alongside the DB rows — the file is
    what a human actually reviews; the DB row is what a future
    retrieval layer would query. Neither replaces the other."""
    if not candidates:
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = WORKSPACE / "data" / "memory-candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.md"

    lines = [
        f"# Memory candidates — {date_str}",
        "",
        "Auto-promoted plain facts from nightly consolidation (Track 1 — "
        "see docs/design/curated-memory-layer.md). Named-entity/glossary "
        "facts only; behavioral patterns and persona edits are Track 2 "
        "and not built by this job.",
        "",
    ]
    for c in candidates:
        lines.append(
            f"- **{c['subject']}** ({c['domain']}): {c['content']} "
            f"_(episode {c['episode_id']}, facts.id {c['fact_id']})_"
        )
    lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


def extract_facts_from_episodes(conn: sqlite3.Connection, new_episodes: list) -> dict:
    """Track 1 driver: for each newly-created episode, try to extract a
    plain fact, validate its citation deterministically, and if it
    passes both, insert it and record it for the audit file."""
    accepted = []
    rejected_citation = 0

    for ep in new_episodes:
        candidate = extract_candidate_fact(ep["id"], ep["summary"])
        if candidate is None:
            continue
        if not citation_is_valid(conn, candidate["episode_id"]):
            # Shouldn't happen — episode_id is assigned by us, not the
            # model — but the check exists precisely so "shouldn't
            # happen" doesn't quietly become "didn't happen, allegedly".
            rejected_citation += 1
            log.warning(f"Rejected candidate fact citing nonexistent episode {candidate['episode_id']}")
            continue
        candidate["fact_id"] = insert_candidate_fact(conn, candidate)
        accepted.append(candidate)

    candidates_file = write_candidates_file(accepted)
    if candidates_file:
        log.info(f"Wrote {len(accepted)} candidate facts to {candidates_file}")

    return {
        "facts_extracted": len(accepted),
        "facts_rejected_citation": rejected_citation,
        "candidates_file": str(candidates_file) if candidates_file else None,
    }


def main():
    log.info("Memory maintenance starting")
    start = time.time()

    try:
        conn = init_db()

        stats = {"score_failures": 0}
        episodes_created, new_episodes = process_messages_to_episodes(conn, stats)

        stats["episodes_created"] = episodes_created
        stats["decayed"] = decay_importance(conn)
        stats["pruned"] = prune_low_importance(conn)
        stats["consolidated"] = consolidate_episodes(conn)
        stats["embedded"] = generate_embeddings(conn)
        stats.update(extract_facts_from_episodes(conn, new_episodes))

        newest = conn.execute(
            "SELECT MAX(inserted_at) AS newest FROM episodes"
        ).fetchone()
        stats["newest_inserted_at"] = newest["newest"] if newest else None

        conn.close()
        duration = round(time.time() - start, 2)
        stats["duration_s"] = duration

        log.info(f"Maintenance complete in {duration}s: {json.dumps(stats)}")
        write_health(True, stats)

    except Exception as e:
        log.error(f"Maintenance failed: {e}")
        write_health(False, {"error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
