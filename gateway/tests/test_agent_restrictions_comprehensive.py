"""Comprehensive tests for all agent role restrictions in the gateway.

Covers:
- File access patterns for ALL agent roles (not just overseer)
- New AUTOFIXER and CONFLICT_RESOLVER patterns
- AGENT_PATTERNS registry completeness
- GitHub operation restrictions for all roles
- validate_agent_push function
- check_agent_file_access function
- Path traversal protection
- Edge cases: empty role, empty files, unknown role

Related: issue #1030 — Agent team roster, roles, and access controls
"""

import pytest
from agent_restrictions import (
    AGENT_GH_RESTRICTIONS,
    AGENT_PATTERNS,
    AgentFilePattern,
    AgentRestrictionResult,
    AgentRole,
    check_agent_file_access,
    check_agent_gh_operation,
    get_agent_pattern,
    validate_agent_push,
)

# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


class TestAgentPatternsRegistry:
    """Verify AGENT_PATTERNS has entries for all expected roles."""

    EXPECTED_ROLES = [
        AgentRole.CODER,
        AgentRole.TESTER,
        AgentRole.DOCUMENTER,
        AgentRole.ARCHITECT,
        AgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST,
        AgentRole.REFINER,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_PLAN,
        AgentRole.OVERSEER,
    ]

    def test_all_existing_roles_registered(self):
        """All standard roles should be in AGENT_PATTERNS."""
        for role in self.EXPECTED_ROLES:
            assert role in AGENT_PATTERNS, f"Missing pattern for role: {role}"

    def test_new_utility_roles_registered(self):
        """AUTOFIXER and CONFLICT_RESOLVER should be in AGENT_PATTERNS."""
        assert hasattr(AgentRole, "AUTOFIXER"), "AgentRole.AUTOFIXER not defined"
        assert hasattr(AgentRole, "CONFLICT_RESOLVER"), "AgentRole.CONFLICT_RESOLVER not defined"
        assert AgentRole.AUTOFIXER in AGENT_PATTERNS, (
            "AUTOFIXER missing from AGENT_PATTERNS registry"
        )
        assert AgentRole.CONFLICT_RESOLVER in AGENT_PATTERNS, (
            "CONFLICT_RESOLVER missing from AGENT_PATTERNS registry"
        )

    def test_get_agent_pattern_returns_pattern(self):
        """get_agent_pattern should return AgentFilePattern for known roles."""
        for role in self.EXPECTED_ROLES:
            pattern = get_agent_pattern(role)
            assert pattern is not None, f"get_agent_pattern({role}) returned None"
            assert isinstance(pattern, AgentFilePattern)

    def test_get_agent_pattern_case_insensitive(self):
        """get_agent_pattern should be case-insensitive."""
        assert get_agent_pattern("CODER") is not None
        assert get_agent_pattern("Coder") is not None
        assert get_agent_pattern("coder") is not None

    def test_get_agent_pattern_unknown_returns_none(self):
        """Unknown role should return None."""
        assert get_agent_pattern("nonexistent_role") is None


# ---------------------------------------------------------------------------
# Coder file patterns
# ---------------------------------------------------------------------------


class TestCoderFilePatterns:
    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.CODER)

    def test_coder_can_write_python(self, pattern):
        assert pattern.can_write("src/main.py") is True
        assert pattern.can_write("shared/egg_contracts/agent_roles.py") is True

    def test_coder_can_write_typescript(self, pattern):
        assert pattern.can_write("src/app.ts") is True
        assert pattern.can_write("src/component.tsx") is True

    def test_coder_can_write_config(self, pattern):
        assert pattern.can_write("config.yml") is True
        assert pattern.can_write("settings.json") is True

    def test_coder_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/coder.json") is True

    def test_coder_blocked_from_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_coder_blocked_from_readmes(self, pattern):
        assert pattern.can_write("README.md") is False

    def test_coder_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/1030.json") is False

    def test_coder_blocked_from_tests(self, pattern):
        """Coder should not write test files (tester handles)."""
        assert pattern.can_write("tests/test_foo.py") is False
        assert pattern.can_write("orchestrator/tests/test_models.py") is False


# ---------------------------------------------------------------------------
# Tester file patterns
# ---------------------------------------------------------------------------


