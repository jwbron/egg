"""Unit tests for agent_restrictions.py pattern matching, path normalization, and role validation.

Covers:
- AgentFilePattern.can_write() pattern matching logic
- _normalize_path() path traversal prevention
- _matches_pattern() glob-style matching (prefix, wildcard, **)
- All 14 agent role permission matrices
- check_agent_file_access() and validate_agent_push() entry points
- Blocked patterns taking precedence over allowed patterns (security)
- Edge cases: empty inputs, unknown roles, many blocked files
"""

import pytest
from agent_restrictions import (
    AGENT_PATTERNS,
    AgentFilePattern,
    AgentRestrictionResult,
    AgentRole,
    check_agent_file_access,
    get_agent_pattern,
    validate_agent_push,
)


class TestNormalizePath:
    """Tests for AgentFilePattern._normalize_path()."""

    def test_strips_leading_dot_slash(self):
        assert AgentFilePattern._normalize_path("./src/app.py") == "src/app.py"

    def test_strips_leading_slash(self):
        assert AgentFilePattern._normalize_path("/src/app.py") == "src/app.py"

    def test_normalizes_double_slashes(self):
        assert AgentFilePattern._normalize_path("src//app.py") == "src/app.py"

    def test_rejects_path_traversal(self):
        result = AgentFilePattern._normalize_path("../etc/passwd")
        assert result == "__INVALID_PATH_TRAVERSAL__"

    def test_rejects_embedded_traversal(self):
        result = AgentFilePattern._normalize_path("src/../../etc/passwd")
        assert result == "__INVALID_PATH_TRAVERSAL__"

    def test_dot_dot_resolves_up(self):
        """src/.. normalizes to '.' which is the current directory (safe)."""
        result = AgentFilePattern._normalize_path("src/..")
        assert result == "."

    def test_simple_path_unchanged(self):
        assert AgentFilePattern._normalize_path("src/app.py") == "src/app.py"

    def test_bare_filename(self):
        assert AgentFilePattern._normalize_path("file.py") == "file.py"

    def test_dot_in_filename(self):
        """Single dot in path component is fine (not traversal)."""
        result = AgentFilePattern._normalize_path("./file.py")
        assert result == "file.py"

    def test_leading_multiple_slashes(self):
        result = AgentFilePattern._normalize_path("///src/app.py")
        assert result == "src/app.py"


class TestMatchesPattern:
    """Tests for AgentFilePattern._matches_pattern()."""

    def test_exact_match(self):
        assert AgentFilePattern._matches_pattern("Makefile", "Makefile") is True

    def test_prefix_directory_match(self):
        assert AgentFilePattern._matches_pattern("docs/guide.md", "docs/") is True

    def test_prefix_no_match(self):
        assert AgentFilePattern._matches_pattern("src/docs/guide.md", "docs/") is False

    def test_nested_prefix_match(self):
        assert AgentFilePattern._matches_pattern("docs/guides/setup.md", "docs/") is True

    def test_directory_itself(self):
        """Pattern 'docs/' should match path 'docs' (directory itself)."""
        assert AgentFilePattern._matches_pattern("docs", "docs/") is True

    def test_wildcard_extension(self):
        assert AgentFilePattern._matches_pattern("file.py", "*.py") is True

    def test_wildcard_extension_no_match(self):
        assert AgentFilePattern._matches_pattern("file.js", "*.py") is False

    def test_double_wildcard_py(self):
        assert AgentFilePattern._matches_pattern("src/deep/module.py", "**/*.py") is True

    def test_double_wildcard_at_root(self):
        assert AgentFilePattern._matches_pattern("module.py", "**/*.py") is True

    def test_double_wildcard_with_prefix(self):
        assert AgentFilePattern._matches_pattern("tests/unit/test_foo.py", "**/test_*.py") is True

    def test_fnmatch_in_pattern(self):
        assert AgentFilePattern._matches_pattern("test_foo.py", "test_*.py") is True

    def test_prefix_match_with_egg_state(self):
        assert (
            AgentFilePattern._matches_pattern(
                ".egg-state/agent-outputs/out.json", ".egg-state/agent-outputs/"
            )
            is True
        )

    def test_leading_dot_slash_normalized(self):
        assert AgentFilePattern._matches_pattern("./src/app.py", "./src/") is True


