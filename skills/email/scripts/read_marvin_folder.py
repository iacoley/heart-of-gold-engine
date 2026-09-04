#!/usr/bin/env python3
"""
read_marvin_folder skill — read new mail from the "Marvin" label in Ian's
personal Gmail, via IMAP.

Deliberately does NOT mark messages read/unread in Gmail — that's Ian's
own mailbox and his own read state to control. Instead, "new since last
call" is tracked via IMAP UID (stable across sessions, unlike sequence
numbers) in a local state file. Set include_seen=true to bypass that and
return everything in the folder regardless of history.

Credentials (GMAIL_ADDRESS, GMAIL_APP_PASSWORD) are read directly from
config/.env rather than the process environment, same reasoning as
send_email.py: they were added after the container was already running,
and docker-compose's env_file only loads at container creation.

Hard safeguard, Ian's explicit instruction 2026-08-06: all IMAP access
goes through gmail_guard.MarvinFolderOnly rather than calling imaplib
directly. That wrapper hardcodes the folder (not a parameter, cannot be
overridden) and, as of 2026-09-01, also hardcodes the one flag
(\\Seen) it's allowed to write — see gmail_guard.py for the full
reasoning.

Each genuinely-new message (i.e. every call that isn't include_seen)
also spawns a taskboard entry via create_intake_tasks(), so new mail
has to be explicitly closed out rather than just noted in passing —
see that function's docstring for how it ties back to mark_email_read.
"""

import email
import email.message
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path

from gmail_guard import MarvinFolderOnly

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
ENV_PATH = WORKSPACE_ROOT / "config" / ".env"
STATE_PATH = WORKSPACE_ROOT / "data" / "gmail-marvin-state.json"
TASKS_PATH = WORKSPACE_ROOT / "data" / "taskboard.json"
FOLDER = MarvinFolderOnly.ALLOWED_FOLDER
MAX_BODY_CHARS = 4000  # keep responses reasonable; this is a summary tool, not a full mail client


def load_env_var(name: str) -> str:
    if not ENV_PATH.exists():
        return ""
    pattern = re.compile(rf"^{re.escape(name)}=(.*)$")
    for line in ENV_PATH.read_text().splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1).strip()
    return ""


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def create_intake_tasks(messages: list) -> None:
    """One taskboard entry per genuinely-new message, so a new email has
    to be explicitly closed out rather than just riding along in whatever
    heartbeat context happened to mention it and then quietly falling out
    of the window. Added 2026-09-01 per Ian: "make sure emails get
    addressed when they come in, not just noticed."

    Each task carries source="email-intake" and the message's uid — when
    that task gets marked done, tools-server.py's taskboard handler uses
    those two fields to flip the message's own \\Seen flag via
    mark_email_read.py, so Gmail's read state ends up as the record of
    what's actually been dealt with.

    Same non-atomic read-modify-write as tools-server.py's own taskboard
    handler (data/taskboard.json has no lock file) — this script and a
    live agent turn both touching it in close succession is an existing,
    accepted risk, not a new one introduced here.
    """
    if not messages:
        return
    tasks = []
    if TASKS_PATH.exists():
        try:
            tasks = json.loads(TASKS_PATH.read_text()).get("tasks", [])
        except Exception:
            tasks = []

    now = datetime.now(timezone.utc).isoformat()
    for msg in messages:
        tasks.append({
            "id": f"task-{int(time.time())}-email{msg['uid']}",
            "title": f"Email: \"{msg['subject']}\" from {msg['from']}",
            "status": "pending",
            "created_at": now,
            "source": "email-intake",
            "email_uid": msg["uid"],
        })

    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASKS_PATH.write_text(json.dumps({"tasks": tasks}, indent=2))


def decode_mime_str(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return "(no plain-text body found — message may be HTML-only or all attachments)"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return ""


def main():
    args = json.loads(os.environ.get("TOOL_ARGS", "{}"))
    include_seen = bool(args.get("include_seen", False))

    address = load_env_var("GMAIL_ADDRESS")
    app_password = load_env_var("GMAIL_APP_PASSWORD")

    if not address or not app_password:
        print(json.dumps({
            "error": "GMAIL_ADDRESS or GMAIL_APP_PASSWORD missing from config/.env"
        }))
        sys.exit(1)

    state = load_state()
    last_uid = 0 if include_seen else int(state.get("last_uid", 0))

    try:
        with MarvinFolderOnly(address, app_password) as gmail:
            search_criterion = "ALL" if last_uid == 0 else f"UID {last_uid + 1}:*"
            status, uid_data = gmail.search(search_criterion)
            if status != "OK":
                print(json.dumps({"error": "IMAP search failed"}))
                sys.exit(1)

            uids = [u for u in uid_data[0].decode().split() if u]
            # A UID range search like "N:*" on Gmail returns the last message
            # again even when nothing new exists past it, per IMAP semantics —
            # filter anything not strictly greater than what we've already seen.
            uids = [u for u in uids if include_seen or int(u) > last_uid]

            messages = []
            highest_uid = last_uid
            for uid in uids:
                status, msg_data = gmail.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                body = extract_body(msg)
                if len(body) > MAX_BODY_CHARS:
                    body = body[:MAX_BODY_CHARS] + "... [truncated]"
                messages.append({
                    "uid": int(uid),
                    "from": decode_mime_str(msg.get("From", "")),
                    "to": decode_mime_str(msg.get("To", "")),
                    "cc": decode_mime_str(msg.get("Cc", "")),
                    "subject": decode_mime_str(msg.get("Subject", "")),
                    "date": msg.get("Date", ""),
                    "body": body,
                })
                highest_uid = max(highest_uid, int(uid))

        if not include_seen:
            state["last_uid"] = highest_uid
            save_state(state)
            create_intake_tasks(messages)

        print(json.dumps({
            "folder": FOLDER,
            "new_message_count": len(messages),
            "messages": messages,
        }))

    except imaplib.IMAP4.error as e:
        print(json.dumps({"error": f"IMAP error: {e}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"Read failed: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
