"""
Shared pytest fixtures for Karakos test suite.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent

# Keep server-script logging out of the real production log files.
# bin/agent-server.py and bin/relay.py both point a RotatingFileHandler
# at WORKSPACE_ROOT/logs/*.log during their own module-level setup, and
# guard it against *repeat* imports (see the "Guard against duplicate
# handlers" comment in each) — but that guard only stops re-imports
# within a process from piling up handlers, it doesn't stop the first
# import of a real dev/prod WORKSPACE_ROOT from writing into the real
# log file. Set this before collection can trigger the first
# import_script() call anywhere in the suite, so the file handler binds
# to a throwaway directory for the whole test session instead. Real
# incident 2026-08-29: pytest test noise (a fake "TestAgent" rate-limit
# warning, fired dozens of times) ended up in the live production log
# and read like an active Discord outage.
os.environ.setdefault("KARAKOS_LOG_DIR", tempfile.mkdtemp(prefix="karakos-test-logs-"))


def import_script(name: str, file_path: Path = None):
    """Import a Python script by name, handling hyphens in filenames.

    Searches bin/ and system/ directories for the script.
    """
    module_name = name.replace("-", "_")

    if file_path is None:
        for search_dir in ["bin", "system", "mcp"]:
            candidate = PACKAGE_ROOT / search_dir / f"{name}.py"
            if candidate.exists():
                file_path = candidate
                break

    if file_path is None or not file_path.exists():
        raise FileNotFoundError(f"Script not found: {name}")

    # Scripts under bin/ do bare (non-package) intra-bin imports — e.g.
    # relay.py's `import banana`/`import context_box`, agent-server.py's
    # `import banana` — which only resolve when bin/ is actually on
    # sys.path, true when launched normally (see supervisord.conf) but
    # not when loaded by file path via spec_from_file_location like this.
    # Several test files used to work around this individually (see
    # test_attachments.py's `relay` fixture); centralized here so any
    # script loaded through import_script() gets the same fix, instead of
    # every new bare import breaking another batch of tests one file at a
    # time (agent-server.py's `import banana` broke 6 of them at once).
    bin_dir = str(PACKAGE_ROOT / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    # Don't cache in sys.modules — allows reload with different env
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace with expected directory structure."""
    dirs = [
        "data/messages",
        "data/memory",
        "data/health",
        "logs/agent-streams",
        "logs/session-summaries",
        "logs/git-events",
        "config",
        "mcp",
        "bin",
        "agents/templates",
        "inbox",
    ]
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    # Create minimal agents config
    agents_config = {
        "agents": {
            "test-agent": {
                "system_prompt": "agents/test-agent/SYSTEM_PROMPT.md",
                "discord_bot_token_env": "DISCORD_BOT_TOKEN_TEST",
            }
        }
    }
    (tmp_path / "config" / "agents.json").write_text(json.dumps(agents_config))

    # Create minimal channels config
    channels_config = {
        "channels": {
            "general": "123456789",
            "signals": "987654321",
        }
    }
    (tmp_path / "config" / "channels.json").write_text(json.dumps(channels_config))

    return tmp_path


@pytest.fixture
def protected_paths_config(tmp_workspace):
    """Create protected paths config for testing."""
    config = {
        "tier1_protected": [
            "system/",
            "config/",
            "bin/agent-server.py",
            "bin/relay.py",
            "Dockerfile",
        ],
        "tier2_review_required": [
            "bin/",
            "agents/templates/",
        ],
        "unprotected_overrides": [
            "agents/*/persona/",
            "agents/*/journal/",
        ],
    }
    config_path = tmp_workspace / "config" / "protected-paths.json"
    config_path.write_text(json.dumps(config))
    return config

# memory_db fixture removed 2026-09-05 (debloat pass): it hand-rolled a
# second copy of the episodes/facts/patterns schema instead of using the
# real bin/memory-maintenance.py init_db(), so nothing built on it ever
# exercised real app code. Its only caller (test_memory.py) was rewritten
# to use import_script("memory-maintenance") + mm.init_db() directly,
# matching the pattern TestMemoryDatabaseInit already used correctly.
