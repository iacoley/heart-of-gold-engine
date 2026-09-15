#!/usr/bin/env bash
# check-hf-offline.sh — smoke-load assertion: confirms HF_HUB_OFFLINE=1 is
# actually honored by huggingface_hub in a given Python interpreter.
#
# Why this exists (task-1788562253, 2026-09-14): no skill or core script
# here calls huggingface_hub directly today, but it's present as a
# dormant transitive dep (fastembed -> huggingface_hub, used live by
# memory-maintenance.py/memory-dedup.py/voice_presence.py for the
# BAAI/bge-small-en-v1.5 embedding model) and per-skill venvs are the
# documented pattern for any future ML-heavy skill (see skills/README.md,
# "Third-Party Dependencies"). Grep-based checks for from_pretrained()/
# snapshot_download() calls in a skill's own code won't catch a fetch a
# *dependency* makes internally. Amos hit this on his side and tested his
# own voice-transcribe skill (faster_whisper) clean offline with this
# exact pattern — same fix, adopted here before, not after, something
# actually needs it.
#
# Usage:
#   bin/check-hf-offline.sh [path-to-python3]
#
# Defaults to this repo's own .venv. Point it at a skill's own
# skills/<name>/.venv/bin/python3 after building one, if that skill pulls
# in huggingface_hub directly or transitively (transformers,
# sentence-transformers, faster-whisper, etc.) — run it once as part of
# standing the venv up, same spirit as a build step's own smoke test.
#
# Exit 0 if huggingface_hub isn't importable in the given interpreter at
# all (nothing to check) or if it's present and correctly blocks a fetch
# under offline mode. Exit 1 only if huggingface_hub is present and
# offline mode did NOT block the fetch — that's the real regression this
# guards against (a library update that silently stops honoring the
# env var, or a skill that shells out to something bypassing it).
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/opt/karakos}"
PYTHON="${1:-$WORKSPACE_ROOT/.venv/bin/python3}"

if [[ ! -x "$PYTHON" ]]; then
    echo "check-hf-offline.sh: interpreter not found or not executable: $PYTHON" >&2
    exit 1
fi

HF_HUB_OFFLINE=1 "$PYTHON" - <<'PYEOF'
import sys

try:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import LocalEntryNotFoundError
except ImportError:
    print("check-hf-offline: huggingface_hub not importable here, nothing to check")
    sys.exit(0)

try:
    hf_hub_download(
        repo_id="karakos-offline-smoke-test/does-not-exist",
        filename="config.json",
    )
except LocalEntryNotFoundError:
    print("check-hf-offline: OK -- HF_HUB_OFFLINE=1 blocked the fetch as expected")
    sys.exit(0)
except Exception as e:
    print(
        "check-hf-offline: FAIL -- unexpected exception under offline mode: "
        f"{type(e).__name__}: {e}"
    )
    sys.exit(1)
else:
    print("check-hf-offline: FAIL -- fetch did not raise at all under offline mode")
    sys.exit(1)
PYEOF
