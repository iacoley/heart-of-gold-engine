"""
Tests for config/.env.template — the documented set of environment
variables every install needs, regardless of how it's deployed. Split out
of tests/test_setup.py on 2026-08-18 when that file's setup.sh-wizard
tests were deleted (setup.sh is the Docker-era interactive installer;
this repo has run native systemd since 2026-08-11 and isn't
reinstalled through it — see native-migration-complete-2026-08-11 in
memory). .env.template itself is deploy-method-agnostic — every
native systemd unit still sources config/.env via EnvironmentFile= — so
its own content checks stayed, unlike the wizard-specific ones.
"""

from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent


class TestEnvTemplate:
    """Validate .env.template has required variables."""

    def setup_method(self):
        self.content = (PACKAGE_ROOT / "config" / ".env.template").read_text()

    # Merged 2026-09-05 (debloat pass): test_has_required_vars,
    # test_has_cost_limits, and test_has_session_secret used to be three
    # separate functions each asserting `"X" in self.content` for a
    # different variable name -- same check, no distinct behavior between
    # them. Parametrizing preserves every individual assertion (and gives
    # a clearer per-var failure report) with no coverage lost.
    @pytest.mark.parametrize("var", [
        "AGENT_SERVER_TOKEN",
        "OWNER_DISCORD_ID",
        "COST_DAILY_LIMIT",
        "COST_MONTHLY_LIMIT",
        "SESSION_SECRET",
    ])
    def test_has_required_var(self, var):
        assert var in self.content, f"Missing required env var: {var}"

    def test_no_filled_secrets(self):
        """Template should have placeholder values, not real secrets."""
        lines = self.content.strip().split("\n")
        for line in lines:
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                if "#" in value:
                    value = value[:value.index("#")]
                value = value.strip().strip('"').strip("'")
                if key.strip() in ("AGENT_SERVER_TOKEN", "SESSION_SECRET"):
                    assert not value or "..." in value or value.startswith("$") or value.startswith("<"), (
                        f"Template has non-placeholder value for {key.strip()}"
                    )
