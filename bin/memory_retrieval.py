"""Hybrid memory retrieval: SQLite FTS5 (BM25) + optional embedding cosine, fused with RRF.

Used by agent-server.py for per-turn injection of relevant facts/episodes and by
the `memory` MCP tool's `facts` / `recall` search.

Design
------
* Index: an FTS5 table `mem_fts(kind, ref_id, subject, body)` living in the same
  memory.db, covering `facts` (kind "fact") and `episodes` (kind "episode").
  Staleness is detected by a cheap signature (row count, max id, total text
  length, max updated_at per table) stored in `mem_fts_meta`; on mismatch the
  index is rebuilt inside one IMMEDIATE transaction. Rebuild is a single
  INSERT..SELECT, a few ms for thousands of rows. No triggers are used because
  facts/episodes are written by several processes (tools-server, maintenance,
  dedup) that each create their own tables; rebuild-on-open keeps all of them
  correct without touching their DDL. If the DB is read-only or locked, the
  index is built in an in-memory connection instead.
* Ranking: BM25 (subject weighted above body). If the query can be embedded
  (fastembed importable) and rows carry stored embeddings, a cosine ranking is
  computed over those rows and fused with BM25 by Reciprocal Rank Fusion
  (k=60). Otherwise BM25 only. Embedding is never required and never raises.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

log = logging.getLogger("memory_retrieval")

RRF_K = 60
CANDIDATES = 50
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

_STOPWORDS = frozenset(
    "a an and are as at be but by can did do does for from had has have how i if in "
    "into is it its me my of on or our so than that the their them then there these "
    "they this to up was we were what when where which who why will with you your".split()
)


@dataclass
class Hit:
    kind: str  # "fact" | "episode"
    id: int
    subject: str
    text: str
    score: float
    domain: Optional[str] = None
    confidence: Optional[float] = None
    importance: Optional[float] = None
    created_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Index maintenance
# --------------------------------------------------------------------------- #

def _table_cols(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _source_signature(conn: sqlite3.Connection) -> str:
    parts = []
    if _has_table(conn, "facts"):
        upd = "COALESCE(MAX(updated_at),'')" if "updated_at" in _table_cols(conn, "facts") else "''"
        r = conn.execute(
            f"SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(LENGTH(subject)+LENGTH(content)),0), {upd} FROM facts"
        ).fetchone()
        parts.append("f:" + "|".join(str(x) for x in r))
    if _has_table(conn, "episodes"):
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(LENGTH(summary)),0) FROM episodes"
        ).fetchone()
        parts.append("e:" + "|".join(str(x) for x in r))
    return ";".join(parts)


def _build_index(conn: sqlite3.Connection, signature: str) -> None:
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
        "kind UNINDEXED, ref_id UNINDEXED, subject, body, tokenize='porter unicode61')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS mem_fts_meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("DELETE FROM mem_fts")
    if _has_table(conn, "facts"):
        dom = "COALESCE(domain,'')" if "domain" in _table_cols(conn, "facts") else "''"
        conn.execute(
            f"INSERT INTO mem_fts(kind, ref_id, subject, body) "
            f"SELECT 'fact', id, subject, content || ' ' || {dom} FROM facts"
        )
    if _has_table(conn, "episodes"):
        tags = "COALESCE(tags,'')" if "tags" in _table_cols(conn, "episodes") else "''"
        conn.execute(
            f"INSERT INTO mem_fts(kind, ref_id, subject, body) "
            f"SELECT 'episode', id, '', summary || ' ' || {tags} FROM episodes"
        )
    conn.execute("INSERT OR REPLACE INTO mem_fts_meta(k, v) VALUES ('sig', ?)", (signature,))


def ensure_index(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Return a connection whose `mem_fts` is current for the source tables.

    Normally `conn` itself. If the index cannot be written (read-only DB, lock),
    returns a throwaway in-memory connection holding a fresh index.
    """
    sig = _source_signature(conn)
    try:
        have = None
        if _has_table(conn, "mem_fts") and _has_table(conn, "mem_fts_meta"):
            row = conn.execute("SELECT v FROM mem_fts_meta WHERE k='sig'").fetchone()
            have = row[0] if row else None
        if have == sig:
            return conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            _build_index(conn, sig)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return conn
    except sqlite3.Error as e:
        log.debug("persistent FTS index unavailable (%s); using in-memory index", e)
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        # copy the source rows we need into the scratch DB
        for table in ("facts", "episodes"):
            if _has_table(conn, table):
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                mem.execute(ddl)
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    n = len(rows[0])
                    mem.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * n)})", [tuple(r) for r in rows])
        _build_index(mem, sig)
        return mem