class TestCanWrite:
    """Tests for AgentFilePattern.can_write()."""

    def test_allowed_pattern_permits(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=["src/"])
        assert pattern.can_write("src/app.py") is True

    def test_missing_allowed_pattern_denies(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=["src/"])
        assert pattern.can_write("docs/readme.md") is False

    def test_blocked_overrides_allowed(self):
        """Blocked patterns must take precedence over allowed patterns."""
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=["**/*.py"],
            blocked_patterns=["tests/"],
        )
        assert pattern.can_write("tests/test_foo.py") is False

    def test_path_traversal_denied(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=["**/*"])
        assert pattern.can_write("../../../etc/passwd") is False

    def test_no_allowed_patterns_denies_all(self):
        pattern = AgentFilePattern(role="test", allowed_patterns=[])
        assert pattern.can_write("anything.py") is False

    def test_empty_blocked_allows_through(self):
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=["**/*.py"],
            blocked_patterns=[],
        )
        assert pattern.can_write("src/app.py") is True

    def test_egg_state_agent_outputs_allowed(self):
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=[".egg-state/agent-outputs/"],
        )
        assert pattern.can_write(".egg-state/agent-outputs/result.json") is True

    def test_egg_state_contracts_blocked(self):
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=[".egg-state/"],
            blocked_patterns=[".egg-state/contracts/"],
        )
        assert pattern.can_write(".egg-state/contracts/644.json") is False


