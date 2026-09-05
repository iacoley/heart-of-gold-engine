#!/usr/bin/env python3
"""
Karakos MCP Tool Server — JSON-RPC 2.0 over stdin/stdout

Discovers tools from skills/*/tools.json, validates calls,
dispatches to skill scripts, maintains audit trail.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
SKILLS_DIR = WORKSPACE / "skills"
HEALTH_FILE = WORKSPACE / "data" / "health" / "mcp-tools.json"
AUDIT_DB_PATH = WORKSPACE / "data" / "mcp-tools-audit.db"

# Maximum payload size for tool arguments
MAX_ARGS_SIZE = 65536

# =============================================================================
# Core Tools (ship by default)
# =============================================================================

CORE_TOOLS = [
    {
        "name": "workspace",
        "description": "System config, agent registry, version info. Actions: status (show workspace info), agents (list agents), config (show system config).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "agents", "config"],
                    "description": "The action to perform"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "session",
        "description": "Session lifecycle management. Actions: finalize (generate summary), load_last (retrieve checkpoint).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["finalize", "load_last"],
                    "description": "The action to perform"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "memory",
        "description": "Query episodic memory. Actions: recall (search episodes), facts (search facts), recent (recent episodes).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["recall", "facts", "recent"],
                    "description": "The action to perform"
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for recall and facts)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 10)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "discord",
        "description": "Discord server read-only access. Actions: history (view messages), channels (list configured channels), online (list members), channel_info (live name/type lookup by ID or configured name -- the only action here that actually calls Discord's API rather than echoing local config/logs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["history", "channels", "online", "channel_info"],
                    "description": "The action to perform"
                },
                "channel": {
                    "type": "string",
                    "description": "Channel name or ID (required for history and channel_info)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of messages (default 20, max 50)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "taskboard",
        "description": "Task and todo tracking. Actions: list (show tasks), add (create task), update (modify task), complete (mark done).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "update", "complete"],
                    "description": "The action to perform"
                },
                "title": {
                    "type": "string",
                    "description": "Task title (for add)"
                },
                "id": {
                    "type": "string",
                    "description": "Task ID (for update/complete)"
                },
                "status": {
                    "type": "string",
                    "description": "New status (for update)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "vault",
        "description": "Git-backed knowledge vault. Actions: pull (sync from remote), push (sync to remote), status (show git status).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pull", "push", "status"],
                    "description": "The action to perform"
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (for push)"
                }
            },
            "required": ["action"]
        }
    },
]


# =============================================================================
# Audit Database
# =============================================================================

def init_audit_db():
    """Initialize audit trail database."""
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUDIT_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT,
            result_size_bytes INTEGER,
            duration_ms REAL,
            success INTEGER,
            error_msg TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name)")
    conn.commit()
    return conn


def log_audit(conn, tool_name, args_json, result_size, duration_ms, success, error_msg=None):
    """Record tool call in audit trail."""
    try:
        conn.execute(
            "INSERT INTO tool_calls (timestamp, tool_name, args_json, result_size_bytes, "
            "duration_ms, success, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                tool_name,
                args_json[:1024] if args_json else None,
                result_size,
                duration_ms,
                1 if success else 0,
                error_msg,
            )
        )
        conn.commit()
    except Exception:
        pass


def write_health():
    """Write health heartbeat."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy",
    }))


# =============================================================================
# Skill Discovery
# =============================================================================

def discover_skills() -> list[dict]:
    """Scan skills/*/tools.json for tool definitions.

    This package's skill convention is tools.json + scripts/ (see
    docs/EXTENDING.md). A skill directory that ships only a frontmatter-style
    SKILL.md (the Anthropic Agent Skills convention) has no tools.json for
    this server to read, so it is skipped — but skipped loudly, on stderr,
    naming the file, per issue #84.
    """
    tools = []
    if not SKILLS_DIR.exists():
        return tools

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        tools_file = skill_dir / "tools.json"
        if not tools_file.exists():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                sys.stderr.write(
                    f"Skipping {skill_md}: no tools.json in {skill_dir}. "
                    "This server only loads the tools.json + scripts/ skill "
                    "convention (docs/EXTENDING.md); frontmatter-only "
                    "SKILL.md files are not discovered.\n"
                )
            continue
        try:
            data = json.loads(tools_file.read_text())
            for tool in data.get("tools", []):
                tool["_skill_dir"] = str(skill_dir)
                tools.append(tool)
        except Exception as e:
            sys.stderr.write(f"Error loading {tools_file}: {e}\n")

    return tools