# --------------------------------------------------------------------------- #
# Query handling
# --------------------------------------------------------------------------- #

def query_tokens(text: str) -> List[str]:
    toks = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
    seen, out = set(), []
    for t in toks:
        if len(t) < 2 or t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:40]


def _fts_query(tokens: Sequence[str]) -> str:
    # quoted tokens are literal (no FTS operator injection); prefix match for longer ones
    return " OR ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in tokens)


_embedder = None
_embedder_failed = False


def embed_query(text: str) -> Optional[List[float]]:
    """Embed `text` with fastembed if available; None when unavailable/disabled."""
    global _embedder, _embedder_failed
    if _embedder_failed or os.environ.get("KARAKOS_RETRIEVAL_EMBEDDINGS", "1") == "0":
        return None
    try:
        if _embedder is None:
            from fastembed import TextEmbedding  # optional dependency
            _embedder = TextEmbedding(model_name=EMBED_MODEL)
        vec = next(iter(_embedder.embed([text])))
        return [float(x) for x in vec]
    except Exception as e:  # ImportError, model download failure, ...
        _embedder_failed = True
        log.info("query embeddings unavailable, BM25 only: %s", e)
        return None


def _cosine_rank(conn, table: str, kind: str, qvec: Sequence[float], limit: int) -> List[tuple]:
    """[(kind, id)] best-first by cosine over rows with a stored embedding."""
    if not _has_table(conn, table) or "embedding" not in _table_cols(conn, table):
        return []
    rows = conn.execute(f"SELECT id, embedding FROM {table} WHERE embedding IS NOT NULL").fetchall()
    if not rows:
        return []
    dim = len(qvec)
    ids, blobs = [], []
    for r in rows:
        if r[1] is not None and len(r[1]) == dim * 4:
            ids.append(r[0])
            blobs.append(r[1])
    if not ids:
        return []
    try:
        import numpy as np
        mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(ids), dim)
        q = np.asarray(qvec, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0)
        sims = (mat @ q) / np.where(norms == 0, 1.0, norms)
        order = np.argsort(-sims)[:limit]
        return [(kind, ids[int(i)]) for i in order]
    except ImportError:
        from array import array
        qn = sum(x * x for x in qvec) ** 0.5 or 1.0
        scored = []
        for i, b in zip(ids, blobs):
            a = array("f")
            a.frombytes(b)
            n = sum(x * x for x in a) ** 0.5 or 1.0
            scored.append((sum(x * y for x, y in zip(a, qvec)) / (n * qn), i))
        scored.sort(reverse=True)
        return [(kind, i) for _, i in scored[:limit]]


def rrf_fuse(rankings: Iterable[Sequence[tuple]], k: int = RRF_K) -> List[tuple]:
    """Reciprocal Rank Fusion over best-first lists of hashable keys."""
    scores: dict = {}
    for ranking in rankings:
        for pos, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def search(
    conn: sqlite3.Connection,
    query: str,
    kinds: Sequence[str] = ("fact", "episode"),
    k: int = 5,
    embed_fn: Optional[Callable[[str], Optional[Sequence[float]]]] = embed_query,
) -> List[Hit]:
    """Top-`k` hits for `query`. Raises sqlite3.Error on DB trouble; callers
    that must not fail (per-turn injection) wrap this."""
    tokens = query_tokens(query)
    if not tokens:
        return []
    conn.row_factory = sqlite3.Row
    idx = ensure_index(conn)
    idx.row_factory = sqlite3.Row
    kinds = tuple(kinds)
    marks = ",".join("?" * len(kinds))
    rows = idx.execute(
        f"SELECT kind, ref_id FROM mem_fts WHERE mem_fts MATCH ? AND kind IN ({marks}) "
        f"ORDER BY bm25(mem_fts, 0.0, 0.0, 3.0, 1.0) LIMIT ?",
        (_fts_query(tokens), *kinds, CANDIDATES),
    ).fetchall()
    bm25_rank = [(r["kind"], int(r["ref_id"])) for r in rows]

    rankings = [bm25_rank]
    qvec = None
    if embed_fn is not None:
        try:
            qvec = embed_fn(query)
        except Exception as e:
            log.info("embed_fn failed, BM25 only: %s", e)
    if qvec:
        for kind, table in (("fact", "facts"), ("episode", "episodes")):
            if kind in kinds:
                try:
                    cr = _cosine_rank(conn, table, kind, qvec, CANDIDATES)
                except Exception as e:
                    log.info("cosine ranking failed for %s: %s", table, e)
                    cr = []
                if cr:
                    rankings.append(cr)
    if len(rankings) == 1:
        ordered = [(key, 1.0 / (RRF_K + i + 1)) for i, key in enumerate(bm25_rank)]
    else:
        ordered = rrf_fuse(rankings)
    ordered = ordered[:k]
    return _hydrate(conn, ordered)


