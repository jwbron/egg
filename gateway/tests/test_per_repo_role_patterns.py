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
- Production-like import paths: when ``config.repo_config`` is not on
  ``sys.path``, the fallback to top-level ``repo_config`` resolves
  (mirrors the gateway / orchestrator container layout).
- Sandbox env-var injection: when
  ``EGG_PIPELINE_REPO_PATTERNS_JSON`` is set, the override is read
  from the env without touching the filesystem.
"""

from __future__ import annotations

import json
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

    def test_default_registry_preserves_canonical_partition(self):
        """Pin ``can_write`` outcomes for paths whose role assignment
        is load-bearing across every consumer (gateway push, sandbox
        tool interceptor, plan-time validator). The structural-equality
        tests above don't catch a regression where ``DEFAULT_*_GLOBS``
        and the legacy hardcoded literals drift apart but happen to
        produce dicts that look the same — these behavioral assertions
        do.
        """
        built = build_agent_patterns(None)
        # Python — coder owns source; tester owns tests; documenter
        # owns markdown; everyone else is blocked from each other's
        # scope.
        assert built["coder"].can_write("orchestrator/routes/pipelines.py")
        assert not built["coder"].can_write("tests/test_foo.py")
        assert not built["coder"].can_write("docs/index.md")
        assert built["tester"].can_write("tests/test_foo.py")
        assert not built["tester"].can_write("orchestrator/routes/pipelines.py")
        assert built["documenter"].can_write("docs/index.md")
        assert not built["documenter"].can_write("orchestrator/routes/pipelines.py")
        # Go — same-dir tests route to tester (default ``*_test.go``
        # in tests_globs, even without a per-repo override).
        assert not built["coder"].can_write("internal/foo/bar_test.go")
        assert built["tester"].can_write("internal/foo/bar_test.go")
        # JS / TS — test files route to tester.
        assert not built["coder"].can_write("src/foo.spec.ts")
        assert built["tester"].can_write("src/foo.spec.ts")
        # Security — agents cannot bypass blocklists from any role.
        for role in ("coder", "tester", "documenter"):
            assert not built[role].can_write(".egg-state/contracts/foo.json")
            assert not built[role].can_write(".github/workflows/ci.yml")


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


class TestProductionLikeImportPaths:
    """The two-step ``config.repo_config`` → ``repo_config`` import is
    load-bearing for every production runtime — gateway, orchestrator,
    and sandbox each lay out the file differently. Tests that pass a
    mock for ``config.repo_config.get_repo_role_patterns`` cover the
    *function* but not the *import path*; the prior PR's tests passed
    only because pytest runs with the repo root on ``sys.path``, where
    ``config/`` happens to resolve as a Python namespace package — a
    property that does not hold inside any container.

    These tests simulate two production layouts:

    1. ``config.repo_config`` import succeeds (gateway when ``/app/`` is
       on ``PYTHONPATH`` *and* ``config/__init__.py`` exists alongside
       — or when running from the worktree root in test).
    2. ``config.repo_config`` raises ``ImportError`` and the fallback
       ``from repo_config import …`` succeeds (gateway / orchestrator
       containers, where ``repo_config.py`` lands at the top level).
    """

    def setup_method(self):
        reset_pattern_cache()

    def teardown_method(self):
        reset_pattern_cache()

    def test_falls_back_to_top_level_repo_config_when_config_package_missing(self, monkeypatch):
        # Simulate the gateway's ``/app/repo_config.py`` (no ``config/``
        # package on sys.path). We hide ``config.repo_config`` from
        # ``sys.modules`` and stand up a fake top-level ``repo_config``.
        import sys
        import types

        from agent_restrictions import load_repo_pattern_override

        fake_top_level = types.ModuleType("repo_config")

        def _override(repo: str):
            if repo == "owner/go-repo":
                return {"tests_globs": ["**/*_test.go", "**/testdata/**"]}
            return None

        fake_top_level.get_repo_role_patterns = _override
        monkeypatch.setitem(sys.modules, "repo_config", fake_top_level)
        # Make ``from config.repo_config import ...`` fail. A sentinel
        # module without the symbol triggers ``ImportError`` for a
        # ``from-import`` that asks for the missing name, which is what
        # the production gateway sees when ``/config`` lacks
        # ``__init__.py``.
        broken_config = types.ModuleType("config")
        monkeypatch.setitem(sys.modules, "config", broken_config)
        monkeypatch.delitem(sys.modules, "config.repo_config", raising=False)

        result = load_repo_pattern_override("owner/go-repo")
        assert result == {"tests_globs": ["**/*_test.go", "**/testdata/**"]}

    def test_returns_none_when_neither_module_importable(self, monkeypatch):
        import sys
        import types

        from agent_restrictions import load_repo_pattern_override

        # Hide both import paths.
        broken_config = types.ModuleType("config")
        monkeypatch.setitem(sys.modules, "config", broken_config)
        monkeypatch.delitem(sys.modules, "config.repo_config", raising=False)
        monkeypatch.delitem(sys.modules, "repo_config", raising=False)
        # Block any subsequent import attempt for the top-level fallback.
        original_meta_path = sys.meta_path

        class _Blocker:
            def find_spec(self, name, *_args, **_kwargs):
                if name == "repo_config":
                    raise ImportError("simulated: repo_config not on path")
                return None

        monkeypatch.setattr(sys, "meta_path", [_Blocker(), *original_meta_path])

        # Should return None gracefully, not raise.
        assert load_repo_pattern_override("owner/repo") is None


class TestSandboxEnvVarInjection:
    """The sandbox container has no ``repositories.yaml`` mounted, so
    the orchestrator pre-resolves the override at spawn time and passes
    it via ``EGG_PIPELINE_REPO_PATTERNS_JSON``. These tests pin the
    env-var contract that the sandbox-side ``build_agent_patterns``
    consumes."""

    def setup_method(self):
        reset_pattern_cache()

    def teardown_method(self):
        reset_pattern_cache()

    def test_env_var_short_circuits_filesystem_lookup(self, monkeypatch):
        from agent_restrictions import load_repo_pattern_override

        # Even if the filesystem-side ``get_repo_role_patterns`` says
        # something different, the env-var snapshot wins.
        monkeypatch.setenv(
            "EGG_PIPELINE_REPO_PATTERNS_JSON",
            json.dumps(
                {
                    "owner/go-repo": {
                        "tests_globs": ["**/*_test.go"],
                        "code_globs": ["**/*.go"],
                    }
                }
            ),
        )

        # If filesystem lookup were consulted it would raise; this
        # confirms it isn't reached.
        with patch(
            "config.repo_config.get_repo_role_patterns",
            side_effect=AssertionError("filesystem lookup should not run"),
        ):
            result = load_repo_pattern_override("owner/go-repo")
        assert result == {
            "tests_globs": ["**/*_test.go"],
            "code_globs": ["**/*.go"],
        }

    def test_env_var_for_other_repos_is_ignored(self, monkeypatch):
        from agent_restrictions import load_repo_pattern_override

        # Snapshot only covers a different repo — the requested repo
        # falls through to the filesystem path, which we mock.
        monkeypatch.setenv(
            "EGG_PIPELINE_REPO_PATTERNS_JSON",
            json.dumps({"owner/other-repo": {"tests_globs": ["**/*_test.go"]}}),
        )
        with patch(
            "config.repo_config.get_repo_role_patterns",
            return_value={"tests_globs": ["**/__tests__/"]},
        ):
            result = load_repo_pattern_override("owner/js-repo")
        assert result == {"tests_globs": ["**/__tests__/"]}

    def test_malformed_env_var_does_not_raise(self, monkeypatch, caplog):
        from agent_restrictions import load_repo_pattern_override

        monkeypatch.setenv("EGG_PIPELINE_REPO_PATTERNS_JSON", "not-json{{")
        with patch("config.repo_config.get_repo_role_patterns", return_value=None):
            result = load_repo_pattern_override("owner/repo")
        assert result is None

    def test_pattern_lookup_uses_env_var_override(self, monkeypatch):
        # End-to-end: a sandbox-style env injection alters the registry
        # that ``get_agent_pattern_for_repo`` returns, without any
        # filesystem access.
        monkeypatch.setenv(
            "EGG_PIPELINE_REPO_PATTERNS_JSON",
            json.dumps({"owner/go-repo": {"tests_globs": ["**/*_test.go"]}}),
        )
        coder = get_agent_pattern_for_repo("coder", repo="owner/go-repo")
        assert coder is not None
        # Go same-dir tests are now blocked for coder.
        assert not coder.can_write("internal/foo/bar_test.go")
        # Python tests are no longer special — coder can write them
        # because the override replaced the global tests_globs.
        assert coder.can_write("internal/foo/test_bar.py")


class TestCaseInsensitiveCacheKey:
    """Repo lookups should not produce two cache entries for the same
    repo spelled with different casings (``Owner/Repo`` vs
    ``owner/repo``). Mirrors ``config.repo_config.get_repo_setting``'s
    case-insensitive lookup."""

    def setup_method(self):
        reset_pattern_cache()

    def teardown_method(self):
        reset_pattern_cache()

    def test_mixed_case_resolves_same_object(self, monkeypatch):
        # Stub the override resolver so both casings would *otherwise*
        # see the same data — what we're checking is the cache key.
        monkeypatch.setattr(
            "agent_restrictions.load_repo_pattern_override",
            lambda repo: {"tests_globs": ["**/*_test.go"]},
        )

        a = get_agent_pattern_for_repo("coder", repo="Owner/Repo")
        b = get_agent_pattern_for_repo("coder", repo="owner/repo")
        assert a is b


class TestAtomicCacheReset:
    """``reset_pattern_cache`` must be atomic — a concurrent reader
    must never see a half-empty cache. Atomic rebind of the module
    global guarantees this under CPython's GIL."""

    def test_reset_preserves_default_identity(self):
        reset_pattern_cache()
        # The default-repo entry must point at the module-level
        # ``AGENT_PATTERNS`` (not a fresh registry of identical
        # contents), so callers comparing with ``is`` keep working.
        coder = get_agent_pattern_for_repo("coder", repo=None)
        assert coder is AGENT_PATTERNS["coder"]


