"""Tests for get_role_definition with complexity_tier.

Covers:
- Roles are unaffected by complexity_tier
"""

from __future__ import annotations

from egg_contracts.agent_roles import (
    AgentRole,
    get_role_definition,
)


class TestOtherRolesUnaffected:
    """Tests that roles ignore complexity_tier."""

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
