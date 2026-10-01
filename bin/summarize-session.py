#!/usr/bin/env python3
"""
Session Summarizer — Generates session summaries for agent context preservation

Reads recent agent stream logs, calls Claude to generate a summary, validates
required headers, and outputs to checkpoint file for next session re-injection.
"""

import argparse
from claude_bin import claude_bin
import json
import os
import sqlite3
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
# NOTE (2026-08-06): this used to point at logs/agent-streams/, which
# nothing has ever written to — see cost-model-migration.md. Real
# per-turn history lives in the Claude Code CLI's own transcript files,
# one per session, under ~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl
# — same format bin/friction-sensor.py already parses correctly.
DB_PATH = WORKSPACE_ROOT / "data" / "memory" / "agent-server.db"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SUMMARY_DIR = WORKSPACE_ROOT / "logs" / "session-summaries"
LAST_SUMMARY_TEMPLATE = WORKSPACE_ROOT / "data" / "last-session-summary-{agent}.md"
AUDIT_LOG = WORKSPACE_ROOT / "logs" / "summarizer-audit.jsonl"

REQUIRED_HEADERS = [
    "## Primary Task",
    "## Current State",
    "## Key Context for Next Session"
]

SUMMARIZER_PROMPT = """You are a session summarizer. Your job is to read the recent agent activity stream and generate a concise summary that will be injected into the agent's next session to preserve context.

The summary must contain these sections:

## Primary Task
What is the agent currently working on? 1-2 sentences.

## Current State
Where did the agent leave off? What's the next step? 2-3 sentences.

## Key Context for Next Session
Critical information that must be preserved (decisions made, files changed, commitments, blockers). Bullet list, max 8 items. If the activity below spans multiple distinct threads of work, not just one, make sure at least one bullet covers each thread rather than letting the most recent one crowd out earlier ones.

Keep it concise — aim for 200-350 words total. Do not include full transcripts or code snippets.

Recent agent activity:
{stream_content}
"""

def find_session_id(agent: str) -> Optional[str]:
    """Look up the agent's live Claude CLI session_id from agent-server's own
    DB — the same id it passes to `claude --resume` on every subprocess
    (re)start (see agent-server.py's `sessions` table)."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cur = conn.execute("SELECT session_id FROM sessions WHERE agent = ?", (agent,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None

def find_transcript_path(session_id: str) -> Optional[Path]:
    """Locate the Claude CLI transcript file for a session_id. Search all
    project dirs rather than assuming the sanitized-cwd directory name
    (currently "-workspace"), since that encoding isn't a documented
    stable contract — a glob is cheap and doesn't depend on it."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    matches = list(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None

