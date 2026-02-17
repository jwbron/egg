"""Tests for get_role_definition with complexity_tier (Tier 3).

Covers:
- Integrator role gets expanded write access with complexity_tier='high'
- Integrator role retains standard access with other tiers
- Non-integrator roles are unaffected by complexity_tier
- Tier 3 integrator blocks .egg-state/contracts/
- Tier 3 integrator has expanded responsibilities
"""

from __future__ import annotations

from egg_contracts.agent_roles import (
    AGENT_ROLES,
    AgentRole,
    get_role_definition,
)


class TestGetRoleDefinitionDefault:
    """Tests for get_role_definition without complexity_tier."""

    def test_integrator_default_returns_standard(self):
        """Integrator without complexity_tier returns standard definition."""
        role_def = get_role_definition(AgentRole.INTEGRATOR)
        assert role_def is AGENT_ROLES[AgentRole.INTEGRATOR]

    def test_integrator_none_tier_returns_standard(self):
        """Integrator with complexity_tier=None returns standard definition."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier=None)
        assert role_def is AGENT_ROLES[AgentRole.INTEGRATOR]

    def test_integrator_mid_tier_returns_standard(self):
        """Integrator with complexity_tier='mid' returns standard definition."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="mid")
        assert role_def is AGENT_ROLES[AgentRole.INTEGRATOR]

    def test_integrator_low_tier_returns_standard(self):
        """Integrator with complexity_tier='low' returns standard definition."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="low")
        assert role_def is AGENT_ROLES[AgentRole.INTEGRATOR]

    def test_string_role_works(self):
        """String role name works correctly."""
        role_def = get_role_definition("integrator")
        assert role_def is AGENT_ROLES[AgentRole.INTEGRATOR]


class TestGetRoleDefinitionTier3Integrator:
    """Tests for integrator role with complexity_tier='high'."""

    def test_returns_different_object(self):
        """Tier 3 integrator returns a new definition, not the cached one."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert role_def is not AGENT_ROLES[AgentRole.INTEGRATOR]

    def test_description_mentions_tier3(self):
        """Tier 3 integrator description mentions Tier 3."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert "Tier 3" in role_def.description

    def test_expanded_responsibilities(self):
        """Tier 3 integrator has additional responsibilities."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        responsibilities = role_def.responsibilities
        assert "Fix integration issues across phase boundaries" in responsibilities
        assert "Resolve merge conflicts between phase implementations" in responsibilities
        assert "Ensure all tests pass end-to-end" in responsibilities

    def test_expanded_write_access_source(self):
        """Tier 3 integrator can write to source directories."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        allowed_write = role_def.file_access.allowed_write
        assert "src/" in allowed_write
        assert "lib/" in allowed_write
        assert "shared/" in allowed_write
        assert "gateway/" in allowed_write
        assert "orchestrator/" in allowed_write

    def test_expanded_write_access_tests(self):
        """Tier 3 integrator can write to test directories."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        allowed_write = role_def.file_access.allowed_write
        assert "tests/" in allowed_write
        assert "test/" in allowed_write
        assert "integration_tests/" in allowed_write

    def test_expanded_write_access_docs(self):
        """Tier 3 integrator can write to docs directory."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert "docs/" in role_def.file_access.allowed_write

    def test_expanded_write_access_agent_outputs(self):
        """Tier 3 integrator can write to agent-outputs."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert ".egg-state/agent-outputs/" in role_def.file_access.allowed_write

    def test_blocked_from_contracts(self):
        """Tier 3 integrator is still blocked from contracts."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert ".egg-state/contracts/" in role_def.file_access.blocked_write

    def test_preserves_dependencies(self):
        """Tier 3 integrator preserves dependencies from base role."""
        base_def = AGENT_ROLES[AgentRole.INTEGRATOR]
        tier3_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert tier3_def.dependencies == base_def.dependencies

    def test_preserves_parallel_flag(self):
        """Tier 3 integrator preserves can_run_in_parallel from base role."""
        base_def = AGENT_ROLES[AgentRole.INTEGRATOR]
        tier3_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert tier3_def.can_run_in_parallel == base_def.can_run_in_parallel

    def test_preserves_role_enum(self):
        """Tier 3 integrator preserves the role enum value."""
        role_def = get_role_definition(AgentRole.INTEGRATOR, complexity_tier="high")
        assert role_def.role == AgentRole.INTEGRATOR

    def test_string_role_with_high_tier(self):
        """String 'integrator' with complexity_tier='high' returns Tier 3 def."""
        role_def = get_role_definition("integrator", complexity_tier="high")
        assert "Tier 3" in role_def.description


class TestOtherRolesUnaffected:
    """Tests that non-integrator roles ignore complexity_tier."""

    def test_coder_unaffected_by_high_tier(self):
        """Coder role is not affected by complexity_tier='high'."""
        default = get_role_definition(AgentRole.CODER)
        high = get_role_definition(AgentRole.CODER, complexity_tier="high")
        assert default is high

    def test_tester_unaffected_by_high_tier(self):
        """Tester role is not affected by complexity_tier='high'."""
        default = get_role_definition(AgentRole.TESTER)
        high = get_role_definition(AgentRole.TESTER, complexity_tier="high")
        assert default is high

    def test_documenter_unaffected_by_high_tier(self):
        """Documenter role is not affected by complexity_tier='high'."""
        default = get_role_definition(AgentRole.DOCUMENTER)
        high = get_role_definition(AgentRole.DOCUMENTER, complexity_tier="high")
        assert default is high

    def test_reviewer_code_unaffected_by_high_tier(self):
        """Reviewer code role is not affected by complexity_tier='high'."""
        default = get_role_definition(AgentRole.REVIEWER_CODE)
        high = get_role_definition(AgentRole.REVIEWER_CODE, complexity_tier="high")
        assert default is high

    def test_reviewer_contract_unaffected_by_high_tier(self):
        """Reviewer contract role is not affected by complexity_tier='high'."""
        default = get_role_definition(AgentRole.REVIEWER_CONTRACT)
        high = get_role_definition(AgentRole.REVIEWER_CONTRACT, complexity_tier="high")
        assert default is high
