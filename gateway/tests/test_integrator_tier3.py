"""Tests for Tier 3 integrator expanded write access.

Covers:
- get_agent_pattern returns INTEGRATOR_TIER3_PATTERNS when complexity_tier='high'
- Integrator Tier 3 can write to source, tests, docs directories
- Integrator Tier 3 is still blocked from .egg-state/contracts/
- Default integrator (no tier or mid tier) cannot write source
- check_agent_file_access passes complexity_tier through
- validate_agent_push passes complexity_tier through
"""

from agent_restrictions import (
    INTEGRATOR_PATTERNS,
    INTEGRATOR_TIER3_PATTERNS,
    AgentRole,
    check_agent_file_access,
    get_agent_pattern,
    validate_agent_push,
)


class TestGetAgentPatternTier3:
    """Tests for get_agent_pattern with complexity_tier."""

    def test_integrator_default_returns_standard(self):
        """Integrator without complexity_tier returns standard patterns."""
        pattern = get_agent_pattern(AgentRole.INTEGRATOR)
        assert pattern is INTEGRATOR_PATTERNS

    def test_integrator_mid_returns_standard(self):
        """Integrator with mid complexity_tier returns standard patterns."""
        pattern = get_agent_pattern(AgentRole.INTEGRATOR, complexity_tier="mid")
        assert pattern is INTEGRATOR_PATTERNS

    def test_integrator_low_returns_standard(self):
        """Integrator with low complexity_tier returns standard patterns."""
        pattern = get_agent_pattern(AgentRole.INTEGRATOR, complexity_tier="low")
        assert pattern is INTEGRATOR_PATTERNS

    def test_integrator_high_returns_tier3(self):
        """Integrator with high complexity_tier returns Tier 3 patterns."""
        pattern = get_agent_pattern(AgentRole.INTEGRATOR, complexity_tier="high")
        assert pattern is INTEGRATOR_TIER3_PATTERNS

    def test_coder_ignores_complexity_tier(self):
        """Coder role is not affected by complexity_tier."""
        pattern_default = get_agent_pattern(AgentRole.CODER)
        pattern_high = get_agent_pattern(AgentRole.CODER, complexity_tier="high")
        assert pattern_default is pattern_high

    def test_tester_ignores_complexity_tier(self):
        """Tester role is not affected by complexity_tier."""
        pattern_default = get_agent_pattern(AgentRole.TESTER)
        pattern_high = get_agent_pattern(AgentRole.TESTER, complexity_tier="high")
        assert pattern_default is pattern_high


class TestIntegratorTier3WriteAccess:
    """Tests for INTEGRATOR_TIER3_PATTERNS file access."""

    def test_can_write_source(self):
        """Tier 3 integrator can write to src/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("src/main.py") is True

    def test_can_write_lib(self):
        """Tier 3 integrator can write to lib/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("lib/utils.py") is True

    def test_can_write_shared(self):
        """Tier 3 integrator can write to shared/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("shared/models.py") is True

    def test_can_write_gateway(self):
        """Tier 3 integrator can write to gateway/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("gateway/api.py") is True

    def test_can_write_tests(self):
        """Tier 3 integrator can write to tests/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("tests/test_main.py") is True

    def test_can_write_test(self):
        """Tier 3 integrator can write to test/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("test/test_main.py") is True

    def test_can_write_docs(self):
        """Tier 3 integrator can write to docs/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("docs/guide.md") is True

    def test_can_write_orchestrator(self):
        """Tier 3 integrator can write to orchestrator/ directory."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write("orchestrator/models.py") is True

    def test_can_write_agent_outputs(self):
        """Tier 3 integrator can write to agent-outputs."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write(".egg-state/agent-outputs/handoff.json") is True

    def test_blocked_from_contracts(self):
        """Tier 3 integrator is still blocked from contracts."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write(".egg-state/contracts/contract.json") is False

    def test_blocked_from_github(self):
        """Tier 3 integrator is blocked from .github/."""
        assert INTEGRATOR_TIER3_PATTERNS.can_write(".github/workflows/ci.yml") is False


class TestDefaultIntegratorBlocked:
    """Tests that default integrator CANNOT write source (for contrast)."""

    def test_cannot_write_source(self):
        """Default integrator cannot write to src/."""
        assert INTEGRATOR_PATTERNS.can_write("src/main.py") is False

    def test_cannot_write_tests(self):
        """Default integrator cannot write to tests/."""
        assert INTEGRATOR_PATTERNS.can_write("tests/test_main.py") is False

    def test_cannot_write_docs(self):
        """Default integrator cannot write to docs/."""
        assert INTEGRATOR_PATTERNS.can_write("docs/guide.md") is False

    def test_can_write_agent_outputs(self):
        """Default integrator can write to agent-outputs."""
        assert INTEGRATOR_PATTERNS.can_write(".egg-state/agent-outputs/handoff.json") is True


class TestCheckAgentFileAccessTier3:
    """Tests for check_agent_file_access with complexity_tier."""

    def test_integrator_high_allows_source(self):
        """check_agent_file_access allows integrator source write with high tier."""
        allowed, blocked, reason = check_agent_file_access(
            AgentRole.INTEGRATOR,
            ["src/main.py", "tests/test_main.py"],
            complexity_tier="high",
        )
        assert allowed is True
        assert blocked == []

    def test_integrator_mid_blocks_source(self):
        """check_agent_file_access blocks integrator source write with mid tier."""
        allowed, blocked, reason = check_agent_file_access(
            AgentRole.INTEGRATOR,
            ["src/main.py"],
            complexity_tier="mid",
        )
        assert allowed is False
        assert "src/main.py" in blocked

    def test_integrator_high_still_blocks_contracts(self):
        """check_agent_file_access blocks contracts even with high tier."""
        allowed, blocked, reason = check_agent_file_access(
            AgentRole.INTEGRATOR,
            [".egg-state/contracts/contract.json"],
            complexity_tier="high",
        )
        assert allowed is False


class TestValidateAgentPushTier3:
    """Tests for validate_agent_push with complexity_tier."""

    def test_integrator_high_push_allowed(self):
        """validate_agent_push allows integrator push with high tier."""
        result = validate_agent_push(
            AgentRole.INTEGRATOR,
            ["src/app.py", "shared/models.py", "docs/README.md"],
            complexity_tier="high",
        )
        assert result.allowed is True

    def test_integrator_default_push_blocked(self):
        """validate_agent_push blocks integrator push without tier."""
        result = validate_agent_push(
            AgentRole.INTEGRATOR,
            ["src/app.py"],
        )
        assert result.allowed is False
        assert "src/app.py" in result.blocked_files

    def test_integrator_high_push_blocked_contracts(self):
        """validate_agent_push blocks contracts push even with high tier."""
        result = validate_agent_push(
            AgentRole.INTEGRATOR,
            [".egg-state/contracts/contract.json"],
            complexity_tier="high",
        )
        assert result.allowed is False
