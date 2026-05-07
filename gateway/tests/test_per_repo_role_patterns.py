"""Tests for per-repo role-pattern overrides (#2528).

Covers:
- ``build_agent_patterns`` produces the same registry as ``AGENT_PATTERNS``
  when called with no arguments (default-repo regression).
- Per-repo overrides shift coder / tester / documenter boundaries
  according to the language conventions a repo declares.
- Security blocklists (``.egg-state/contracts/``, ``.github/``) cannot be
  bypassed by an attempted override.
- ``get_repo_role_patterns`` validates the YAML shape and silently drops
  unknown keys / non-list values / non-string entries.
- The cache invalidates on ``reset_pattern_cache``.
"""

from __future__ import annotations

from unittest.mock import patch

# Imports go through the gateway-side re-export barrel
# (``gateway/agent_restrictions.py``) rather than directly through
# ``egg_restrictions.patterns`` so the gateway test conftest's
# import-replacement / sys.path setup resolves the worktree's shared/
# package, not the editable install's main-repo copy. The legacy
# pattern-matrix tests use the same convention; see
# gateway/tests/test_agent_restrictions_patterns.py.
from agent_restrictions import (
    AGENT_PATTERNS,
    DEFAULT_CODE_GLOBS,
    DEFAULT_DOCS_GLOBS,
    DEFAULT_TESTS_GLOBS,
    build_agent_patterns,
    get_agent_pattern_for_repo,
    reset_pattern_cache,
)


class TestDefaultRegistryParity:
    """``build_agent_patterns(None)`` must equal the legacy ``AGENT_PATTERNS``."""

    def test_default_registry_has_same_roles(self):
        assert set(build_agent_patterns(None).keys()) == set(AGENT_PATTERNS.keys())

    def test_default_registry_has_same_allowed_patterns(self):
        built = build_agent_patterns(None)
        for role, pattern in AGENT_PATTERNS.items():
            assert built[role].allowed_patterns == pattern.allowed_patterns, role

    def test_default_registry_has_same_blocked_patterns(self):
        built = build_agent_patterns(None)
        for role, pattern in AGENT_PATTERNS.items():
            assert built[role].blocked_patterns == pattern.blocked_patterns, role

    def test_default_registry_has_same_block_exempt_patterns(self):
        built = build_agent_patterns(None)
        for role, pattern in AGENT_PATTERNS.items():
            assert built[role].block_exempt_patterns == pattern.block_exempt_patterns, role


class TestJsConventionRepo:
    """A repo that declares JS conventions should have JS test paths route
    to tester, not coder."""

    def setup_method(self):
        self.patterns = build_agent_patterns(
            None,
            tests_globs=[
                "tests/",
                "**/__tests__/",
                "**/*.test.ts",
                "**/*.test.tsx",
                "**/*.spec.ts",
            ],
            code_globs=["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"],
            docs_globs=["docs/", "**/*.md"],
        )

    def test_coder_blocked_from_js_test_files(self):
        assert not self.patterns["coder"].can_write("src/foo/__tests__/foo.test.ts")
        assert not self.patterns["coder"].can_write("src/bar.spec.ts")

    def test_coder_can_write_production_code(self):
        assert self.patterns["coder"].can_write("src/foo.ts")
        assert self.patterns["coder"].can_write("src/foo.tsx")

    def test_tester_can_write_js_test_files(self):
        assert self.patterns["tester"].can_write("src/foo/__tests__/foo.test.ts")
        assert self.patterns["tester"].can_write("src/bar.spec.ts")

    def test_documenter_blocked_from_js_test_files(self):
        # Tester scope, not documenter — documenter's blocklist must
        # include the repo's tests_globs.
        assert not self.patterns["documenter"].can_write("src/foo/__tests__/foo.test.ts")


