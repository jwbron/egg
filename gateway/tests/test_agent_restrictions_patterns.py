"""Unit tests for agent_restrictions.py pattern matching, path normalization, and role validation.

Covers:
- AgentFilePattern.can_write() pattern matching logic
- _normalize_path() path traversal prevention
- _matches_pattern() glob-style matching (prefix, wildcard, **)
- All 12 agent role permission matrices
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

    def test_block_exempt_overrides_blocked(self):
        """block_exempt_patterns carve out exceptions from blocked patterns."""
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=["**/*.md"],
            blocked_patterns=["**/*.md"],
            block_exempt_patterns=["config/rules/*.md"],
        )
        assert pattern.can_write("config/rules/my-rule.md") is True
        assert pattern.can_write("docs/guide.md") is False

    def test_block_exempt_still_requires_allowed(self):
        """Exempt from block is not enough — must also match allowed patterns."""
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=["src/"],
            blocked_patterns=["**/*.md"],
            block_exempt_patterns=["config/rules/*.md"],
        )
        assert pattern.can_write("config/rules/my-rule.md") is False

    def test_block_exempt_does_not_bypass_other_blocks(self):
        """Exempt patterns must only bypass their intended block, not others."""
        pattern = AgentFilePattern(
            role="test",
            allowed_patterns=["**/*.md", "config/rules/*.md"],
            blocked_patterns=["**/*.md", "docs/"],
            block_exempt_patterns=["config/rules/*.md"],
        )
        assert pattern.can_write("config/rules/my-rule.md") is True
        # Exempt pattern should NOT let paths inside docs/ through
        assert pattern.can_write("docs/rules/evil.md") is False


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

    def test_cannot_write_go_test_file(self, pattern):
        """Coder cannot write Go test files (Tester handles)."""
        assert pattern.can_write("pkg/merge_test.go") is False

    def test_cannot_write_go_test_prefix(self, pattern):
        """Coder cannot write Go test files with test_ prefix."""
        assert pattern.can_write("pkg/test_merge.go") is False

    def test_cannot_write_conftest(self, pattern):
        """Coder cannot write conftest.py (Tester handles pytest infrastructure)."""
        assert pattern.can_write("conftest.py") is False

    def test_cannot_write_nested_conftest(self, pattern):
        """Coder cannot write nested conftest.py files."""
        assert pattern.can_write("gateway/conftest.py") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/coder-out.json") is True

    def test_can_write_python_version(self, pattern):
        """Coder can write .python-version (common project config)."""
        assert pattern.can_write(".python-version") is True

    def test_can_write_uv_lock(self, pattern):
        """Coder can write uv.lock (dependency lock file)."""
        assert pattern.can_write("uv.lock") is True

    def test_can_write_makefile(self, pattern):
        """Coder can write Makefile."""
        assert pattern.can_write("Makefile") is True

    def test_can_write_dockerfile(self, pattern):
        """Coder can write Dockerfile."""
        assert pattern.can_write("Dockerfile") is True

    def test_can_write_gitignore(self, pattern):
        """Coder can write .gitignore."""
        assert pattern.can_write(".gitignore") is True

    def test_can_write_requirements_txt(self, pattern):
        """Coder can write requirements.txt."""
        assert pattern.can_write("requirements.txt") is True

    def test_can_write_nested_requirements(self, pattern):
        """Coder can write nested requirements files."""
        assert pattern.can_write("deploy/requirements-prod.txt") is True

    def test_can_write_poetry_lock(self, pattern):
        """Coder can write poetry.lock."""
        assert pattern.can_write("poetry.lock") is True

    def test_cannot_write_test_dir_still_blocked(self, pattern):
        """Regression: Coder still cannot write test directory files."""
        assert pattern.can_write("tests/test_foo.py") is False

    def test_can_write_agent_config_rules_md(self, pattern):
        """Coder can write .md files in rules/ (functional code, not docs)."""
        assert pattern.can_write("sandbox/agent-config/rules/push-recovery.md") is True

    def test_can_write_agent_config_commands_md(self, pattern):
        """Coder can write .md files in commands/ (functional code, not docs)."""
        assert pattern.can_write("sandbox/agent-config/commands/run-tests.md") is True

    def test_cannot_write_docs_md_still_blocked(self, pattern):
        """Regression: Coder still cannot write documentation .md files."""
        assert pattern.can_write("docs/guide.md") is False

    def test_cannot_write_readme_still_blocked(self, pattern):
        """Regression: Coder still cannot write README.md."""
        assert pattern.can_write("README.md") is False

    def test_cannot_write_arbitrary_md(self, pattern):
        """Coder cannot write .md files outside exempt directories."""
        assert pattern.can_write("CHANGELOG.md") is False

    def test_cannot_write_docs_rules_md(self, pattern):
        """Coder cannot write .md in docs/ even if path contains 'rules/'."""
        assert pattern.can_write("docs/rules/evil.md") is False

    def test_cannot_write_tests_rules_md(self, pattern):
        """Coder cannot write .md in tests/ even if path contains 'rules/'."""
        assert pattern.can_write("tests/rules/evil.md") is False

    def test_cannot_write_contracts_commands_md(self, pattern):
        """Coder cannot write .md in contracts/ even if path contains 'commands/'."""
        assert pattern.can_write(".egg-state/contracts/commands/evil.md") is False

    def test_can_write_top_level_skills_md(self, pattern):
        """Coder can write .md files in top-level skills/ (skill definitions)."""
        assert pattern.can_write("skills/sdlc/SKILL.md") is True
        assert pattern.can_write("skills/egg-setup/SKILL.md") is True

    def test_can_write_top_level_skills_non_md(self, pattern):
        """Coder can write non-.md files in top-level skills/."""
        assert pattern.can_write("skills/sdlc/helper.py") is True


class TestCoderBlocklistComplement1901:
    """TASK-5-2 (#1901): mirror of TASK-5-1 against the gateway re-export.

    Same behavior contract as shared/tests/test_egg_restrictions.py — the
    gateway's ``agent_restrictions`` module re-exports the shared package,
    so the same assertions must hold here.  Duplicated rather than
    parametrized because the two test suites historically run in
    different PYTHONPATH contexts (shared-only vs. gateway+shared).
    """

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.CODER)

    # --- Allowed (TASK-5-2 mirroring TASK-5-1 True list) ---

    def test_allows_extensionless_bin_egg(self, pattern):
        assert pattern.can_write("bin/egg") is True

    def test_allows_extensionless_bin_egg_deploy(self, pattern):
        assert pattern.can_write("bin/egg-deploy") is True

    def test_allows_extensionless_bin_egg_status(self, pattern):
        assert pattern.can_write("bin/egg-status") is True

    def test_allows_sandbox_egg(self, pattern):
        assert pattern.can_write("sandbox/egg") is True

    def test_allows_sandbox_bin_egg_health_inspect(self, pattern):
        assert pattern.can_write("sandbox/bin/egg-health-inspect") is True

    def test_allows_sandbox_scripts_gh(self, pattern):
        """sandbox/scripts/ is writable; the gateway is the sole egress
        chokepoint, so credential-shim modifications are reviewed by
        reviewer_security rather than blocked at the role-pattern layer."""
        assert pattern.can_write("sandbox/scripts/gh") is True

    def test_allows_sandbox_scripts_git_credential_helper(self, pattern):
        """sandbox/scripts/ is writable; see test_allows_sandbox_scripts_gh."""
        assert pattern.can_write("sandbox/scripts/git-credential-github-token") is True

    def test_blocks_github_workflows(self, pattern):
        """.github/ is blocked (branch-protection invariant)."""
        assert pattern.can_write(".github/workflows/ci.yml") is False

    def test_blocks_github_codeowners(self, pattern):
        """.github/ is blocked (branch-protection invariant)."""
        assert pattern.can_write(".github/CODEOWNERS") is False

    def test_allows_license(self, pattern):
        assert pattern.can_write("LICENSE") is True

    def test_allows_dockerignore(self, pattern):
        assert pattern.can_write(".dockerignore") is True

    def test_allows_arbitrary_new_path(self, pattern):
        assert pattern.can_write("path/to/new-thing") is True

    def test_allows_pyproject_toml(self, pattern):
        assert pattern.can_write("pyproject.toml") is True

    def test_allows_makefile(self, pattern):
        assert pattern.can_write("Makefile") is True

    def test_allows_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/coder.json") is True

    def test_allows_skills_skill_md(self, pattern):
        assert pattern.can_write("skills/my-skill/SKILL.md") is True

    def test_allows_skills_handler_py(self, pattern):
        assert pattern.can_write("skills/my-skill/handler.py") is True

    def test_allows_agent_config_rules_md(self, pattern):
        assert pattern.can_write("sandbox/agent-config/rules/foo.md") is True

    def test_allows_agent_config_commands_md(self, pattern):
        assert pattern.can_write("sandbox/agent-config/commands/bar.md") is True

    # --- Blocked (TASK-5-2 mirroring TASK-5-1 False list) ---

    def test_blocks_docs_md(self, pattern):
        assert pattern.can_write("docs/foo.md") is False

    def test_blocks_root_readme(self, pattern):
        assert pattern.can_write("README.md") is False

    def test_blocks_tests_dir(self, pattern):
        assert pattern.can_write("tests/test_x.py") is False

    def test_blocks_singular_test_dir(self, pattern):
        assert pattern.can_write("test/test_y.py") is False

    def test_blocks_nested_tests_init(self, pattern):
        """gateway/tests/__init__.py — covered by the matcher fix in TASK-2-1."""
        assert pattern.can_write("gateway/tests/__init__.py") is False

    def test_blocks_root_conftest(self, pattern):
        # Root-level conftest matches **/conftest.py under the fixed matcher.
        assert pattern.can_write("conftest.py") is False

    def test_blocks_egg_state_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/spec.json") is False

    def test_blocks_egg_state_drafts(self, pattern):
        assert pattern.can_write(".egg-state/drafts/1901-plan.md") is False

    def test_blocks_egg_state_reviews(self, pattern):
        assert pattern.can_write(".egg-state/reviews/verdict.json") is False

    def test_blocks_egg_state_future_subdir(self, pattern):
        assert pattern.can_write(".egg-state/secrets/key") is False

    def test_blocks_traversal_via_can_write(self, pattern):
        # The path-traversal guard lives in _normalize_path via can_write;
        # asserting at the can_write level (NOT _matches_pattern) per TASK-5-2.
        assert pattern.can_write("../../etc/passwd") is False


class TestMatchesPatternFix1901:
    """TASK-5-2 (#1901): regression coverage for the _matches_pattern fix.

    Before #1901 the ** branch fell through to fnmatch-on-basename, which
    incorrectly returned False for nested files under a directory pattern
    like ``**/tests/``.  These tests pin the new behavior.
    """

    def test_nested_dir_under_double_star(self):
        """gateway/tests/__init__.py matches **/tests/ — the bug fix."""
        assert AgentFilePattern._matches_pattern("gateway/tests/__init__.py", "**/tests/") is True

    def test_unrelated_file_under_double_star_dir(self):
        """src/foo.py must NOT match **/tests/ — guards against over-match."""
        assert AgentFilePattern._matches_pattern("src/foo.py", "**/tests/") is False

    def test_top_level_tests_under_double_star(self):
        """tests/conftest.py matches **/tests/ — ** must accept zero segments."""
        assert AgentFilePattern._matches_pattern("tests/conftest.py", "**/tests/") is True

    def test_double_star_extension_pattern_unchanged(self):
        """Regression: **/*.py still matches files anywhere — file-name
        pattern semantics must not be touched by the directory-pattern fix."""
        assert AgentFilePattern._matches_pattern("pkg/bar.py", "**/*.py") is True

    def test_leaf_file_named_like_dir_does_not_match(self):
        """src/tests.py is a Python file named 'tests' — NOT inside a tests/
        directory, so **/tests/ must not match it."""
        assert AgentFilePattern._matches_pattern("src/tests.py", "**/tests/") is False


class TestNoAllowedPatternsDeniesAll1901:
    """TASK-5-2 (#1901): empty-allowed-patterns deny-all backstop preserved."""

    def test_no_allowed_patterns_denies_all(self):
        """Sentinel: pattern with no allow list still denies everything.

        Other roles (architect, refiner, reviewers, …) rely on the
        absence of an explicit allow list defaulting to deny.  The
        blocklist-complement rewrite of CODER_PATTERNS must not have
        broken this for everyone else.
        """
        pattern = AgentFilePattern(role="empty", allowed_patterns=[])
        assert pattern.can_write("anything.py") is False
        assert pattern.can_write(".egg-state/agent-outputs/x.json") is False


class TestTesterScope1901:
    """TASK-5-2 (#1901): tester-scoped regression tests."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.TESTER)

    def test_tester_can_write_test_file(self, pattern):
        assert pattern.can_write("tests/test_x.py") is True

    def test_tester_cannot_write_gateway_source(self, pattern):
        """Tester cannot stray into source code (coder owns)."""
        assert pattern.can_write("gateway/server.py") is False


class TestDocumenterScope1901:
    """TASK-5-2 (#1901): documenter-scoped regression tests."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.DOCUMENTER)

    def test_documenter_can_write_docs_md(self, pattern):
        assert pattern.can_write("docs/x.md") is True

    def test_documenter_cannot_write_source(self, pattern):
        """Documenter cannot stray into source code (coder owns)."""
        assert pattern.can_write("src/app.py") is False


class TestTesterRole:
    """Verify tester agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.TESTER)

    def test_can_write_test_dir(self, pattern):
        assert pattern.can_write("tests/test_foo.py") is True

    def test_can_write_nested_test_dir(self, pattern):
        """Tester can write test files in nested directories."""
        assert pattern.can_write("gateway/tests/test_gw.py") is True

    def test_can_write_test_file_in_src(self, pattern):
        """Tester can write test-named files in source directories."""
        assert pattern.can_write("src/test_module.py") is True

    def test_can_write_spec_file(self, pattern):
        assert pattern.can_write("components/Button.spec.tsx") is True

    def test_can_write_conftest(self, pattern):
        """Tester can write conftest.py (pytest infrastructure)."""
        assert pattern.can_write("conftest.py") is True

    def test_can_write_nested_conftest(self, pattern):
        """Tester can write nested conftest.py files."""
        assert pattern.can_write("gateway/conftest.py") is True

    def test_cannot_write_source(self, pattern):
        """Tester cannot write source files (coder handles implementation)."""
        assert pattern.can_write("src/app.py") is False

    def test_cannot_write_gateway(self, pattern):
        """Tester cannot write gateway source files."""
        assert pattern.can_write("gateway/gateway.py") is False

    def test_cannot_write_config_toml(self, pattern):
        """Tester cannot write configuration files."""
        assert pattern.can_write("pyproject.toml") is False

    def test_cannot_write_config_yml(self, pattern):
        """Tester cannot write YAML configuration files."""
        assert pattern.can_write(".github/workflows/ci.yml") is False

    def test_cannot_write_config_json(self, pattern):
        """Tester cannot write JSON configuration files."""
        assert pattern.can_write("package.json") is False

    def test_cannot_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is False

    def test_cannot_write_contracts(self, pattern):
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/tester-out.json") is True

    def test_can_write_python_version(self, pattern):
        """Tester can write .python-version (needed for test environment setup)."""
        assert pattern.can_write(".python-version") is True


class TestDocumenterRole:
    """Verify documenter agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.DOCUMENTER)

    def test_can_write_docs(self, pattern):
        assert pattern.can_write("docs/guide.md") is True

    def test_can_write_readme(self, pattern):
        assert pattern.can_write("README.md") is True

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

    def test_cannot_write_reviews(self, pattern):
        assert pattern.can_write(".egg-state/reviews/904-review.json") is False


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
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ],
    )
    def test_cannot_write_contracts(self, role):
        pattern = get_agent_pattern(role)
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_reviewer_contract_can_write_contracts(self):
        """Contract reviewer needs write access to mark items done."""
        pattern = get_agent_pattern(AgentRole.REVIEWER_CONTRACT)
        assert pattern.can_write(".egg-state/contracts/123.json") is True


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