class TestTesterFilePatterns:
    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.TESTER)

    def test_tester_can_write_test_dirs(self, pattern):
        assert pattern.can_write("tests/test_main.py") is True
        assert pattern.can_write("test/test_foo.py") is True

    def test_tester_can_write_nested_test_dirs(self, pattern):
        assert pattern.can_write("orchestrator/tests/test_models.py") is True
        assert pattern.can_write("gateway/tests/test_restrictions.py") is True

    def test_tester_cannot_write_source(self, pattern):
        """Tester cannot write source files (coder handles implementation)."""
        assert pattern.can_write("src/main.py") is False
        assert pattern.can_write("shared/egg_contracts/agent_roles.py") is False

    def test_tester_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/tester.json") is True

    def test_tester_blocked_from_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_tester_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/1030.json") is False

    def test_tester_blocked_from_markdown(self, pattern):
        assert pattern.can_write("README.md") is False

    def test_tester_can_write_lock_files(self, pattern):
        """Tester can write lock files for test dependency management."""
        assert pattern.can_write("uv.lock") is True
        assert pattern.can_write("poetry.lock") is True
        assert pattern.can_write("yarn.lock") is True
        assert pattern.can_write("subdir/yarn.lock") is True

    def test_tester_can_write_requirements_files(self, pattern):
        """Tester can write requirements files for test dependency management."""
        assert pattern.can_write("requirements.txt") is True
        assert pattern.can_write("requirements-test.txt") is True
        assert pattern.can_write("requirements-dev.txt") is True
        assert pattern.can_write("subdir/requirements.txt") is True


# ---------------------------------------------------------------------------
# Documenter file patterns
# ---------------------------------------------------------------------------


class TestDocumenterFilePatterns:
    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.DOCUMENTER)

    def test_documenter_can_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is True
        assert pattern.can_write("docs/index.md") is True

    def test_documenter_can_write_readmes(self, pattern):
        assert pattern.can_write("README.md") is True
        assert pattern.can_write("orchestrator/README.md") is True

    def test_documenter_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/documenter.json") is True

    def test_documenter_blocked_from_source_code(self, pattern):
        assert pattern.can_write("src/main.py") is False
        assert pattern.can_write("gateway/gateway.py") is False

    def test_documenter_blocked_from_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_documenter_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/1030.json") is False


# ---------------------------------------------------------------------------
# Plan-phase agent patterns (Architect, Task Planner, Risk Analyst)
# ---------------------------------------------------------------------------


class TestPlanPhaseAgentPatterns:
    """Plan-phase agents can only write to drafts and agent-outputs."""

    @pytest.fixture(params=[AgentRole.ARCHITECT, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST])
    def pattern(self, request):
        return get_agent_pattern(request.param)

    def test_can_write_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/plan.md") is True

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/output.json") is True

    def test_blocked_from_source(self, pattern):
        assert pattern.can_write("shared/module.py") is False
        assert pattern.can_write("gateway/app.py") is False

    def test_blocked_from_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_blocked_from_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/contract.json") is False

    def test_blocked_from_reviews(self, pattern):
        assert pattern.can_write(".egg-state/reviews/review.json") is False


# ---------------------------------------------------------------------------
# Reviewer agent patterns
# ---------------------------------------------------------------------------


class TestReviewerAgentPatterns:
    """Reviewers can only write to reviews and agent-outputs."""

    @pytest.fixture(
        params=[
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ]
    )
    def pattern(self, request):
        return get_agent_pattern(request.param)

    def test_can_write_reviews(self, pattern):
        assert pattern.can_write(".egg-state/reviews/review.json") is True

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/output.json") is True

    def test_blocked_from_source(self, pattern):
        assert pattern.can_write("shared/module.py") is False
        assert pattern.can_write("gateway/app.py") is False

    def test_blocked_from_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_blocked_from_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/contract.json") is False

    def test_blocked_from_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/draft.md") is False


