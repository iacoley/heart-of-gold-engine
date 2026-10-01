"""
Keeps .github/CODEOWNERS in sync with config/protected-paths.json.

CODEOWNERS (plus the "Require review from Code Owners" branch-protection
setting) is what actually enforces Tier 1. If a path is added to
tier1_protected without a matching CODEOWNERS entry, nothing enforces it.
"""

import json
from pathlib import Path

from conftest import PACKAGE_ROOT

ROOT = Path(PACKAGE_ROOT)
OWNER = "@iacoley"


def _codeowners_entries():
    entries = {}
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        entries[pattern.lstrip("/")] = owners
    return entries


def _tier1():
    cfg = json.loads((ROOT / "config" / "protected-paths.json").read_text())
    return cfg["tier1_protected"]


def test_every_tier1_path_is_in_codeowners():
    entries = _codeowners_entries()
    missing = [p for p in _tier1() if p not in entries]
    assert not missing, (
        f"Tier 1 paths missing from .github/CODEOWNERS: {missing}. "
        f"Add `/{{path}} {OWNER}` for each."
    )


def test_tier1_codeowners_entries_name_the_owner():
    entries = _codeowners_entries()
    wrong = [p for p in _tier1() if OWNER not in entries.get(p, [])]
    assert not wrong, f"Tier 1 CODEOWNERS entries not owned by {OWNER}: {wrong}"


def test_tier2_entries_exist_on_disk():
    cfg = json.loads((ROOT / "config" / "protected-paths.json").read_text())
    gone = [p for p in cfg["tier2_review_required"] if not (ROOT / p).exists()]
    assert not gone, f"Tier 2 paths that do not exist: {gone}"
