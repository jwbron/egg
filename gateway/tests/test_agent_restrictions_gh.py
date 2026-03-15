"""Tests for agent role-based GitHub operation restrictions.

Tests cover:
- AgentGHRestriction.is_blocked() matching logic
- check_agent_gh_operation() for all pipeline roles
- Backward compatibility for unknown/empty roles
"""

from agent_restrictions import (
    AGENT_GH_RESTRICTIONS,
    AgentGHRestriction,
    AgentRole,
    check_agent_gh_operation,
)


class TestAgentGHRestriction:
    """Tests for AgentGHRestriction dataclass."""

    def test_is_blocked_prefix_match(self):
        """Wildcard pattern blocks commands with any suffix."""
        restriction = AgentGHRestriction(
            role="test",
            blocked_operations=["issue comment *"],
        )
        assert restriction.is_blocked("issue comment 123") is True
        assert restriction.is_blocked("issue comment 456 --body hello") is True

    def test_is_blocked_exact_match(self):
        """Exact pattern blocks only exact command."""
        restriction = AgentGHRestriction(
            role="test",
            blocked_operations=["issue list"],
        )
        assert restriction.is_blocked("issue list") is True
        assert restriction.is_blocked("issue list 123") is False

    def test_is_blocked_case_insensitive(self):
        """Matching is case-insensitive."""
        restriction = AgentGHRestriction(
            role="test",
            blocked_operations=["issue comment *"],
        )
        assert restriction.is_blocked("Issue Comment 123") is True
        assert restriction.is_blocked("ISSUE COMMENT 456") is True

    def test_not_blocked_different_command(self):
        """Non-matching commands are not blocked."""
        restriction = AgentGHRestriction(
            role="test",
            blocked_operations=["issue comment *", "issue edit *"],
        )
        assert restriction.is_blocked("pr view 123") is False
        assert restriction.is_blocked("pr create --title test") is False
        assert restriction.is_blocked("issue view 123") is False
        assert restriction.is_blocked("issue list") is False


class TestAgentGHRestrictionsRegistry:
    """Tests for the AGENT_GH_RESTRICTIONS registry."""

    def test_all_pipeline_roles_have_restrictions(self):
        """All pipeline agent roles are registered."""
        expected_roles = [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.INTEGRATOR,
            AgentRole.CHECKER,
            AgentRole.ARCHITECT,
            AgentRole.TASK_PLANNER,
            AgentRole.RISK_ANALYST,
            AgentRole.REFINER,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
            AgentRole.REVIEWER_UNIFIED,
            AgentRole.COORDINATOR,
        ]
        for role in expected_roles:
            assert role in AGENT_GH_RESTRICTIONS, f"Missing restriction for role: {role}"

    def test_all_roles_block_issue_comment(self):
        """All registered roles block issue comment."""
        for role, restriction in AGENT_GH_RESTRICTIONS.items():
            assert restriction.is_blocked("issue comment 123"), (
                f"Role '{role}' should block 'issue comment'"
            )

    def test_all_roles_block_issue_edit(self):
        """All registered roles block issue edit."""
        for role, restriction in AGENT_GH_RESTRICTIONS.items():
            assert restriction.is_blocked("issue edit 123"), (
                f"Role '{role}' should block 'issue edit'"
            )

    def test_roles_allow_pr_view(self):
        """Roles should not block pr view."""
        for role, restriction in AGENT_GH_RESTRICTIONS.items():
            assert restriction.is_blocked("pr view 123") is False, (
                f"Role '{role}' should not block 'pr view'"
            )


class TestCheckAgentGHOperation:
    """Tests for check_agent_gh_operation function."""

    def test_blocks_issue_comment_for_reviewer_refine(self):
        """reviewer_refine is blocked from posting issue comments."""
        allowed, reason = check_agent_gh_operation("reviewer_refine", "issue comment 1032")
        assert allowed is False
        assert "not allowed" in reason.lower()

    def test_blocks_issue_comment_for_coder(self):
        """coder is blocked from posting issue comments."""
        allowed, reason = check_agent_gh_operation("coder", "issue comment 123")
        assert allowed is False

    def test_blocks_issue_edit_for_refiner(self):
        """refiner is blocked from editing issues."""
        allowed, reason = check_agent_gh_operation("refiner", "issue edit 456")
        assert allowed is False

    def test_allows_pr_view_for_coder(self):
        """coder is allowed to view PRs."""
        allowed, reason = check_agent_gh_operation("coder", "pr view 123")
        assert allowed is True

    def test_allows_unknown_role(self):
        """Unknown roles are allowed for backward compatibility."""
        allowed, reason = check_agent_gh_operation("unknown_role", "issue comment 123")
        assert allowed is True

    def test_allows_empty_role(self):
        """Empty role string is allowed."""
        allowed, reason = check_agent_gh_operation("", "issue comment 123")
        assert allowed is True

    def test_allows_none_role(self):
        """None role is allowed (backward compat)."""
        # check_agent_gh_operation expects str, but empty works
        allowed, reason = check_agent_gh_operation("", "issue comment 123")
        assert allowed is True

    def test_blocks_all_pipeline_roles(self):
        """All pipeline roles are blocked from issue comments."""
        roles = [
            "coder", "tester", "documenter", "integrator", "checker",
            "architect", "task_planner", "risk_analyst", "refiner",
            "reviewer_code", "reviewer_contract", "reviewer_agent_design",
            "reviewer_refine", "reviewer_plan", "reviewer_unified",
            "coordinator",
        ]
        for role in roles:
            allowed, _ = check_agent_gh_operation(role, "issue comment 123")
            assert allowed is False, f"Role '{role}' should be blocked from issue comment"
