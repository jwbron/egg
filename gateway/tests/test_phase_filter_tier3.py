"""Tests for check_agent_restrictions with complexity_tier (Tier 3).

Covers:
- check_agent_restrictions passes complexity_tier to validate_agent_push
- Non-producer roles are unaffected by complexity_tier
"""

from phase_filter import check_agent_restrictions


class TestOtherRolesUnaffectedByTier:
    """Tests that non-producer roles ignore complexity_tier."""

    def test_coder_unaffected_by_high_tier(self):
        """Coder role behavior unchanged with complexity_tier='high'."""
        result_default = check_agent_restrictions("coder", ["src/main.py"])
        result_high = check_agent_restrictions("coder", ["src/main.py"], complexity_tier="high")
        assert result_default.allowed == result_high.allowed

    def test_tester_unaffected_by_high_tier(self):
        """Tester role behavior unchanged with complexity_tier='high'."""
        result_default = check_agent_restrictions("tester", ["tests/test_main.py"])
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
            "coder",
            ["tests/test_main.py"],
        )
        assert result.allowed is False
        assert result.role == "coder"

    def test_blocked_result_has_blocked_files(self):
        """Blocked result includes the specific files that were blocked."""
        result = check_agent_restrictions(
            "coder",
            ["tests/test_main.py", "gateway/app.py"],
        )
        assert result.allowed is False
        assert "tests/test_main.py" in result.blocked_files

    def test_allowed_result_has_no_blocked_files(self):
        """Allowed result has no blocked files."""
        result = check_agent_restrictions(
            "coder",
            ["gateway/app.py"],
        )
        assert result.allowed is True