class TestConfigValidationLogging:
    """``get_repo_role_patterns`` must emit WARNING logs for every
    dropped key/value so a misconfigured operator gets a signal in the
    logs instead of silently shipping the global default."""

    def test_unknown_key_logs_warning(self, caplog):
        from config.repo_config import get_repo_role_patterns

        with patch(
            "config.repo_config.get_repo_setting",
            return_value={"tests_glob": ["**/*_test.go"]},  # typo
        ):
            with caplog.at_level("WARNING", logger="config.repo_config"):
                result = get_repo_role_patterns("owner/repo")
        assert result is None
        joined = " ".join(r.message for r in caplog.records)
        assert "tests_glob" in joined
        assert "not recognised" in joined or "valid keys" in joined

    def test_non_list_value_logs_warning(self, caplog):
        from config.repo_config import get_repo_role_patterns

        with patch(
            "config.repo_config.get_repo_setting",
            return_value={"tests_globs": 42},
        ):
            with caplog.at_level("WARNING", logger="config.repo_config"):
                result = get_repo_role_patterns("owner/repo")
        assert result is None
        joined = " ".join(r.message for r in caplog.records)
        assert "tests_globs" in joined and "list" in joined

    def test_non_string_entry_logs_warning(self, caplog):
        from config.repo_config import get_repo_role_patterns

        with patch(
            "config.repo_config.get_repo_setting",
            return_value={"tests_globs": ["**/*_test.go", None, ""]},
        ):
            with caplog.at_level("WARNING", logger="config.repo_config"):
                result = get_repo_role_patterns("owner/repo")
        # The valid entry is kept; the None/"" entries are dropped with
        # warnings.
        assert result == {"tests_globs": ["**/*_test.go"]}
        joined = " ".join(r.message for r in caplog.records)
        assert "invalid entry" in joined
