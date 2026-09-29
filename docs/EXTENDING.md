# Extending Karakos

Guide to customizing agents, adding skills, and growing the system.

## Adding a New Agent

```bash
# Inside the container
bin/create-agent.sh --template primary --model sonnet oracle

# With a Discord bot identity
bin/create-agent.sh --template builder --model sonnet \
  --discord-token "$DISCORD_BOT_TOKEN_BUILDER" builder
```

Templates: `primary`, `relay`, `builder`, `reviewer`

The agent is hot-registered — no server restart needed.

## Customizing Agent Personality

Each agent has a persona directory:

```
agents/{name}/
├── SYSTEM_PROMPT.md    # Core instructions (generated from template)
├── persona/
│   └── voice.md        # Voice, tone, behavioral rules
├── inbox/              # Incoming work briefs
└── journal/            # Agent-written logs
```

Edit `persona/voice.md` to customize how the agent communicates. This file is loaded fresh on each session start — no restart needed.

### Example voice.md

```markdown
# Voice

## Tone
Direct and concise. No filler words. Technical when appropriate.

## Addressing Style
Call the owner by first name.

## Boundaries
Never discuss politics or religion. Redirect to practical topics.
```

## Adding a Skill

Skills add new tools to the MCP server. They're automatically discovered at startup.

**This is not Claude Code's built-in Agent Skills feature.** Claude Code has
its own native skill system: a `SKILL.md` file with YAML frontmatter,
auto-discovered from `.claude/skills/`. Karakos "skills" are a separate,
package-specific convention — a `tools.json` schema plus a `scripts/`
implementation, discovered by `mcp/tools-server.py` and exposed as MCP
tools. A frontmatter-only `SKILL.md` dropped under `skills/` will not load
here; it needs `tools.json` and `scripts/` as shown below.

### 1. Create the Skill Directory

```bash
cp -r skills/hello-world skills/my-skill
```

### 2. Define Tools

Edit `skills/my-skill/tools.json`:

```json
{
  "skill_name": "my-skill",
  "version": "1.0.0",
  "description": "What this skill does",
  "tools": [
    {
      "name": "my_tool",
      "description": "What this tool does",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "Search query"
          }
        },
        "required": ["query"]
      }
    }
  ]
}
```

### 3. Implement

Create `skills/my-skill/scripts/my_tool.py`:

```python
#!/usr/bin/env python3
import json, os

args = json.loads(os.environ.get("TOOL_ARGS", "{}"))
query = args.get("query", "")

# Do something useful
result = {"answer": f"You asked about: {query}"}

print(json.dumps(result))
```

Scripts receive `TOOL_ARGS` (JSON) and `WORKSPACE_ROOT` via environment. Print JSON to stdout. Exit code 0 = success.

### 4. Test

```bash
python3 mcp/tools-server.py --test-tool my_tool '{"query": "test"}'
```

### 5. Activate

Reset the agent session (dashboard → Agents → Reset, or via API). The MCP server restarts with the agent and discovers the new skill.

## Using the Builder Agent

The builder agent receives specs as markdown files in its inbox and implements them on feature branches.

### Writing a Spec

Create a file in `agents/builder/inbox/`:

```markdown
---
type: build
target_branch: main
---

# Feature: User Preferences

## Summary
Add a user preferences system that persists settings to a JSON file.

## Requirements
1. Create `data/preferences.json` with default values
2. Add `preferences` tool to MCP server (get/set actions)
3. Primary agent can read and update preferences

## Acceptance Criteria
- [ ] Preferences persist across restarts
- [ ] Default values provided for new installations
```

### Triggering a Build

The dispatch adapter watches inbox directories. When it finds a spec:

1. Invokes `bin/invoke-builder.sh` with the spec path
2. Builder reads the spec, creates a feature branch, implements, and opens a PR
3. Cost is posted to #signals
4. Owner reviews and merges

### Using the Reviewer Agent

Send a spec and codebase for adversarial review:

```bash
bin/invoke-reviewer.sh --spec agents/builder/inbox/my-feature.md \
  --branch builder/my-feature --mode spec
```

The reviewer returns a verdict: APPROVE, REVISE, or REJECT.

## Inter-Agent Communication

Agents communicate via `bin/poke.sh`:

```bash
# Send a message to an agent
bin/poke.sh agent-name channel-name "Your message here"

# Send to the primary agent on #general
bin/poke.sh primary general "Status report please"
```

For file-based dispatch, drop files in `inbox/{agent-name}/`.

## Self-Modification

The system can modify itself through the builder agent:

1. Write a spec describing the change
2. Builder implements on a feature branch
3. Reviewer provides adversarial feedback
4. Owner merges the PR
5. Protected paths (Tier 1) block unauthorized changes to core files

### What Requires Restart

| Changed File | Restart Needed | How |
|-------------|---------------|-----|
| `persona/voice.md` | None | Loaded fresh each session |
| `skills/*/` | Agent session reset | Dashboard → Reset |
| `config/agents.json` | Agent server restart | POST `/restart/server` |
| `bin/agent-server.py` | Agent server restart | POST `/restart/server` |
| `Dockerfile` | Container rebuild | see [Local development build](#local-development-build) |

## Local Development Build

Production installs pull a prebuilt image from GHCR (`docker compose pull`).
If you are modifying the `Dockerfile` or Python/Node dependencies and need to
test those changes before a release, use the dev compose override:

```bash
docker compose \
  -f config/docker-compose.yml \
  -f config/docker-compose.dev.yml \
  up --build -d
```

`config/docker-compose.dev.yml` overlays `build: .` back onto the service so
your local changes are compiled into an image named `karakos-dev:local`.
Bring the stack back down and return to the prebuilt image at any time with:

```bash
docker compose down
docker compose -f config/docker-compose.yml up -d
```

The dev override is intentionally not committed to production flows — it is
only for contributors iterating on the image itself.

## Environment Variables

All configuration lives in `config/.env`. Key variables:

| Variable | Description |
|----------|-------------|
| *(Anthropic auth)* | Handled by `claude login` — no API key in env |
| `AGENT_SERVER_TOKEN` | Bearer token for API auth |
| `COST_DAILY_LIMIT` | Daily spend cap in USD |
| `COST_MONTHLY_LIMIT` | Monthly spend cap in USD |
| `MAX_CONCURRENT_BUILDERS` | Parallel builder agents |
| `MEMORY_DECAY_RATE` | Episode importance decay per pass (0-1), applied from `base_importance` |
| `MEMORY_CUTOFF` | Importance below which an episode is eligible to be dropped (default 6.0) |
| `MEMORY_PRUNE_GRACE_DAYS` | Days an episode is protected from pruning regardless of score, measured from `inserted_at`; default 7 |
| `MEMORY_SCORE_TIMEOUT` / `MEMORY_SCORE_RETRY_TIMEOUT` | Haiku importance-scoring call timeout, first attempt and retry; default 20s / 60s |
| `MESSAGE_RETENTION_DAYS` | JSONL log retention |
