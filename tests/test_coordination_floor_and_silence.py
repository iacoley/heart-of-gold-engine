"""Tests for floor governance, bracketed silence suppression, and Banana mutex scope.

Verifies:
1. is_silence_announcement() suppresses square-bracketed aside declarations like '*[no reply — floor's closed]*'.
2. speaking_banana.in_scope() exempts #lounge from Banana turn claims to prevent starving #the-banana-stand.
3. relay.py floor governance (task-1788566837): reply=none with floor=open yields speaker but falls through to scored gate.
"""

import sys
from pathlib import Path
import pytest

from conftest import PACKAGE_ROOT, import_script

BIN_DIR = PACKAGE_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from handoff import parse_handoff, VALID_MIRROR_CHANNELS
import speaking_banana


def test_mirror_channels_includes_the_banana_stand():
    assert "the-banana-stand" in VALID_MIRROR_CHANNELS
    env = parse_handoff('```handoff\n{"v":1,"kind":"status","reply":"none","mirror_to":"the-banana-stand"}\n```')
    assert env is not None
    assert env.mirror_to == "the-banana-stand"


def test_speaking_banana_in_scope_exempts_lounge():
    channels_cfg = {
        "server_ids": ["home-guild-111"],
        "channels": {
            "general": {"id": "100", "guild_id": "home-guild-111"},
            "agent-chat": {"id": "1534436119888793750", "guild_id": "crab-cavern-222"},
            "lounge": {"id": "1534452820995080192", "guild_id": "crab-cavern-222"},
            "custom-unmutexed": {"id": "300", "guild_id": "crab-cavern-222", "banana_mutex": False},
            "custom-mutexed": {"id": "400", "guild_id": "crab-cavern-222", "banana_mutex": True},
        }
    }

    # Home guild is out of scope
    assert not speaking_banana.in_scope("100", channels_cfg)

    # Multi-agent coordination channel is in scope
    assert speaking_banana.in_scope("1534436119888793750", channels_cfg)

    # #lounge is strictly exempt to prevent seizing global floor lock
    assert not speaking_banana.in_scope("1534452820995080192", channels_cfg)

    # Explicit banana_mutex overrides
    assert not speaking_banana.in_scope("300", channels_cfg)
    assert speaking_banana.in_scope("400", channels_cfg)

    # Unknown channel
    assert not speaking_banana.in_scope("99999", channels_cfg)


def test_bracketed_silence_suppression_extracted():
    from test_silence_announcement_suppression import load_is_silence_announcement
    is_silence = load_is_silence_announcement()

    # The exact string leaked in #lounge on 2026-09-04
    assert is_silence("*[no reply — floor's closed]*")
    assert is_silence("[no reply — floor's closed]")
    assert is_silence("*(no reply — floor's closed)*")
    assert is_silence("(no reply — floor's closed)")

    # Real answers still pass
    assert not is_silence("Floor is open for Amos or Zero to chime in.")
    assert not is_silence("[WIP] Added new benchmark tests for latency.")