class TestCoderRole:
    """Verify coder agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.CODER)

    def test_can_write_source_py(self, pattern):
        assert pattern.can_write("gateway/gateway.py") is True

    def test_can_write_source_ts(self, pattern):
        assert pattern.can_write("frontend/src/App.tsx") is True

    def test_can_write_config_yaml(self, pattern):
        assert pattern.can_write("config/settings.yml") is True

    def test_can_write_config_json(self, pattern):
        assert pattern.can_write("package.json") is True

    def test_cannot_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_cannot_write_readme(self, pattern):
        assert pattern.can_write("README.md") is False

    def test_cannot_write_test_dir(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_cannot_write_nested_test_dir(self, pattern):
        assert pattern.can_write("gateway/tests/test_gw.py") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/coder-out.json") is True


class TestTesterRole:
    """Verify tester agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.TESTER)

    def test_can_write_test_dir(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is True

    def test_cannot_write_nested_test_in_blocked_dir(self, pattern):
        """Tester blocked from gateway/ even for test files (blocked > allowed)."""
        assert pattern.can_write("gateway/tests/test_gw.py") is False

    def test_cannot_write_test_file_in_blocked_dir(self, pattern):
        """Tester blocked from src/ even for test-named files (blocked > allowed)."""
        assert pattern.can_write("src/test_module.py") is False

    def test_can_write_spec_file(self, pattern):
        assert pattern.can_write("components/Button.spec.tsx") is True

    def test_cannot_write_source(self, pattern):
        assert pattern.can_write("src/app.py") is False

    def test_cannot_write_gateway(self, pattern):
        assert pattern.can_write("gateway/gateway.py") is False

    def test_cannot_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/tester-out.json") is True


class TestDocumenterRole:
    """Verify documenter agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.DOCUMENTER)

    def test_can_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is True

    def test_can_write_readme(self, pattern):
        assert pattern.can_write("README.md") is True

    def test_can_write_changelog(self, pattern):
        assert pattern.can_write("CHANGELOG.md") is True

    def test_cannot_write_source_py(self, pattern):
        assert pattern.can_write("src/app.py") is False

    def test_cannot_write_source_ts(self, pattern):
        assert pattern.can_write("frontend/src/App.tsx") is False

    def test_cannot_write_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/doc-out.json") is True


class TestIntegratorRole:
    """Verify integrator agent is read-only except handoff output."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.INTEGRATOR)

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/integrator-out.json") is True

    def test_cannot_write_source(self, pattern):
        assert pattern.can_write("src/app.py") is False

    def test_cannot_write_gateway(self, pattern):
        assert pattern.can_write("gateway/gateway.py") is False

    def test_cannot_write_tests(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is False

    def test_cannot_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_cannot_write_github(self, pattern):
        assert pattern.can_write(".github/workflows/ci.yml") is False


class TestArchitectRole:
    """Verify architect agent can only write drafts and agent-outputs."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.ARCHITECT)

    def test_can_write_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/analysis.md") is True

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/arch-out.json") is True

    def test_cannot_write_source(self, pattern):
        assert pattern.can_write("src/app.py") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False


class TestReviewerRoles:
    """Verify reviewer agents can only write reviews and agent-outputs."""

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ],
    )
    def test_can_write_reviews(self, role):
        pattern = get_agent_pattern(role)
        assert pattern.can_write(".egg-state/reviews/review.json") is True

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ],
    )
    def test_can_write_agent_outputs(self, role):
        pattern = get_agent_pattern(role)
        assert pattern.can_write(".egg-state/agent-outputs/review-out.json") is True

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ],
    )
    def test_cannot_write_source(self, role):
        pattern = get_agent_pattern(role)
        assert pattern.can_write("src/app.py") is False

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ],
    )
    def test_cannot_write_drafts(self, role):
        pattern = get_agent_pattern(role)
        assert pattern.can_write(".egg-state/drafts/plan.md") is False

    @pytest.mark.parametrize(
        "role",
        [
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ],
    )
    def test_cannot_write_contracts(self, role):
        pattern = get_agent_pattern(role)
        assert pattern.can_write(".egg-state/contracts/123.json") is False


class TestRefinerRole:
    """Verify refiner agent can only write drafts and agent-outputs."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.REFINER)

    def test_can_write_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/analysis.md") is True

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/refiner-out.json") is True

    def test_cannot_write_source_py(self, pattern):
        assert pattern.can_write("src/app.py") is False

    def test_cannot_write_source_ts(self, pattern):
        assert pattern.can_write("frontend/src/App.tsx") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False


class TestAllRolesRegistered:
    """Verify all defined roles have patterns in the registry."""

    def test_all_roles_in_registry(self):
        """Every role constant in AgentRole should have an entry in AGENT_PATTERNS."""
        role_constants = [
            v for k, v in vars(AgentRole).items() if not k.startswith("_") and isinstance(v, str)
        ]
        for role in role_constants:
            assert role in AGENT_PATTERNS, f"Role '{role}' missing from AGENT_PATTERNS registry"

    def test_registry_count_matches_roles(self):
        role_constants = [
            v for k, v in vars(AgentRole).items() if not k.startswith("_") and isinstance(v, str)
        ]
        assert len(AGENT_PATTERNS) == len(role_constants)


class TestGetAgentPattern:
    """Tests for get_agent_pattern()."""

    def test_known_role(self):
        pattern = get_agent_pattern("coder")
        assert pattern is not None
        assert pattern.role == "coder"

    def test_case_insensitive(self):
        pattern = get_agent_pattern("CODER")
        assert pattern is not None

    def test_unknown_role_returns_none(self):
        assert get_agent_pattern("nonexistent") is None


class TestCheckAgentFileAccess:
    """Tests for check_agent_file_access()."""

    def test_allowed_files_pass(self):
        allowed, blocked, reason = check_agent_file_access("coder", ["gateway/app.py"])
        assert allowed is True
        assert blocked == []

    def test_blocked_files_reported(self):
        allowed, blocked, reason = check_agent_file_access("coder", ["tests/test_foo.py"])
        assert allowed is False
        assert "tests/test_foo.py" in blocked

    def test_unknown_role_allows_all(self):
        allowed, blocked, reason = check_agent_file_access("unknown_role", ["anything.py"])
        assert allowed is True
        assert blocked == []

    def test_multiple_blocked_files_truncated(self):
        """Reason message shows at most 5 blocked files then count."""
        files = [f"tests/test_{i}.py" for i in range(8)]
        allowed, blocked, reason = check_agent_file_access("coder", files)
        assert allowed is False
        assert len(blocked) == 8
        assert "and 3 more" in reason

    def test_mixed_allowed_and_blocked(self):
        files = ["gateway/app.py", "tests/test_foo.py"]
        allowed, blocked, reason = check_agent_file_access("coder", files)
        assert allowed is False
        assert "tests/test_foo.py" in blocked
        assert "gateway/app.py" not in blocked


class TestValidateAgentPush:
    """Tests for validate_agent_push() main entry point."""

    def test_no_role_allows(self):
        result = validate_agent_push("", ["file.py"])
        assert result.allowed is True

    def test_no_files_allows(self):
        result = validate_agent_push("coder", [])
        assert result.allowed is True

    def test_allowed_push(self):
        result = validate_agent_push("coder", ["gateway/app.py"])
        assert result.allowed is True
        assert result.role == "coder"

    def test_blocked_push(self):
        result = validate_agent_push("coder", ["tests/test_foo.py"])
        assert result.allowed is False
        assert result.role == "coder"
        assert "tests/test_foo.py" in result.blocked_files

    def test_result_types(self):
        result = validate_agent_push("coder", ["gateway/app.py"])
        assert isinstance(result, AgentRestrictionResult)


class TestAgentRestrictionResult:
    """Tests for AgentRestrictionResult dataclass."""

    def test_allow_factory(self):
        result = AgentRestrictionResult.allow("coder", "All OK")
        assert result.allowed is True
        assert result.role == "coder"
        assert result.blocked_files == []

    def test_block_factory(self):
        result = AgentRestrictionResult.block("coder", ["file.py"], "Blocked")
        assert result.allowed is False
        assert result.role == "coder"
        assert result.blocked_files == ["file.py"]
