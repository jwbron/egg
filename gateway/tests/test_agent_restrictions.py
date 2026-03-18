"""Tests for agent restrictions in the gateway.

Covers:
- Overseer file patterns: allows .egg-state/oversight/, blocks source files
- Overseer GitHub restrictions: blocks pr merge, pr create; allows issue create
- Autofixer file patterns: allows source+config, blocks docs and contracts
- Conflict resolver file patterns: allows source+test+docs+config, blocks .egg-state/
- Inspector file patterns: allows agent-outputs only

Related: issue #1059 — Phase 1 overseer role definition
Related: issue #1030 — Agent team roster, roles, and access controls
"""

import pytest
from agent_restrictions import (
    AGENT_GH_RESTRICTIONS,
    AGENT_PATTERNS,
    AUTOFIXER_PATTERNS,
    CONFLICT_RESOLVER_PATTERNS,
    INSPECTOR_PATTERNS,
    OVERSEER_PATTERNS,
    AgentRole,
    check_agent_file_access,
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


# ---------------------------------------------------------------------------
# Autofixer file patterns
# ---------------------------------------------------------------------------


class TestAutofixerFilePatterns:
    """Verify the autofixer agent file access patterns."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.AUTOFIXER)

    def test_autofixer_allows_python_source(self, pattern):
        """Autofixer can write to Python source files."""
        assert pattern.can_write("orchestrator/main.py") is True
        assert pattern.can_write("shared/utils.py") is True

    def test_autofixer_allows_typescript(self, pattern):
        """Autofixer can write to TypeScript files."""
        assert pattern.can_write("src/app.ts") is True
        assert pattern.can_write("src/component.tsx") is True

    def test_autofixer_allows_config(self, pattern):
        """Autofixer can write to config files."""
        assert pattern.can_write("pyproject.toml") is True
        assert pattern.can_write("config.yml") is True
        assert pattern.can_write("config.yaml") is True
        assert pattern.can_write("package.json") is True

    def test_autofixer_allows_agent_outputs(self, pattern):
        """Autofixer can write to agent-outputs directory."""
        assert pattern.can_write(".egg-state/agent-outputs/report.json") is True

    def test_autofixer_blocks_docs(self, pattern):
        """Autofixer cannot write to docs/."""
        assert pattern.can_write("docs/guide.md") is False
        assert pattern.can_write("docs/index.md") is False

    def test_autofixer_blocks_markdown(self, pattern):
        """Autofixer cannot write to markdown files."""
        assert pattern.can_write("README.md") is False

    def test_autofixer_blocks_contracts(self, pattern):
        """Autofixer cannot write to .egg-state/contracts/."""
        assert pattern.can_write(".egg-state/contracts/contract.json") is False

    def test_autofixer_in_registry(self):
        """Autofixer should be registered in AGENT_PATTERNS."""
        assert AgentRole.AUTOFIXER in AGENT_PATTERNS
        assert AGENT_PATTERNS[AgentRole.AUTOFIXER] is AUTOFIXER_PATTERNS

    def test_check_agent_file_access_autofixer(self):
        """check_agent_file_access works for autofixer role."""
        allowed, blocked, _ = check_agent_file_access("autofixer", ["src/main.py", "config.yml"])
        assert allowed is True
        assert blocked == []

        allowed, blocked, _ = check_agent_file_access("autofixer", ["src/main.py", "docs/guide.md"])
        assert allowed is False
        assert "docs/guide.md" in blocked


# ---------------------------------------------------------------------------
# Conflict resolver file patterns
# ---------------------------------------------------------------------------


class TestConflictResolverFilePatterns:
    """Verify the conflict resolver agent file access patterns."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.CONFLICT_RESOLVER)

    def test_conflict_resolver_allows_source(self, pattern):
        """Conflict resolver can write to source files."""
        assert pattern.can_write("orchestrator/main.py") is True
        assert pattern.can_write("src/app.ts") is True

    def test_conflict_resolver_allows_tests(self, pattern):
        """Conflict resolver can write to test files."""
        assert pattern.can_write("tests/test_foo.py") is True
        assert pattern.can_write("orchestrator/tests/test_bar.py") is True

    def test_conflict_resolver_allows_docs(self, pattern):
        """Conflict resolver can write to documentation files."""
        assert pattern.can_write("docs/guide.md") is True
        assert pattern.can_write("README.md") is True

    def test_conflict_resolver_allows_config(self, pattern):
        """Conflict resolver can write to config files."""
        assert pattern.can_write("config.yml") is True
        assert pattern.can_write("pyproject.toml") is True

    def test_conflict_resolver_allows_agent_outputs(self, pattern):
        """Conflict resolver can write to agent-outputs."""
        assert pattern.can_write(".egg-state/agent-outputs/report.json") is True

    def test_conflict_resolver_blocks_egg_state(self, pattern):
        """Conflict resolver cannot write to .egg-state/ (except agent-outputs)."""
        assert pattern.can_write(".egg-state/contracts/contract.json") is False
        assert pattern.can_write(".egg-state/pipelines/state.json") is False
        assert pattern.can_write(".egg-state/drafts/draft.md") is False
        assert pattern.can_write(".egg-state/reviews/review.json") is False

    def test_conflict_resolver_in_registry(self):
        """Conflict resolver should be registered in AGENT_PATTERNS."""
        assert AgentRole.CONFLICT_RESOLVER in AGENT_PATTERNS
        assert AGENT_PATTERNS[AgentRole.CONFLICT_RESOLVER] is CONFLICT_RESOLVER_PATTERNS

    def test_check_agent_file_access_conflict_resolver(self):
        """check_agent_file_access works for conflict_resolver role."""
        allowed, blocked, _ = check_agent_file_access(
            "conflict_resolver", ["src/main.py", "tests/test_foo.py", "docs/guide.md"]
        )
        assert allowed is True
        assert blocked == []

        allowed, blocked, _ = check_agent_file_access(
            "conflict_resolver", ["src/main.py", ".egg-state/contracts/c.json"]
        )
        assert allowed is False
        assert ".egg-state/contracts/c.json" in blocked


# ---------------------------------------------------------------------------
# Inspector file patterns
# ---------------------------------------------------------------------------


class TestInspectorFilePatterns:
    """Verify the inspector agent file access patterns."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.INSPECTOR)

    def test_inspector_allows_agent_outputs(self, pattern):
        """Inspector can write to .egg-state/agent-outputs/."""
        assert pattern.can_write(".egg-state/agent-outputs/diagnostic.json") is True

    def test_inspector_blocks_source(self, pattern):
        """Inspector cannot write to source files."""
        assert pattern.can_write("orchestrator/main.py") is False
        assert pattern.can_write("src/app.ts") is False

    def test_inspector_blocks_docs(self, pattern):
        """Inspector cannot write to docs."""
        assert pattern.can_write("docs/guide.md") is False

    def test_inspector_blocks_tests(self, pattern):
        """Inspector cannot write to test directories."""
        assert pattern.can_write("tests/test_foo.py") is False

    def test_inspector_in_registry(self):
        """Inspector should be registered in AGENT_PATTERNS."""
        assert AgentRole.INSPECTOR in AGENT_PATTERNS
        assert AGENT_PATTERNS[AgentRole.INSPECTOR] is INSPECTOR_PATTERNS


# ---------------------------------------------------------------------------
# GH restrictions for new roles
# ---------------------------------------------------------------------------


class TestNewRoleGHRestrictions:
    """Verify GitHub restrictions for new roles."""

    @pytest.mark.parametrize("role", ["autofixer", "conflict_resolver", "inspector"])
    def test_new_roles_block_issue_comment(self, role):
        """New roles cannot post issue comments."""
        allowed, _ = check_agent_gh_operation(role, "issue comment 123")
        assert allowed is False

    @pytest.mark.parametrize("role", ["autofixer", "conflict_resolver", "inspector"])
    def test_new_roles_block_issue_edit(self, role):
        """New roles cannot edit issues."""
        allowed, _ = check_agent_gh_operation(role, "issue edit 123")
        assert allowed is False

    @pytest.mark.parametrize("role", ["autofixer", "conflict_resolver", "inspector"])
    def test_new_roles_in_gh_restrictions(self, role):
        """New roles should be registered in AGENT_GH_RESTRICTIONS."""
        assert role in AGENT_GH_RESTRICTIONS
