"""
Tests for peer-targeted mention filtering in reply_gate.py and relay.py.
Ensures messages explicitly directed to other bots/users (e.g. '@Zero ...',
'@Amos ...') do not wake Marvin or invoke Tier 2 Haiku scoring.
"""

from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).parent.parent
bin_dir = str(PACKAGE_ROOT / "bin")
if bin_dir not in sys.path:
    sys.path.insert(0, bin_dir)

from reply_gate import Decision, GateMessage, ReplyGate
from handoff import Envelope, parse_handoff, required_but_misdirected


def test_peer_mention_drops_immediately_without_scoring():
    gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",), cooldown_sec=300)
    msg = GateMessage(
        channel_id="c_agent_chat",
        author_id="user_ryan",
        content="<@1542285964213358633> do you have access to Ian's heart of gold repo",
        mentions_self=False,
        mentions_other=True,
    )
    decision = gate.evaluate(msg)
    assert not decision.wake, "Must not wake when message is targeted at another bot"
    assert not decision.needs_score, "Must skip Tier 2 scorer entirely for peer pings"
    assert decision.tier == "tier1-peer"


def test_dual_mention_wakes_marvin():
    gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",), cooldown_sec=300)
    msg = GateMessage(
        channel_id="c_agent_chat",
        author_id="user_ryan",
        content="@Zero check X. @Marvin what do you think of Y?",
        mentions_self=True,
        mentions_other=True,
    )
    decision = gate.evaluate(msg)
    assert decision.wake, "Must wake when Marvin is explicitly co-addressed"
    assert decision.tier == "tier1"


def test_role_mention_with_peer_mention_wakes_marvin():
    gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",), cooldown_sec=300)
    msg = GateMessage(
        channel_id="c_agent_chat",
        author_id="user_ryan",
        content="@robots @Zero check X",
        mentions_self=False,
        mentions_role=True,
        mentions_other=True,
    )
    decision = gate.evaluate(msg)
    assert decision.wake, "Must wake when shared role is pinged"
    assert decision.tier == "tier1"


def test_reply_to_self_with_peer_mention_wakes_marvin():
    gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",), cooldown_sec=300)
    msg = GateMessage(
        channel_id="c_agent_chat",
        author_id="user_ryan",
        content="also cc @Zero",
        mentions_self=False,
        mentions_other=True,
        is_reply_to_self=True,
    )
    decision = gate.evaluate(msg)
    assert decision.wake, "Must wake when message is a reply to Marvin, even if tagging a peer"
    assert decision.tier == "tier1"
    assert decision.reason == "reply to you"


def test_peer_mention_with_marvin_named_in_prose_falls_through_to_scorer():
    # Verify both default clock and low-uptime system clock (fresh CI runner where uptime < cooldown_sec)
    for clock_fn in (None, lambda: 10.0):
        kwargs = {"clock": clock_fn} if clock_fn else {}
        gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",), cooldown_sec=300, **kwargs)
        msg = GateMessage(
            channel_id="c_agent_chat",
            author_id="user_ryan",
            content="@Zero check X. Marvin, what do you think?",
            mentions_self=False,
            mentions_other=True,
        )
        decision = gate.evaluate(msg)
        assert decision.needs_score, f"Must fall through to scorer when Marvin is named in prose (clock={clock_fn})"
        assert decision.tier == "tier2", f"Expected tier2, got {decision.tier}"
        assert decision.named is True


def test_misdirected_required_handoff_declines_cleanly():
    env = Envelope(v=1, 
        kind="question",
        reply="required",
        reply_from="Amos",
        subject="banana-mutex",
    )
    assert required_but_misdirected(env, "Marvin") is True
    assert required_but_misdirected(env, "Amos") is False


def test_handoff_envelope_floor_attribute():
    content_with_floor = """```handoff
{
  "v": 1,
  "kind": "status",
  "reply": "required",
  "reply_from": "Amos",
  "floor": "open",
  "subject": "floor-test"
}
```"""
    env = parse_handoff(content_with_floor)
    assert env is not None
    assert env.floor == "open", f"Expected floor == 'open', got {env.floor!r}"
    assert required_but_misdirected(env, "Marvin") is True

    # Floor attribute access on Envelope with default None
    env_plain = Envelope(v=1, kind="status", reply="optional")
    assert env_plain.floor is None
    assert getattr(env_plain, "floor", None) is None


if __name__ == "__main__":
    test_peer_mention_drops_immediately_without_scoring()
    test_dual_mention_wakes_marvin()
    test_role_mention_with_peer_mention_wakes_marvin()
    test_reply_to_self_with_peer_mention_wakes_marvin()
    test_peer_mention_with_marvin_named_in_prose_falls_through_to_scorer()
    test_misdirected_required_handoff_declines_cleanly()
    test_handoff_envelope_floor_attribute()
    print("All peer filtering tests passed!")


def test_fresh_gate_is_not_in_cooldown_on_a_small_monotonic_clock():
    """Regression: a gate that has never woken must never report a cooldown.

    time.monotonic()'s epoch is arbitrary (uptime on Linux). The old 0.0
    sentinel only looked correct because monotonic() is large on a long-lived
    machine. On a CI runner 63 seconds into its life, a brand-new ReplyGate
    reported 'cooldown, 237s left' and skipped the scorer entirely.
    """
    gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",),
                     cooldown_sec=300, clock=lambda: 63.0)
    msg = GateMessage(
        channel_id="c_fresh",
        author_id="user_ryan",
        content="@Zero - Marvin, is your gate still dropping envelopes?",
        mentions_self=False,
        mentions_other=True,
    )
    decision = gate.evaluate(msg)
    assert decision.tier != "cooldown", (
        f"A gate with no recorded wake must not be in cooldown, got {decision!r}"
    )
    assert decision.needs_score, "Must reach the Tier 2 scorer"


def test_cooldown_still_applies_after_a_real_wake():
    """The sentinel fix must not disable the cooldown it guards."""
    clock = {"t": 63.0}
    gate = ReplyGate(self_id="marvin_bot_id", names=("marvin",),
                     cooldown_sec=300, clock=lambda: clock["t"])
    gate._last_tier2_wake["c_fresh"] = clock["t"]
    msg = GateMessage(
        channel_id="c_fresh",
        author_id="user_ryan",
        content="@Zero - Marvin, thoughts on the gate?",
        mentions_self=False,
        mentions_other=True,
    )
    clock["t"] += 10.0
    decision = gate.evaluate(msg)
    assert decision.tier == "cooldown", f"Expected cooldown, got {decision!r}"
    assert not decision.needs_score