class TestGoConventionRepo:
    """A Go repo with same-directory test files (``*_test.go``)."""

    def setup_method(self):
        self.patterns = build_agent_patterns(
            None,
            tests_globs=["**/*_test.go", "**/testdata/**"],
            code_globs=["**/*.go"],
            docs_globs=["docs/", "**/*.md"],
        )

    def test_coder_blocked_from_go_tests_in_source_dirs(self):
        # Go puts tests next to source — `internal/foo/bar_test.go` is
        # a test file.
        assert not self.patterns["coder"].can_write("internal/foo/bar_test.go")

    def test_coder_can_write_go_source(self):
        assert self.patterns["coder"].can_write("internal/foo/bar.go")
        assert self.patterns["coder"].can_write("cmd/server/main.go")

    def test_tester_can_write_go_tests(self):
        assert self.patterns["tester"].can_write("internal/foo/bar_test.go")

    def test_tester_can_write_testdata(self):
        assert self.patterns["tester"].can_write("internal/foo/testdata/sample.json")


class TestSecurityBlocklistsAreNotBypassable:
    """Per-repo overrides MUST NOT relax the security blocklists.

    A repo cannot, e.g., add ``.egg-state/contracts/`` to ``tests_globs``
    in an attempt to grant tester write access there. The pattern
    builders hard-code the security blocks independent of the per-repo
    knobs.
    """

    def test_attempted_contract_bypass_via_tests_globs_fails(self):
        sneaky = build_agent_patterns(None, tests_globs=[".egg-state/contracts/", "**/*_test.py"])
        # Tester's blocked_patterns still forbid contracts.
        assert not sneaky["tester"].can_write(".egg-state/contracts/foo.json")
        # Coder's `.egg-state/` block still forbids contracts.
        assert not sneaky["coder"].can_write(".egg-state/contracts/foo.json")

    def test_attempted_github_bypass_via_code_globs_fails(self):
        sneaky = build_agent_patterns(None, code_globs=["**/*.py", ".github/**"])
        # Coder's `.github/` block holds.
        assert not sneaky["coder"].can_write(".github/workflows/ci.yml")
        # Tester's `.github/` block holds (added in #2521 for parity).
        assert not sneaky["tester"].can_write(".github/workflows/ci.yml")
        # Documenter's `.github/` block holds (#2508 branch-protection
        # invariant).
        assert not sneaky["documenter"].can_write(".github/PULL_REQUEST_TEMPLATE.md")

    def test_attempted_pipeline_state_bypass_via_docs_globs_fails(self):
        sneaky = build_agent_patterns(None, docs_globs=[".egg-state/drafts/", "**/*.md"])
        # Documenter's `.egg-state/contracts/` block holds (it lives in
        # blocked_patterns, not in the configurable docs_globs).
        assert not sneaky["documenter"].can_write(".egg-state/contracts/foo.json")


class TestGetRepoRolePatternsValidation:
    """``config.repo_config.get_repo_role_patterns`` validation."""

    def _patch_setting(self, value):
        return patch("config.repo_config.get_repo_setting", return_value=value)

    def test_missing_block_returns_none(self):
        from config.repo_config import get_repo_role_patterns

        with self._patch_setting(None):
            assert get_repo_role_patterns("owner/repo") is None

    def test_non_dict_returns_none(self):
        from config.repo_config import get_repo_role_patterns

        with self._patch_setting(["not", "a", "dict"]):
            assert get_repo_role_patterns("owner/repo") is None

    def test_valid_subset_returned(self):
        from config.repo_config import get_repo_role_patterns

        with self._patch_setting({"tests_globs": ["**/*_test.go"]}):
            assert get_repo_role_patterns("owner/repo") == {"tests_globs": ["**/*_test.go"]}

    def test_unknown_keys_dropped(self):
        from config.repo_config import get_repo_role_patterns

        # Contracts blocklist is NOT a configurable knob; it must be
        # silently dropped so a misconfig cannot relax security.
        with self._patch_setting(
            {
                "tests_globs": ["**/*_test.go"],
                "contracts_blocklist": [],
                "github_blocklist": [],
            }
        ):
            assert get_repo_role_patterns("owner/repo") == {"tests_globs": ["**/*_test.go"]}

    def test_non_list_values_dropped(self):
        from config.repo_config import get_repo_role_patterns

        with self._patch_setting({"tests_globs": "not-a-list"}):
            assert get_repo_role_patterns("owner/repo") is None

    def test_non_string_entries_filtered(self):
        from config.repo_config import get_repo_role_patterns

        with self._patch_setting({"tests_globs": ["**/*_test.go", 42, None, ""]}):
            assert get_repo_role_patterns("owner/repo") == {"tests_globs": ["**/*_test.go"]}

    def test_empty_dict_returns_none(self):
        from config.repo_config import get_repo_role_patterns

        with self._patch_setting({}):
            assert get_repo_role_patterns("owner/repo") is None