def read_recent_stream(agent: str, limit: int = 200) -> str:
    """Read the last N text/tool_use events out of the agent's live Claude
    CLI transcript.

    limit raised 50 -> 200 (and per-block truncation 200 -> 400 chars)
    2026-08-07, same day auto-compaction got re-enabled: with the old
    default, a long multi-threaded session's summary was recency-biased
    to whatever happened in the last ~50 events, which for a session that
    size could mean the summary only reflected the most recent thread of
    work and silently dropped everything earlier — not compressed, gone,
    since compact_session() clears the actual session (no --resume) and
    this summary is the only thing carried into the next one. Caught
    before it actually happened, not after — no confirmed real data loss
    yet, but the mechanism clearly had the shape for it. 200 events is
    still bounded (the summarizer call itself has a 20s timeout to stay
    inside), just a meaningfully wider recent window than 50.

    Real per-turn history — replaces the old
    logs/agent-streams/ read, which nothing ever populated.

    (The "20s timeout" note above is now 45s — see call_summarizer() —
    raised 2026-08-07 after a real "Failed to generate summary: timeout"
    on a live session; the 200-vs-50 event-count sizing above is
    unaffected by the timeout change, left as-is.)"""
    session_id = find_session_id(agent)
    if not session_id:
        return ""

    transcript_path = find_transcript_path(session_id)
    if not transcript_path:
        return ""

    with open(transcript_path) as f:
        all_lines = f.readlines()

    # Transcript lines interleave thinking/meta/tool_result entries that
    # don't contribute a summary-worthy item, so scan more raw lines than
    # `limit` (backward, most-recent-first) to reliably collect `limit`
    # real ones without reading the whole file for very long sessions.
    formatted = []
    for line in reversed(all_lines[-(limit * 8):]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    formatted.append(f"[TEXT] {text[:400]}")
            elif btype == "tool_use":
                formatted.append(f"[TOOL] {block.get('name', 'unknown')}")
        if len(formatted) >= limit:
            break

    formatted.reverse()
    return "\n".join(formatted)

def call_summarizer(stream_content: str) -> tuple[bool, str, dict]:
    """Call Claude to generate summary"""
    prompt = SUMMARIZER_PROMPT.format(stream_content=stream_content)

    cmd = [
        claude_bin(), "-p", prompt,
        "--model", "sonnet",
        "--max-turns", "1",
        "--output-format", "stream-json",
        # Required as of the CLI version on this box (2.1.197) — `-p` with
        # `--output-format stream-json` and no `--verbose` now hard-fails
        # with "requires --verbose" before producing any output at all.
        # Found 2026-08-06 by actually running this script instead of
        # trusting it worked (it had never been exercised on this
        # install — see cost-model-migration.md).
        "--verbose",
    ]

    start_time = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # 20 -> 45s, 2026-08-07: hit a real "timeout" failure on a live
            # session (agent-server.log 07:19:50) with the 200-event/400-char
            # window this call reads (raised same day auto-compaction was
            # re-enabled) — 20s was already tight and only gets tighter as
            # sessions grow, since compaction now targets a fixed 200k
            # instead of a fraction of the window. Paired with
            # agent-server.py's own wrapping subprocess.run timeout going
            # 60 -> 75s so it doesn't cut this one off first.
            timeout=45
        )

        duration_ms = (time.time() - start_time) * 1000

        if result.returncode != 0:
            return False, "", {
                "error": "subprocess_failed",
                "stderr": result.stderr[-500:],
                "duration_ms": duration_ms,
            }

        # Parse stream-json output. The real event shape is
        # {"type": "assistant", "message": {"content": [...]}} for each
        # turn, then one {"type": "result", "result": "<final text>"} —
        # NOT a flat {"type": "text", "text": ...} event, which is what
        # this loop checked for before 2026-08-06 and would never match
        # anything real. Pull straight from the result event, same
        # pattern agent-server.py's read_agent_response() already uses
        # in production.
        summary = ""
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                summary = event.get("result", "") or event.get("error", "")
                break

        summary = summary.strip()

        # Validate required headers
        missing = [h for h in REQUIRED_HEADERS if h not in summary]
        if missing:
            return False, summary, {"error": "missing_headers", "missing": missing, "duration_ms": duration_ms}

        return True, summary, {"duration_ms": duration_ms}

    except subprocess.TimeoutExpired:
        duration_ms = (time.time() - start_time) * 1000
        return False, "", {"error": "timeout", "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        return False, "", {"error": str(e), "duration_ms": duration_ms}

def save_summary(agent: str, summary: str):
    """Save summary to checkpoint file and timestamped archive"""
    # Create checkpoint (overwrites)
    checkpoint_path = Path(str(LAST_SUMMARY_TEMPLATE).format(agent=agent))
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with open(checkpoint_path, "w") as f:
        f.write(summary)

    # Create timestamped copy
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive_path = SUMMARY_DIR / f"{agent}-{timestamp}.md"
    with open(archive_path, "w") as f:
        f.write(summary)

def log_audit(event: str, agent: str, success: bool, metadata: dict):
    """Log to audit trail"""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now().isoformat(),
        "event": event,
        "agent": agent,
        "success": success,
        **metadata
    }

    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

def main():
    parser = argparse.ArgumentParser(description="Generate session summary for agent")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument("--limit", type=int, default=200, help="Number of stream lines to read")

    args = parser.parse_args()

    # Read recent stream
    stream_content = read_recent_stream(args.agent, args.limit)

    if not stream_content:
        print(f"No recent stream data for {args.agent}", file=sys.stderr)
        log_audit("summarize", args.agent, False, {"error": "no_stream_data"})
        sys.exit(1)

    # Generate summary
    success, summary, metadata = call_summarizer(stream_content)

    if not success:
        print(f"Failed to generate summary: {metadata.get('error')}", file=sys.stderr)
        log_audit("summarize", args.agent, False, metadata)
        sys.exit(1)

    # Save summary
    save_summary(args.agent, summary)

    log_audit("summarize", args.agent, True, metadata)
    print(f"Summary generated and saved for {args.agent}")

if __name__ == "__main__":
    main()