# =============================================================================
# Input Validation
# =============================================================================

def validate_args(args: dict, schema: dict) -> str | None:
    """Basic JSON Schema validation. Returns error message or None."""
    if schema.get("type") != "object":
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for field in required:
        if field not in args:
            return f"Missing required field: {field}"

    for key, value in args.items():
        if key not in properties:
            continue
        prop_schema = properties[key]

        # Type check
        expected_type = prop_schema.get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"Field '{key}' must be a string"
        elif expected_type == "integer" and not isinstance(value, int):
            return f"Field '{key}' must be an integer"
        elif expected_type == "number" and not isinstance(value, (int, float)):
            return f"Field '{key}' must be a number"
        elif expected_type == "boolean" and not isinstance(value, bool):
            return f"Field '{key}' must be a boolean"

        # Enum check
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            return f"Field '{key}' must be one of: {prop_schema['enum']}"

        # Path safety (reject traversal unless explicitly allowed)
        if isinstance(value, str) and ".." in value:
            if not prop_schema.get("path_mode") == "absolute":
                return f"Field '{key}' contains path traversal"

    return None


# =============================================================================
# Tool Dispatch
# =============================================================================

def handle_core_tool(tool_name: str, args: dict) -> dict:
    """Handle built-in core tools."""

    if tool_name == "workspace":
        action = args.get("action", "status")
        if action == "status":
            config_path = WORKSPACE / ".karakos" / "config.json"
            config = {}
            if config_path.exists():
                config = json.loads(config_path.read_text())
            return {
                "system_name": config.get("system_name", os.environ.get("SYSTEM_NAME", "Karakos")),
                "version": config.get("version", "1.0.0"),
                "owner": config.get("owner_name", os.environ.get("OWNER_NAME", "User")),
                "workspace": str(WORKSPACE),
            }
        elif action == "agents":
            agents_path = WORKSPACE / "config" / "agents.json"
            if agents_path.exists():
                return json.loads(agents_path.read_text())
            return {"agents": {}}
        elif action == "config":
            config_path = WORKSPACE / ".karakos" / "config.json"
            if config_path.exists():
                return json.loads(config_path.read_text())
            return {}

    elif tool_name == "session":
        action = args.get("action", "load_last")
        if action == "finalize":
            try:
                result = subprocess.run(
                    ["python3", str(WORKSPACE / "bin" / "summarize-session.py")],
                    capture_output=True, text=True, timeout=30, cwd=str(WORKSPACE)
                )
                return {"status": "ok" if result.returncode == 0 else "error",
                        "output": result.stdout.strip()}
            except Exception as e:
                return {"error": str(e)}
        elif action == "load_last":
            # Check for session summary files
            data_dir = WORKSPACE / "data"
            summaries = sorted(data_dir.glob("last-session-summary-*.md"))
            if summaries:
                latest = summaries[-1]
                age_hours = (time.time() - latest.stat().st_mtime) / 3600
                return {
                    "status": "success",
                    "summary": latest.read_text(),
                    "age_hours": round(age_hours, 1),
                    "path": str(latest),
                }
            return {"status": "not_found"}

    elif tool_name == "memory":
        action = args.get("action", "recent")
        memory_db = WORKSPACE / "data" / "memory" / "memory.db"
        if not memory_db.exists():
            return {"error": "Memory database not found"}

        conn = sqlite3.connect(str(memory_db))
        conn.row_factory = sqlite3.Row
        limit = args.get("limit", 10)

        if action == "recent":
            rows = conn.execute(
                "SELECT id, summary, importance, created_at FROM episodes "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return {"episodes": [dict(r) for r in rows]}

        elif action == "recall":
            query = args.get("query", "")
            rows = conn.execute(
                "SELECT id, summary, importance, created_at FROM episodes "
                "WHERE summary LIKE ? ORDER BY importance DESC LIMIT ?",
                (f"%{query}%", limit)
            ).fetchall()
            return {"episodes": [dict(r) for r in rows]}

        elif action == "facts":
            query = args.get("query", "")
            rows = conn.execute(
                "SELECT id, subject, content, confidence, domain FROM facts "
                "WHERE content LIKE ? OR subject LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)
            ).fetchall()
            return {"facts": [dict(r) for r in rows]}

        conn.close()

    elif tool_name == "discord":
        action = args.get("action", "channels")
        if action == "channels":
            channels_path = WORKSPACE / "config" / "channels.json"
            if channels_path.exists():
                return json.loads(channels_path.read_text())
            return {"channels": {}}
        elif action == "history":
            channel = args.get("channel", "general")
            limit = min(args.get("limit", 20), 50)
            # Read from JSONL capture
            today = datetime.now().strftime("%Y-%m-%d")
            log_path = WORKSPACE / "data" / "messages" / f"messages-{today}.jsonl"
            if not log_path.exists():
                return {"messages": [], "channel": channel}
            messages = []
            for line in log_path.read_text().strip().split("\n"):
                try:
                    msg = json.loads(line)
                    if msg.get("channel_name") == channel:
                        messages.append({
                            "ts": msg.get("ts", ""),
                            "author": msg.get("author_name", ""),
                            "content": msg.get("content", "")[:500],
                            "is_bot": msg.get("is_bot", False),
                        })
                except json.JSONDecodeError:
                    continue
            return {"messages": messages[-limit:], "channel": channel}
        elif action == "online":
            return {"error": "Online member list requires Discord API access"}
        elif action == "channel_info":
            # The one Discord read action that's a real live API call rather
            # than an echo of channels.json ("channels") or the local JSONL
            # capture ("history") -- added 2026-09-05 after a rename
            # (agent-chat -> "the-banana-stand" in Discord's UI) couldn't be
            # confirmed any other way. A rename never changes the channel
            # ID, so this exists to answer exactly one question: does this
            # ID currently resolve to that name. Read-only (GET only, no
            # token scope for anything else); matches the same
            # already-authorized bot token every other Discord action uses,
            # so this is closing a code gap, not requesting new permissions.
            channel_arg = args.get("channel")
            if not channel_arg:
                return {"error": "channel (ID or configured name) is required for channel_info"}
            channel_id = channel_arg
            channels_path = WORKSPACE / "config" / "channels.json"
            if channels_path.exists():
                cfg = json.loads(channels_path.read_text())
                configured = cfg.get("channels", {}).get(channel_arg)
                if configured and configured.get("id"):
                    channel_id = configured["id"]
            token = os.environ.get("DISCORD_BOT_TOKEN_PRIMARY")
            if not token:
                return {"error": "DISCORD_BOT_TOKEN_PRIMARY not set in environment"}
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{channel_id}",
                headers={
                    "Authorization": f"Bot {token}",
                    # Cloudflare (in front of discord.com) blocks urllib's
                    # default User-Agent outright (error 1010) before the
                    # request ever reaches Discord's own auth check --
                    # found live 2026-09-05 debugging a false 403. Discord's
                    # own docs recommend this exact format.
                    "User-Agent": (
                        "DiscordBot (https://github.com/iacoley/"
                        "heart-of-gold-engine, 1.0)"
                    ),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                return {"error": f"Discord API error {e.code}: {e.reason}", "queried_id": channel_id}
            except urllib.error.URLError as e:
                return {"error": f"request failed: {e.reason}", "queried_id": channel_id}
            return {
                "id": data.get("id"),
                "name": data.get("name"),
                "guild_id": data.get("guild_id"),
                "type": data.get("type"),
            }

    elif tool_name == "taskboard":
        # Simple file-based task tracking
        tasks_file = WORKSPACE / "data" / "taskboard.json"
        action = args.get("action", "list")

        def _maybe_mark_email_read(task: dict) -> None:
            """Completing a task created by read_marvin_folder.py's
            email-intake (source="email-intake", email_uid=<uid>) also
            flips that message's \\Seen flag, via the same skill-script
            path a normal tool call would use — so Gmail's own read state
            doubles as the record of what's actually been addressed.
            Added 2026-09-01 per Ian's sign-off. Best-effort: a failure
            here must not block the task completion itself."""
            if task.get("source") != "email-intake" or "email_uid" not in task:
                return
            try:
                skill_dir = SKILLS_DIR / "email"
                env = os.environ.copy()
                env["WORKSPACE_ROOT"] = str(WORKSPACE)
                env["TOOL_ARGS"] = json.dumps({"uid": task["email_uid"], "read": True})
                subprocess.run(
                    ["python3", str(skill_dir / "scripts" / "mark_email_read.py")],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(skill_dir), env=env,
                )
            except Exception:
                pass  # best-effort; the task itself still completes

        tasks = []
        if tasks_file.exists():
            tasks = json.loads(tasks_file.read_text()).get("tasks", [])

        if action == "list":
            return {"tasks": tasks}
        elif action == "add":
            task = {
                "id": f"task-{int(time.time())}",
                "title": args.get("title", "Untitled"),
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            tasks.append(task)
            tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
            return {"task": task}
        elif action == "complete":
            task_id = args.get("id", "")
            for task in tasks:
                if task["id"] == task_id:
                    task["status"] = "done"
                    task["completed_at"] = datetime.now(timezone.utc).isoformat()
                    tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
                    _maybe_mark_email_read(task)
                    return {"task": task}
            return {"error": f"Task not found: {task_id}"}
        elif action == "update":
            task_id = args.get("id", "")
            new_status = args.get("status")
            if not task_id:
                return {"error": "update requires 'id'"}
            if not new_status:
                return {"error": "update requires 'status'"}
            for task in tasks:
                if task["id"] == task_id:
                    was_done = task["status"] == "done"
                    task["status"] = new_status
                    task["updated_at"] = datetime.now(timezone.utc).isoformat()
                    if new_status == "done" and "completed_at" not in task:
                        task["completed_at"] = task["updated_at"]
                    tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
                    if new_status == "done" and not was_done:
                        _maybe_mark_email_read(task)
                    return {"task": task}
            return {"error": f"Task not found: {task_id}"}
        return {"error": f"Unknown taskboard action: {action}"}

    elif tool_name == "vault":
        action = args.get("action", "status")
        vault_dir = WORKSPACE / "vault"
        if not vault_dir.exists():
            return {"error": "Vault directory not found. Create it with: git clone <repo> vault/"}

        if action == "status":
            result = subprocess.run(
                ["git", "status", "--short"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"status": result.stdout.strip(), "clean": not result.stdout.strip()}
        elif action == "pull":
            result = subprocess.run(
                ["git", "pull"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"output": result.stdout.strip(), "success": result.returncode == 0}
        elif action == "push":
            msg = args.get("message", "Auto-commit from vault tool")
            subprocess.run(["git", "add", "-A"], cwd=str(vault_dir))
            subprocess.run(["git", "commit", "-m", msg], cwd=str(vault_dir))
            result = subprocess.run(
                ["git", "push"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"output": result.stdout.strip(), "success": result.returncode == 0}

    return {"error": f"Unknown tool or action: {tool_name}"}


def handle_skill_tool(tool: dict, args: dict) -> dict:
    """Dispatch to a skill script."""
    skill_dir = Path(tool.get("_skill_dir", ""))
    scripts_dir = skill_dir / "scripts"

    # Find the implementation script
    script = None
    for ext in [".py", ".sh"]:
        candidate = scripts_dir / f"{tool['name']}{ext}"
        if candidate.exists():
            script = candidate
            break
    # Also check for a main script
    if not script:
        for ext in [".py", ".sh"]:
            candidate = scripts_dir / f"main{ext}"
            if candidate.exists():
                script = candidate
                break

    if not script:
        return {"error": f"No implementation script found for tool '{tool['name']}'"}

    # Execute skill script
    try:
        env = os.environ.copy()
        env["WORKSPACE_ROOT"] = str(WORKSPACE)
        env["TOOL_ARGS"] = json.dumps(args)

        if script.suffix == ".py":
            # Escape valve for skills with dependencies too heavy or too
            # version-sensitive to vendor by hand (large ML packages, native
            # extensions, etc.): if the skill ships its own skills/<name>/.venv,
            # use that interpreter. Otherwise fall back to bare "python3",
            # which resolves to the system interpreter — this MCP server is
            # itself launched that way (see .mcp.json), not via the repo's
            # own .venv, so most skills should keep vendoring small pure-Python
            # deps into skills/<name>/vendor/ rather than assuming a shared
            # environment exists. Pattern confirmed live and working by Amos
            # (Mike's Karakos instance) 2026-08-28 — he uses the identical
            # per-skill-.venv-as-escape-valve approach for three of his own
            # skills (voice-transcribe/elevenlabs-voice/recipe-scraper) rather
            # than one shared environment, specifically to avoid collisions
            # between packages like faster-whisper/ctranslate2 and everything
            # else. Deliberately not "unify everything into one venv" — that
            # was the original plan here until his answer changed it.
            skill_venv_python = skill_dir / ".venv" / "bin" / "python3"
            interpreter = str(skill_venv_python) if skill_venv_python.exists() else "python3"
            cmd = [interpreter, str(script)]
        else:
            cmd = ["bash", str(script)]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=str(skill_dir), env=env
        )

        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"Script exited with code {result.returncode}"}

        # Try to parse as JSON
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"output": result.stdout.strip()}

    except subprocess.TimeoutExpired:
        return {"error": "Skill script timed out (60s limit)"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# JSON-RPC Server
# =============================================================================

def main():
    """Run MCP tool server (JSON-RPC 2.0 over stdin/stdout)."""
    audit_conn = init_audit_db()

    # Discover all tools
    skill_tools = discover_skills()
    all_tools = CORE_TOOLS + skill_tools

    # Build tool registry
    tool_registry = {}
    for tool in all_tools:
        tool_registry[tool["name"]] = tool

    # Test mode
    if len(sys.argv) > 1 and sys.argv[1] == "--test-tool":
        if len(sys.argv) < 3:
            print("Usage: tools-server.py --test-tool <name> [args_json]")
            sys.exit(1)
        tool_name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        if tool_name not in tool_registry:
            print(f"Unknown tool: {tool_name}")
            print(f"Available: {list(tool_registry.keys())}")
            sys.exit(1)
        result = handle_core_tool(tool_name, args) if tool_name in [t["name"] for t in CORE_TOOLS] else handle_skill_tool(tool_registry[tool_name], args)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    # Main JSON-RPC loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "system-tools", "version": "1.0.0"},
                },
                "id": req_id,
            }

        elif method == "notifications/initialized":
            # Notification, not a request — no response body, and no
            # req_id to reply to even if we wanted to.
            continue

        elif method == "tools/list":
            # write_health() previously only fired on a successful
            # tools/call, so a session that never happened to invoke a
            # tool through this server looked "missing" to health-monitor
            # even though the server was up and fine — found via a real
            # alert 2026-08-06. tools/list fires once per session at
            # startup regardless of what the session goes on to use, so
            # writing here too catches that case without needing a
            # separate timer thread in this stdio-loop process.
            write_health()
            # Return all registered tools
            tools_list = []
            for tool in all_tools:
                tools_list.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                })
            response = {
                "jsonrpc": "2.0",
                "result": {"tools": tools_list},
                "id": req_id,
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            args_json = json.dumps(args)

            if len(args_json) > MAX_ARGS_SIZE:
                response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32602, "message": f"Arguments too large ({len(args_json)} > {MAX_ARGS_SIZE})"},
                    "id": req_id,
                }
            elif tool_name not in tool_registry:
                response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    "id": req_id,
                }
            else:
                tool = tool_registry[tool_name]

                # Validate args
                validation_error = validate_args(args, tool.get("inputSchema", {}))
                if validation_error:
                    response = {
                        "jsonrpc": "2.0",
                        "error": {"code": -32602, "message": validation_error},
                        "id": req_id,
                    }
                else:
                    start = time.time()
                    try:
                        if tool_name in [t["name"] for t in CORE_TOOLS]:
                            result = handle_core_tool(tool_name, args)
                        else:
                            result = handle_skill_tool(tool, args)

                        duration_ms = (time.time() - start) * 1000
                        result_json = json.dumps(result)
                        log_audit(audit_conn, tool_name, args_json, len(result_json), duration_ms, True)
                        write_health()

                        response = {
                            "jsonrpc": "2.0",
                            "result": {"content": [{"type": "text", "text": result_json}]},
                            "id": req_id,
                        }
                    except Exception as e:
                        duration_ms = (time.time() - start) * 1000
                        log_audit(audit_conn, tool_name, args_json, 0, duration_ms, False, str(e))

                        response = {
                            "jsonrpc": "2.0",
                            "error": {"code": -32603, "message": str(e)},
                            "id": req_id,
                        }
        else:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
                "id": req_id,
            }

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