class TestPerRepoCacheInvalidation:
    """``reset_pattern_cache`` must clear per-repo entries so a SIGHUP /
    config reload picks up edited overrides."""

    def setup_method(self):
        reset_pattern_cache()

    def teardown_method(self):
        reset_pattern_cache()

    def test_get_agent_pattern_for_repo_returns_default_when_no_override(self):
        # Sanity: repo=None falls back to AGENT_PATTERNS.
        coder = get_agent_pattern_for_repo("coder", repo=None)
        assert coder is AGENT_PATTERNS["coder"]

    def test_repo_overrides_picked_up_after_cache_reset(self):
        from config import repo_config

        # First call with mocked config returns A.
        with patch.object(
            repo_config,
            "get_repo_role_patterns",
            return_value={"tests_globs": ["**/__tests__/"]},
        ):
            first = get_agent_pattern_for_repo("coder", repo="owner/js-repo")
        assert not first.can_write("src/__tests__/foo.test.ts")

        # Without resetting, a second mock with different config still
        # returns the cached pattern.
        with patch.object(
            repo_config,
            "get_repo_role_patterns",
            return_value={"tests_globs": ["**/*_test.go"]},
        ):
            cached = get_agent_pattern_for_repo("coder", repo="owner/js-repo")
        assert cached is first

        # After reset, the next call re-reads the config.
        reset_pattern_cache()
        with patch.object(
            repo_config,
            "get_repo_role_patterns",
            return_value={"tests_globs": ["**/*_test.go"]},
        ):
            refreshed = get_agent_pattern_for_repo("coder", repo="owner/js-repo")
        assert refreshed is not first
        # Now the JS pattern is no longer blocked because tests_globs
        # changed to Go conventions (no JS __tests__).
        assert refreshed.can_write("src/__tests__/foo.test.ts")


class TestDefaultGlobConstants:
    """The exported default-glob constants must include the canonical
    multi-language test/code/docs conventions so a repo *omitting* an
    override gets coverage of all common shapes."""

    def test_default_tests_globs_cover_python_go_js(self):
        assert "**/test_*.py" in DEFAULT_TESTS_GLOBS
        assert "**/conftest.py" in DEFAULT_TESTS_GLOBS
        assert "**/*_test.go" in DEFAULT_TESTS_GLOBS
        assert "**/*.test.ts" in DEFAULT_TESTS_GLOBS
        assert "**/*.spec.tsx" in DEFAULT_TESTS_GLOBS

    def test_default_code_globs_cover_common_languages(self):
        assert "**/*.py" in DEFAULT_CODE_GLOBS
        assert "**/*.go" in DEFAULT_CODE_GLOBS
        assert "**/*.ts" in DEFAULT_CODE_GLOBS

    def test_default_docs_globs_cover_markdown_and_docs_dir(self):
        assert "docs/" in DEFAULT_DOCS_GLOBS
        assert "**/*.md" in DEFAULT_DOCS_GLOBS