def _hydrate(conn: sqlite3.Connection, ordered: Sequence[tuple]) -> List[Hit]:
    hits: List[Hit] = []
    fcols = _table_cols(conn, "facts") if _has_table(conn, "facts") else set()
    for (kind, ref_id), score in ordered:
        if kind == "fact":
            sel = "subject, content, " + ("domain, " if "domain" in fcols else "NULL AS domain, ") \
                + ("confidence, " if "confidence" in fcols else "NULL AS confidence, ") \
                + ("created_at" if "created_at" in fcols else "NULL AS created_at")
            r = conn.execute(f"SELECT {sel} FROM facts WHERE id=?", (ref_id,)).fetchone()
            if r:
                hits.append(Hit("fact", ref_id, r["subject"], r["content"], score,
                                domain=r["domain"], confidence=r["confidence"], created_at=r["created_at"]))
        else:
            r = conn.execute(
                "SELECT summary, importance, created_at FROM episodes WHERE id=?", (ref_id,)
            ).fetchone()
            if r:
                hits.append(Hit("episode", ref_id, "", r["summary"], score,
                                importance=r["importance"], created_at=r["created_at"]))
    return hits


# --------------------------------------------------------------------------- #
# Per-turn injection block
# --------------------------------------------------------------------------- #

def subject_key(subject: str) -> str:
    """Normalized subject; must match agent-server's _fact_subject_key."""
    return " ".join((subject or "").lower().split())


def format_block(hits: Sequence[Hit], char_cap: int = 1500, item_cap: int = 300) -> str:
    """Compact `[relevant memory]` block; "" when there is nothing to show."""
    header = "[relevant memory]"
    lines, used = [], len(header) + 1
    for h in hits:
        text = " ".join(h.text.split())
        if len(text) > item_cap:
            text = text[: item_cap - 1].rstrip() + "…"
        if h.kind == "fact":
            dom = f" [{h.domain}]" if h.domain and h.domain != "general" else ""
            line = f"- fact: **{h.subject}{dom}:** {text}"
        else:
            day = f" {str(h.created_at)[:10]}" if h.created_at else ""
            line = f"- episode{day}: {text}"
        if used + len(line) + 1 > char_cap:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return header + "\n" + "\n".join(lines)


def relevant_memory_block(
    db_path,
    query: str,
    exclude_subjects: Optional[set] = None,
    k: int = 5,
    char_cap: int = 1500,
    embed_fn=embed_query,
) -> str:
    """Retrieve + dedup against `exclude_subjects` (spawn-time facts) + format.
    Returns "" when the DB is missing or nothing matches. May raise."""
    from pathlib import Path
    if not Path(db_path).exists():
        return ""
    exclude = exclude_subjects or set()
    conn = sqlite3.connect(str(db_path), timeout=2.0)
    try:
        hits = search(conn, query, k=k + len(exclude) if exclude else k * 3, embed_fn=embed_fn)
    finally:
        conn.close()
    hits = [h for h in hits if not (h.kind == "fact" and subject_key(h.subject) in exclude)]
    return format_block(hits[:k], char_cap=char_cap)