class TestAutofixerRole:
    """Verify autofixer agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.AUTOFIXER)

    def test_can_write_source_py(self, pattern):
        """Autofixer can write Python source files."""
        assert pattern.can_write("gateway/gateway.py") is True

    def test_can_write_makefile(self, pattern):
        """Autofixer can write Makefile."""
        assert pattern.can_write("Makefile") is True

    def test_can_write_nested_makefile(self, pattern):
        """Autofixer can write nested Makefile."""
        assert pattern.can_write("gateway/Makefile") is True

    def test_can_write_dockerfile(self, pattern):
        """Autofixer can write Dockerfile."""
        assert pattern.can_write("Dockerfile") is True

    def test_can_write_python_version(self, pattern):
        """Autofixer can write .python-version."""
        assert pattern.can_write(".python-version") is True

    def test_can_write_node_version(self, pattern):
        """Autofixer can write .node-version."""
        assert pattern.can_write(".node-version") is True

    def test_can_write_nvmrc(self, pattern):
        """Autofixer can write .nvmrc (same purpose as .node-version)."""
        assert pattern.can_write(".nvmrc") is True

    def test_can_write_gitattributes(self, pattern):
        """Autofixer can write .gitattributes (line endings and diff config)."""
        assert pattern.can_write(".gitattributes") is True

    def test_can_write_lock_file(self, pattern):
        """Autofixer can write lock files."""
        assert pattern.can_write("uv.lock") is True

    def test_can_write_requirements_txt(self, pattern):
        """Autofixer can write requirements.txt."""
        assert pattern.can_write("requirements.txt") is True

    def test_can_write_nested_requirements(self, pattern):
        """Autofixer can write nested requirements files."""
        assert pattern.can_write("deploy/requirements-prod.txt") is True

    def test_cannot_write_docs(self, pattern):
        """Autofixer cannot write documentation."""
        assert pattern.can_write("docs/guide.md") is False

    def test_cannot_write_contracts(self, pattern):
        """Autofixer cannot write contracts."""
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/autofixer-out.json") is True


class TestConflictResolverRole:
    """Verify conflict resolver agent can/cannot write expected files."""

    @pytest.fixture
    def pattern(self):
        return get_agent_pattern(AgentRole.CONFLICT_RESOLVER)

    def test_can_write_source_py(self, pattern):
        """Conflict resolver can write source files."""
        assert pattern.can_write("gateway/gateway.py") is True

    def test_can_write_test_dir(self, pattern):
        """Conflict resolver can write test files."""
        assert pattern.can_write("tests/test_foo.py") is True

    def test_can_write_docs(self, pattern):
        """Conflict resolver can write documentation."""
        assert pattern.can_write("docs/guide.md") is True

    def test_can_write_makefile(self, pattern):
        """Conflict resolver can write Makefile."""
        assert pattern.can_write("Makefile") is True

    def test_can_write_dockerfile(self, pattern):
        """Conflict resolver can write Dockerfile."""
        assert pattern.can_write("Dockerfile") is True

    def test_can_write_procfile(self, pattern):
        """Conflict resolver can write Procfile."""
        assert pattern.can_write("Procfile") is True

    def test_can_write_python_version(self, pattern):
        """Conflict resolver can write .python-version."""
        assert pattern.can_write(".python-version") is True

    def test_can_write_node_version(self, pattern):
        """Conflict resolver can write .node-version."""
        assert pattern.can_write(".node-version") is True

    def test_can_write_nvmrc(self, pattern):
        """Conflict resolver can write .nvmrc."""
        assert pattern.can_write(".nvmrc") is True

    def test_can_write_gitattributes(self, pattern):
        """Conflict resolver can write .gitattributes."""
        assert pattern.can_write(".gitattributes") is True

    def test_can_write_lock_file(self, pattern):
        """Conflict resolver can write lock files."""
        assert pattern.can_write("poetry.lock") is True

    def test_can_write_requirements_txt(self, pattern):
        """Conflict resolver can write requirements.txt."""
        assert pattern.can_write("requirements.txt") is True

    def test_can_write_nested_requirements(self, pattern):
        """Conflict resolver can write nested requirements files."""
        assert pattern.can_write("deploy/requirements-prod.txt") is True

    def test_cannot_write_contracts(self, pattern):
        """Conflict resolver cannot write contracts."""
        assert pattern.can_write(".egg-state/contracts/123.json") is False

    def test_cannot_write_drafts(self, pattern):
        """Conflict resolver cannot write drafts."""
        assert pattern.can_write(".egg-state/drafts/plan.md") is False

    def test_cannot_write_pipelines(self, pattern):
        """Conflict resolver cannot write pipeline state."""
        assert pattern.can_write(".egg-state/pipelines/p1.json") is False

    def test_cannot_write_reviews(self, pattern):
        """Conflict resolver cannot write reviews."""
        assert pattern.can_write(".egg-state/reviews/review.json") is False

    def test_can_write_agent_outputs(self, pattern):
        assert pattern.can_write(".egg-state/agent-outputs/resolver-out.json") is True


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

    def test_unknown_role_denies_all(self):
        """Unknown roles are denied by default (RISK-7 mitigation, #1481)."""
        allowed, blocked, reason = check_agent_file_access("unknown_role", ["anything.py"])
        assert allowed is False
        assert blocked == ["anything.py"]

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
