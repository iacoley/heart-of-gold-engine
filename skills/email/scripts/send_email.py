#!/usr/bin/env python3
"""
send_email skill — outbound mail via Mailgun, from marvin@iancoley.org.

Reads MAILGUN_API_KEY / MAILGUN_DOMAIN directly from config/.env rather
than the process environment. They were added to .env 2026-08-06 after
the container was already running, and docker-compose's env_file loading
only happens at container creation — so they won't reach os.environ until
the container is recreated, which nobody was going to force just for
this. Reading the file directly sidesteps that entirely and keeps this
script correct regardless of when the container is next restarted.

The MCP tool server calls this script with TOOL_ARGS as a JSON environment
variable (to/subject/body/from_name, optional attachments: list of absolute
file paths). Output JSON to stdout; non-zero exit + stderr text is treated
as an error by the caller.

Attachment support added 2026-08-13: switches to Mailgun's multipart form
endpoint (via `requests`, already present in the venv) whenever attachments
are supplied. Text-only sends still use the original urlencoded urllib path
unchanged, so existing behavior/callers are untouched.

Multi-recipient support added 2026-09-01: `to` accepts either a single
address string (unchanged) or a list of address strings, joined into the
comma-separated form Mailgun's `to` field already natively accepts —
Mailgun delivers one message with all recipients visible to each other in
the To: header, not N separate sends. If that visibility isn't wanted
(e.g. recipients who shouldn't see each other's address), call this once
per recipient instead.

cc/bcc added same day, same pattern: each accepts a single address string
or a list, normalized the same way as `to` and passed straight through as
Mailgun's own `cc`/`bcc` form fields — Mailgun (not this script) handles
actually hiding bcc recipients from the To:/Cc: headers other recipients
see; this script never puts bcc addresses anywhere but the bcc field.

Standing bcc to Ian added 2026-09-01, his request: mail sent through
Mailgun never touches Ian's own Gmail (no Sent-folder copy, nothing),
so without this there's no trail on his side at all of what went out
under his name. IAN_BCC_ADDRESS is merged into whatever bcc the caller
supplies (deduped, not replaced) rather than being a caller-settable
argument — the whole point is that it happens regardless of what any
given call passes.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
ENV_PATH = WORKSPACE_ROOT / "config" / ".env"

# Not a parameter — see module docstring. Always merged into bcc so every
# send leaves Ian a copy, since Mailgun sends bypass his Gmail entirely.
IAN_BCC_ADDRESS = "iacoley.phone@gmail.com"

# Appended to every outbound body so recipients know they're hearing from
# an AI assistant, not Ian directly, rather than guessing from a slightly
# odd sender name. Ian approved the wording 2026-09-04; a second line
# claiming replies reach Ian was cut same day — this mailbox is Marvin's,
# not a relay, so that line was simply false.
SIGNATURE = "\n\n—\nMarvin, AI assistant to Ian Coley"


def load_env_var(name: str) -> str:
    """Read a single KEY=VALUE line directly from config/.env, bypassing
    the process environment (see module docstring for why)."""
    if not ENV_PATH.exists():
        return ""
    pattern = re.compile(rf"^{re.escape(name)}=(.*)$")
    for line in ENV_PATH.read_text().splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1).strip()
    return ""


def normalize_addrs(value) -> str:
    """Accept a single address string or a list of address strings, return
    the comma-separated form Mailgun's to/cc/bcc fields all natively take.
    Blank/None entries in a list are dropped rather than producing stray
    commas."""
    if isinstance(value, list):
        return ", ".join(addr.strip() for addr in value if addr and addr.strip())
    return value or ""


def with_standing_bcc(bcc: str) -> str:
    """Merge IAN_BCC_ADDRESS into a normalized bcc string, deduped
    case-insensitively so an explicit bcc that already includes Ian
    doesn't double him up."""
    addrs = [a.strip() for a in bcc.split(",") if a.strip()]
    if IAN_BCC_ADDRESS.lower() not in {a.lower() for a in addrs}:
        addrs.append(IAN_BCC_ADDRESS)
    return ", ".join(addrs)


def main():
    args = json.loads(os.environ.get("TOOL_ARGS", "{}"))
    to = normalize_addrs(args.get("to", ""))
    cc = normalize_addrs(args.get("cc", ""))
    bcc = with_standing_bcc(normalize_addrs(args.get("bcc", "")))
    subject = args.get("subject", "")
    body = args.get("body", "")
    from_name = args.get("from_name", "Marvin")
    attachments = args.get("attachments") or []

    if not to or not subject or not body:
        print(json.dumps({"error": "to, subject, and body are all required"}))
        sys.exit(1)

    body += SIGNATURE

    api_key = load_env_var("MAILGUN_API_KEY")
    domain = load_env_var("MAILGUN_DOMAIN")

    if not api_key or not domain:
        print(json.dumps({
            "error": "MAILGUN_API_KEY or MAILGUN_DOMAIN missing from config/.env"
        }))
        sys.exit(1)

    # Mailgun's sending domain (mg.iancoley.org) is separate from the
    # visible From address (marvin@iancoley.org) — this is the standard,
    # documented Mailgun pattern for a subdomain-verified sender, and
    # passes DMARC's default relaxed alignment since both share the same
    # organizational domain.
    mailgun_api_domain = domain if domain.startswith("mg.") else f"mg.{domain}"
    from_domain = domain[3:] if domain.startswith("mg.") else domain
    from_address = f"{from_name} <marvin@{from_domain}>"
    url = f"https://api.mailgun.net/v3/{mailgun_api_domain}/messages"

    # Shared field set for both send paths below — cc/bcc only added when
    # actually supplied, so a plain to-only send's request body is
    # byte-for-byte what it was before cc/bcc existed.
    fields = {"from": from_address, "to": to, "subject": subject, "text": body}
    if cc:
        fields["cc"] = cc
    if bcc:
        fields["bcc"] = bcc
    response_addrs = {"to": to, **({"cc": cc} if cc else {}), **({"bcc": bcc} if bcc else {})}

    if attachments:
        missing = [p for p in attachments if not Path(p).is_file()]
        if missing:
            print(json.dumps({"error": f"attachment(s) not found: {missing}"}))
            sys.exit(1)

        import requests

        opened = [open(p, "rb") for p in attachments]
        try:
            files = [
                ("attachment", (Path(p).name, fh))
                for p, fh in zip(attachments, opened)
            ]
            resp = requests.post(
                url,
                auth=("api", api_key),
                data=fields,
                files=files,
                timeout=30,
            )
        finally:
            for fh in opened:
                fh.close()

        if resp.ok:
            result = resp.json()
            print(json.dumps({
                "status": "sent",
                "message_id": result.get("id", ""),
                "mailgun_response": result.get("message", ""),
                "from": from_address,
                **response_addrs,
                "attachments": [Path(p).name for p in attachments],
            }))
        else:
            print(json.dumps({
                "error": f"Mailgun API error {resp.status_code}: {resp.text}"
            }))
            sys.exit(1)
        return

    payload = urllib.parse.urlencode(fields).encode()

    request = urllib.request.Request(url, data=payload, method="POST")
    credentials = f"api:{api_key}".encode()
    import base64
    request.add_header(
        "Authorization", f"Basic {base64.b64encode(credentials).decode()}"
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode())
            print(json.dumps({
                "status": "sent",
                "message_id": result.get("id", ""),
                "mailgun_response": result.get("message", ""),
                "from": from_address,
                **response_addrs,
            }))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="ignore")
        print(json.dumps({
            "error": f"Mailgun API error {e.code}: {error_body}"
        }))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"Send failed: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