class TestReviewerContractPatterns:
    """Contract reviewer has write access to .egg-state/contracts/."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.REVIEWER_CONTRACT)

    def test_can_write_reviews(self, pattern):
        assert pattern.can_write(".egg-state/reviews/review.json") is True

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/output.json") is True

    def test_can_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/contract.json") is True

    def test_blocked_from_source(self, pattern):
        assert pattern.can_write("shared/module.py") is False
        assert pattern.can_write("gateway/app.py") is False

    def test_blocked_from_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_blocked_from_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_blocked_from_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/draft.md") is False


# ---------------------------------------------------------------------------
# Refiner agent patterns
# ---------------------------------------------------------------------------


class TestRefinerPatterns:
    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.REFINER)

    def test_can_write_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/analysis.md") is True

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/refiner.json") is True

    def test_blocked_from_source(self, pattern):
        assert pattern.can_write("shared/module.py") is False

    def test_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/contract.json") is False


# ---------------------------------------------------------------------------
# AUTOFIXER patterns (new utility role)
# ---------------------------------------------------------------------------


class TestAutofixerPatterns:
    """AUTOFIXER should write source+config but be blocked from docs and contracts."""

    @pytest.fixture
    def pattern(self):
        p = get_agent_pattern("autofixer")
        if p is None:
            pytest.skip("AUTOFIXER pattern not yet registered in AGENT_PATTERNS")
        return p

    def test_autofixer_can_write_python(self, pattern):
        assert pattern.can_write("src/main.py") is True
        assert pattern.can_write("shared/module.py") is True

    def test_autofixer_can_write_typescript(self, pattern):
        assert pattern.can_write("src/app.ts") is True

    def test_autofixer_can_write_config(self, pattern):
        assert pattern.can_write("config.yml") is True

    def test_autofixer_blocked_from_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_autofixer_blocked_from_markdown(self, pattern):
        assert pattern.can_write("README.md") is False

    def test_autofixer_blocked_from_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/contract.json") is False


# ---------------------------------------------------------------------------
# CONFLICT_RESOLVER patterns (new utility role)
# ---------------------------------------------------------------------------


class TestConflictResolverPatterns:
    """CONFLICT_RESOLVER should write source+test+docs+config, blocked from .egg-state/."""

    @pytest.fixture
    def pattern(self):
        p = get_agent_pattern("conflict_resolver")
        if p is None:
            pytest.skip("CONFLICT_RESOLVER pattern not yet registered in AGENT_PATTERNS")
        return p

    def test_conflict_resolver_can_write_source(self, pattern):
        assert pattern.can_write("src/main.py") is True

    def test_conflict_resolver_can_write_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is True

    def test_conflict_resolver_can_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is True

    def test_conflict_resolver_blocked_from_egg_state(self, pattern):
        assert pattern.can_write(".egg-state/contracts/contract.json") is False
        assert pattern.can_write(".egg-state/drafts/draft.md") is False


# ---------------------------------------------------------------------------
# AgentFilePattern path traversal protection
# ---------------------------------------------------------------------------


class TestPathTraversalProtection:
    def test_path_traversal_blocked(self):
        """Path traversal with .. should be blocked."""
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=[".egg-state/oversight/"],
        )
        assert pattern.can_write("../etc/passwd") is False
        assert pattern.can_write(".egg-state/oversight/../../etc/passwd") is False

    def test_normalized_path_valid(self):
        """Valid paths should work after normalization."""
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=[".egg-state/oversight/"],
        )
        assert pattern.can_write(".egg-state/oversight/report.json") is True


# ---------------------------------------------------------------------------
# check_agent_file_access
# ---------------------------------------------------------------------------


class TestCheckAgentFileAccess:
    def test_allowed_files(self):
        allowed, blocked_files, reason = check_agent_file_access(
            "coder", ["src/main.py", "lib/utils.py"]
        )
        assert allowed is True
        assert blocked_files == []

    def test_blocked_files(self):
        allowed, blocked_files, reason = check_agent_file_access("coder", ["docs/guide.md"])
        assert allowed is False
        assert "docs/guide.md" in blocked_files

    def test_mixed_files(self):
        allowed, blocked_files, reason = check_agent_file_access(
            "coder", ["src/main.py", "docs/guide.md"]
        )
        assert allowed is False
        assert "docs/guide.md" in blocked_files

    def test_unknown_role_denied(self):
        """Unknown roles are denied by default (RISK-7 mitigation, #1481)."""
        allowed, blocked_files, reason = check_agent_file_access("unknown_role", ["any/file.py"])
        assert allowed is False
        assert blocked_files == ["any/file.py"]


# ---------------------------------------------------------------------------
# validate_agent_push
# ---------------------------------------------------------------------------


class TestValidateAgentPush:
    def test_no_role_allows(self):
        result = validate_agent_push("", ["any/file.py"])
        assert result.allowed is True

    def test_no_files_allows(self):
        result = validate_agent_push("coder", [])
        assert result.allowed is True

    def test_valid_push(self):
        result = validate_agent_push("coder", ["src/main.py"])
        assert result.allowed is True
        assert isinstance(result, AgentRestrictionResult)

    def test_blocked_push(self):
        result = validate_agent_push("coder", ["docs/guide.md"])
        assert result.allowed is False
        assert "docs/guide.md" in result.blocked_files


# ---------------------------------------------------------------------------
# GitHub operation restrictions
# ---------------------------------------------------------------------------


class TestGHRestrictions:
    """All pipeline agents should be blocked from issue comments and edits."""

    PIPELINE_ROLES = [
        AgentRole.CODER,
        AgentRole.TESTER,
        AgentRole.DOCUMENTER,
        AgentRole.ARCHITECT,
        AgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST,
        AgentRole.REFINER,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_PLAN,
    ]

    @pytest.fixture(params=PIPELINE_ROLES)
    def role(self, request):
        return request.param

    def test_pipeline_agent_blocked_from_issue_comment(self, role):
        allowed, reason = check_agent_gh_operation(role, "issue comment 123")
        assert allowed is False

    def test_pipeline_agent_blocked_from_issue_edit(self, role):
        allowed, reason = check_agent_gh_operation(role, "issue edit 123")
        assert allowed is False

    def test_pipeline_agent_can_view_issues(self, role):
        allowed, reason = check_agent_gh_operation(role, "issue view 123")
        assert allowed is True

    def test_pipeline_agent_can_view_prs(self, role):
        allowed, reason = check_agent_gh_operation(role, "pr view 123")
        assert allowed is True


class TestGHRestrictionsNewRoles:
    """New utility roles should also have GH restrictions."""

    def test_autofixer_in_gh_restrictions(self):
        """AUTOFIXER should have GH restrictions defined."""
        if AgentRole.AUTOFIXER not in AGENT_GH_RESTRICTIONS:
            pytest.skip("AUTOFIXER not yet in AGENT_GH_RESTRICTIONS")
        allowed, reason = check_agent_gh_operation("autofixer", "issue comment 123")
        assert allowed is False

    def test_conflict_resolver_in_gh_restrictions(self):
        """CONFLICT_RESOLVER should have GH restrictions defined."""
        if AgentRole.CONFLICT_RESOLVER not in AGENT_GH_RESTRICTIONS:
            pytest.skip("CONFLICT_RESOLVER not yet in AGENT_GH_RESTRICTIONS")
        allowed, reason = check_agent_gh_operation("conflict_resolver", "issue comment 123")
        assert allowed is False

    def test_unknown_role_denies_gh_operations(self):
        """Unknown roles are denied for consistency with file access deny-by-default (#1494)."""
        allowed, reason = check_agent_gh_operation("unknown_role", "issue comment 123")
        assert allowed is False

    def test_empty_role_denies_gh_operations(self):
        allowed, reason = check_agent_gh_operation("", "issue comment 123")
        assert allowed is False


# ---------------------------------------------------------------------------
# Three-role behavior coverage (TASK-5-3, #1901)
# ---------------------------------------------------------------------------


class TestThreeRoleBehavior1901:
    """TASK-5-3 (#1901): one case per role confirming the allowed/blocked
    sets from TASK-5-1.  Behavior-level assertions only — no pattern
    enumeration — so the tests survive a refactor of the pattern shape.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "bin/egg",  # extensionless script
            "LICENSE",  # top-level metadata
            "path/to/new-thing",  # arbitrary new file
            "pyproject.toml",  # config
            ".egg-state/agent-outputs/coder.json",  # carved-back exempt
            "skills/my-skill/SKILL.md",  # skills exempt
            "sandbox/agent-config/rules/foo.md",  # rules exempt
            "sandbox/scripts/gh",  # credential shim — gateway is the chokepoint
        ],
    )
    def test_coder_allowed_blocklist_complement(self, path):
        pattern = get_agent_pattern(AgentRole.CODER)
        assert pattern.can_write(path) is True, f"coder must allow {path}"

    @pytest.mark.parametrize(
        "path",
        [
            "docs/foo.md",
            "README.md",
            "tests/test_x.py",
            "gateway/tests/__init__.py",  # matcher-fix coverage
            "conftest.py",  # **/conftest.py at root
            ".egg-state/contracts/spec.json",
            ".egg-state/drafts/1901-plan.md",
            ".egg-state/secrets/key",  # future subdir
            ".github/workflows/ci.yml",  # branch-protection invariant
            ".github/CODEOWNERS",  # branch-protection invariant
        ],
    )
    def test_coder_blocked_blocklist_complement(self, path):
        pattern = get_agent_pattern(AgentRole.CODER)
        assert pattern.can_write(path) is False, f"coder must block {path}"

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("tests/test_x.py", True),
            ("gateway/tests/test_y.py", True),
            ("conftest.py", True),
            ("gateway/conftest.py", True),
            ("gateway/server.py", False),  # source code, coder owns
            ("docs/foo.md", False),  # docs, documenter owns
            (".egg-state/contracts/spec.json", False),
        ],
    )
    def test_tester_three_role_set(self, path, expected):
        pattern = get_agent_pattern(AgentRole.TESTER)
        assert pattern.can_write(path) is expected, f"tester {path} expected {expected}"

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("docs/x.md", True),
            ("README.md", True),
            ("CONTRIBUTING.md", True),
            ("src/app.py", False),  # source code, coder owns
            ("tests/test_x.py", False),  # tests, tester owns
            (".egg-state/contracts/spec.json", False),
        ],
    )
    def test_documenter_three_role_set(self, path, expected):
        pattern = get_agent_pattern(AgentRole.DOCUMENTER)
        assert pattern.can_write(path) is expected, f"documenter {path} expected {expected}"
