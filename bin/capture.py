#!/usr/bin/env python3
"""
Message Capture — Persist Discord messages to daily JSONL logs.

Reads from the agent server's message queue and writes structured JSONL
entries to data/messages/messages-YYYY-MM-DD.jsonl. Called by the relay
after processing each message batch.

Usage:
    capture.py --message '{"channel":"general","author":"user",...}'
    capture.py --backfill DATE   # Re-export from SQLite for a given date
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MESSAGES_DIR = WORKSPACE / "data" / "messages"


def ensure_dirs():
    MESSAGES_DIR.mkdir(parents=True, exist_ok=True)


def utc_date_str(offset_days: int = 0) -> str:
    """UTC calendar date (YYYY-MM-DD) used to name daily message files.

    Every writer and reader of data/messages/messages-*.jsonl must use this
    (or an equivalent explicit-UTC date): processes run under different local
    timezones, so naive datetime.now() makes them disagree on the filename.
    """
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def log_path_for_date(date_str: str) -> Path:
    return MESSAGES_DIR / f"messages-{date_str}.jsonl"


def capture_message(msg: dict) -> None:
    """Append a single message to the daily JSONL log."""
    ensure_dirs()

    ts = msg.get("ts") or datetime.now(timezone.utc).isoformat()
    date_str = ts[:10]  # YYYY-MM-DD

    entry = {
        "ts": ts,
        "channel_name": msg.get("channel", msg.get("channel_name", "")),
        "channel_id": msg.get("channel_id", ""),
        "author_name": msg.get("author", msg.get("author_name", "")),
        "author_id": msg.get("author_id", "0"),
        "is_bot": msg.get("is_bot", False),
        "content": msg.get("content", ""),
        "message_id": msg.get("message_id", ""),
        "agent": msg.get("agent", ""),
        "server": msg.get("server", "discord"),
    }

    path = log_path_for_date(date_str)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def backfill(date_str: str) -> None:
    """Re-export messages from SQLite for a specific date."""
    import sqlite3

    db_path = WORKSPACE / "data" / "agent-server.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT * FROM message_queue WHERE DATE(created_at) = ? ORDER BY created_at",
        (date_str,)
    ).fetchall()

    ensure_dirs()
    path = log_path_for_date(date_str)

    count = 0
    with open(path, "w") as f:
        for row in rows:
            entry = {
                "ts": row["created_at"],
                "channel_name": row["channel"],
                "channel_id": row["channel_id"],
                "author_name": row["author"],
                "author_id": row["author_id"],
                "is_bot": bool(row["is_bot"]),
                "content": row["content"],
                "message_id": row["message_id"],
                "agent": row["agent"],
                "server": row.get("server", "discord") if "server" in row.keys() else "discord",
            }
            f.write(json.dumps(entry) + "\n")
            count += 1

    conn.close()
    print(f"Exported {count} messages to {path}")


def main():
    parser = argparse.ArgumentParser(description="Message capture to JSONL")
    parser.add_argument("--message", type=str, help="JSON message to capture")
    parser.add_argument("--backfill", type=str, metavar="DATE", help="Re-export from SQLite for YYYY-MM-DD")
    args = parser.parse_args()

    if args.backfill:
        backfill(args.backfill)
    elif args.message:
        msg = json.loads(args.message)
        capture_message(msg)
    else:
        # Read from stdin (pipe mode)
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    msg = json.loads(line)
                    capture_message(msg)
                except json.JSONDecodeError:
                    print(f"Invalid JSON: {line}", file=sys.stderr)


if __name__ == "__main__":
    main()
