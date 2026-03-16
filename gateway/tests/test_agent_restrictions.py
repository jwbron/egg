"""Tests for overseer agent restrictions in the gateway.

Covers:
- Overseer file patterns: allows .egg-state/oversight/, blocks source files
- Overseer GitHub restrictions: blocks pr merge, pr create; allows issue create

Related: issue #1059 — Phase 1 overseer role definition
"""

import pytest
from agent_restrictions import (
    AGENT_GH_RESTRICTIONS,
    AGENT_PATTERNS,
    AgentRole,
    OVERSEER_PATTERNS,
    check_agent_gh_operation,
    get_agent_pattern,
)


# ---------------------------------------------------------------------------
# Overseer file patterns
# ---------------------------------------------------------------------------


class TestOverseerFilePatterns:
    """Verify the overseer agent file access patterns."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.OVERSEER)

    def test_overseer_file_pattern_allows_oversight_dir(self, pattern):
        """Overseer can write to .egg-state/oversight/."""
        assert pattern.can_write(".egg-state/oversight/report.json") is True
        assert pattern.can_write(".egg-state/oversight/health-log.json") is True

    def test_overseer_file_pattern_allows_agent_outputs(self, pattern):
        """Overseer can write to .egg-state/agent-outputs/."""
        assert pattern.can_write(".egg-state/agent-outputs/overseer.json") is True

    def test_overseer_file_pattern_blocks_source_files(self, pattern):
        """Overseer cannot write to source code files."""
        assert pattern.can_write("orchestrator/main.py") is False
        assert pattern.can_write("gateway/gateway.py") is False
        assert pattern.can_write("shared/egg_contracts/agent_roles.py") is False
        assert pattern.can_write("src/app.ts") is False
        assert pattern.can_write("lib/utils.js") is False

    def test_overseer_file_pattern_blocks_docs(self, pattern):
        """Overseer cannot write to docs/."""
        assert pattern.can_write("docs/guide.md") is False
        assert pattern.can_write("docs/index.md") is False

    def test_overseer_file_pattern_blocks_tests(self, pattern):
        """Overseer cannot write to test directories."""
        assert pattern.can_write("tests/test_foo.py") is False
        assert pattern.can_write("test/test_bar.py") is False

    def test_overseer_file_pattern_blocks_contracts(self, pattern):
        """Overseer cannot write to .egg-state/contracts/."""
        assert pattern.can_write(".egg-state/contracts/contract.json") is False

    def test_overseer_file_pattern_blocks_drafts(self, pattern):
        """Overseer cannot write to .egg-state/drafts/."""
        assert pattern.can_write(".egg-state/drafts/draft.md") is False

    def test_overseer_file_pattern_blocks_reviews(self, pattern):
        """Overseer cannot write to .egg-state/reviews/."""
        assert pattern.can_write(".egg-state/reviews/review.json") is False

    def test_overseer_file_pattern_blocks_github(self, pattern):
        """Overseer cannot write to .github/."""
        assert pattern.can_write(".github/workflows/ci.yml") is False

    def test_overseer_in_registry(self):
        """Overseer should be registered in AGENT_PATTERNS."""
        assert AgentRole.OVERSEER in AGENT_PATTERNS
        assert AGENT_PATTERNS[AgentRole.OVERSEER] is OVERSEER_PATTERNS


# ---------------------------------------------------------------------------
# Overseer GitHub restrictions
# ---------------------------------------------------------------------------


class TestOverseerGHRestrictions:
    """Verify overseer-specific GitHub operation restrictions."""

    def test_overseer_gh_restrictions_block_pr_merge(self):
        """Overseer cannot merge PRs."""
        allowed, reason = check_agent_gh_operation("overseer", "pr merge 123")
        assert allowed is False
        assert "not allowed" in reason.lower()

    def test_overseer_gh_restrictions_block_pr_create(self):
        """Overseer cannot create PRs."""
        allowed, reason = check_agent_gh_operation("overseer", "pr create --title test")
        assert allowed is False
        assert "not allowed" in reason.lower()

    def test_overseer_gh_restrictions_allow_issue_create(self):
        """Overseer CAN create issues (for diagnostic filing)."""
        allowed, reason = check_agent_gh_operation("overseer", "issue create --title test")
        assert allowed is True

    def test_overseer_gh_restrictions_block_issue_comment(self):
        """Overseer cannot post issue comments."""
        allowed, reason = check_agent_gh_operation("overseer", "issue comment 123")
        assert allowed is False

    def test_overseer_gh_restrictions_block_issue_edit(self):
        """Overseer cannot edit issues."""
        allowed, reason = check_agent_gh_operation("overseer", "issue edit 123")
        assert allowed is False

    def test_overseer_gh_restrictions_allow_pr_view(self):
        """Overseer can view PRs (read-only)."""
        allowed, reason = check_agent_gh_operation("overseer", "pr view 123")
        assert allowed is True

    def test_overseer_gh_restrictions_allow_issue_view(self):
        """Overseer can view issues (read-only)."""
        allowed, reason = check_agent_gh_operation("overseer", "issue view 123")
        assert allowed is True

    def test_overseer_in_gh_restrictions_registry(self):
        """Overseer should be registered in AGENT_GH_RESTRICTIONS."""
        assert AgentRole.OVERSEER in AGENT_GH_RESTRICTIONS

    def test_overseer_gh_restriction_description(self):
        """Overseer restriction should have descriptive text."""
        restriction = AGENT_GH_RESTRICTIONS[AgentRole.OVERSEER]
        assert "overseer" in restriction.description.lower()
        assert len(restriction.blocked_operations) >= 4
