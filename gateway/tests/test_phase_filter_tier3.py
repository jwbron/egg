"""Tests for check_agent_restrictions with complexity_tier (Tier 3).

Covers:
- check_agent_restrictions passes complexity_tier to validate_agent_push
- Integrator with high tier can write source/tests/docs
- Integrator with high tier is blocked from contracts
- Integrator without high tier is blocked from source
- Non-integrator roles are unaffected by complexity_tier
"""

import pytest
from phase_filter import FileRestrictionResult, check_agent_restrictions


class TestCheckAgentRestrictionsTier3:
    """Tests for check_agent_restrictions with complexity_tier parameter."""

    def test_integrator_high_allows_source_files(self):
        """Integrator with high tier can modify source files."""
        result = check_agent_restrictions(
            "integrator",
            ["src/main.py", "shared/models.py"],
            complexity_tier="high",
        )
        assert result.allowed is True

    def test_integrator_high_allows_test_files(self):
        """Integrator with high tier can modify test files."""
        result = check_agent_restrictions(
            "integrator",
            ["tests/test_main.py", "test/test_utils.py"],
            complexity_tier="high",
        )
        assert result.allowed is True

    def test_integrator_high_allows_docs(self):
        """Integrator with high tier can modify docs."""
        result = check_agent_restrictions(
            "integrator",
            ["docs/guide.md", "docs/architecture/overview.md"],
            complexity_tier="high",
        )
        assert result.allowed is True

    def test_integrator_high_allows_mixed_files(self):
        """Integrator with high tier can modify mix of source, tests, docs."""
        result = check_agent_restrictions(
            "integrator",
            ["src/app.py", "tests/test_app.py", "docs/README.md", "gateway/api.py"],
            complexity_tier="high",
        )
        assert result.allowed is True

    def test_integrator_high_blocks_contracts(self):
        """Integrator with high tier is still blocked from contracts."""
        result = check_agent_restrictions(
            "integrator",
            [".egg-state/contracts/contract.json"],
            complexity_tier="high",
        )
        assert result.allowed is False

    def test_integrator_high_blocks_github(self):
        """Integrator with high tier is blocked from .github."""
        result = check_agent_restrictions(
            "integrator",
            [".github/workflows/ci.yml"],
            complexity_tier="high",
        )
        assert result.allowed is False

    def test_integrator_default_blocks_source(self):
        """Integrator without tier is blocked from source files."""
        result = check_agent_restrictions(
            "integrator",
            ["src/main.py"],
        )
        assert result.allowed is False

    def test_integrator_mid_blocks_source(self):
        """Integrator with mid tier is blocked from source files."""
        result = check_agent_restrictions(
            "integrator",
            ["src/main.py"],
            complexity_tier="mid",
        )
        assert result.allowed is False

    def test_integrator_low_blocks_source(self):
        """Integrator with low tier is blocked from source files."""
        result = check_agent_restrictions(
            "integrator",
            ["src/main.py"],
            complexity_tier="low",
        )
        assert result.allowed is False

    def test_integrator_allows_agent_outputs_all_tiers(self):
        """Integrator can write agent-outputs regardless of tier."""
        for tier in [None, "low", "mid", "high"]:
            result = check_agent_restrictions(
                "integrator",
                [".egg-state/agent-outputs/handoff.json"],
                complexity_tier=tier,
            )
            assert result.allowed is True, f"Failed for tier={tier}"


class TestOtherRolesUnaffectedByTier:
    """Tests that non-integrator roles ignore complexity_tier."""

    def test_coder_unaffected_by_high_tier(self):
        """Coder role behavior unchanged with complexity_tier='high'."""
        result_default = check_agent_restrictions("coder", ["src/main.py"])
        result_high = check_agent_restrictions(
            "coder", ["src/main.py"], complexity_tier="high"
        )
        assert result_default.allowed == result_high.allowed

    def test_tester_unaffected_by_high_tier(self):
        """Tester role behavior unchanged with complexity_tier='high'."""
        result_default = check_agent_restrictions(
            "tester", ["tests/test_main.py"]
        )
        result_high = check_agent_restrictions(
            "tester", ["tests/test_main.py"], complexity_tier="high"
        )
        assert result_default.allowed == result_high.allowed

    def test_documenter_unaffected_by_high_tier(self):
        """Documenter role behavior unchanged with complexity_tier='high'."""
        result_default = check_agent_restrictions("documenter", ["docs/guide.md"])
        result_high = check_agent_restrictions(
            "documenter", ["docs/guide.md"], complexity_tier="high"
        )
        assert result_default.allowed == result_high.allowed


class TestCheckAgentRestrictionsBlockResult:
    """Tests for FileRestrictionResult details on blocked files."""

    def test_blocked_result_has_role(self):
        """Blocked result includes the role that was checked."""
        result = check_agent_restrictions(
            "integrator",
            ["src/main.py"],
            complexity_tier="mid",
        )
        assert result.allowed is False
        assert result.role == "integrator"

    def test_blocked_result_has_blocked_files(self):
        """Blocked result includes the specific files that were blocked."""
        result = check_agent_restrictions(
            "integrator",
            ["src/main.py", ".egg-state/agent-outputs/out.json"],
            complexity_tier="mid",
        )
        assert result.allowed is False
        assert "src/main.py" in result.blocked_files

    def test_allowed_result_has_no_blocked_files(self):
        """Allowed result has no blocked files."""
        result = check_agent_restrictions(
            "integrator",
            [".egg-state/agent-outputs/out.json"],
            complexity_tier="mid",
        )
        assert result.allowed is True
