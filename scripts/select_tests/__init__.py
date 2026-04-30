#!/usr/bin/env python3
"""
Changeset-aware test selection for `make test` (issue #1973).

OVERVIEW

`make test` historically ran the full unit-test suite (~356 test files)
on every invocation regardless of what changed on the branch.  This
package narrows that default to the transitive reverse-import closure
of files touched since a Last-Known-Good (LKG) commit, falling back
to the base branch when no LKG exists, and falling back to the FULL
SUITE whenever static analysis cannot be trusted (conftest /
lockfile / workflow changes, non-`.py` changes, dynamic-import
reachability, `shared/tests/` fixture edits, unresolvable baseline,
LKG-not-an-ancestor, gateway/*.py changes, source-file staleness).

Correctness posture: the gate is "never skip a test that exercises a
changed code path".  Any sign of static-analysis fog widens to the
full suite with an explicit trigger reason printed to stderr.

USAGE

    python scripts/select_tests/__main__.py
        Print the selected test file paths, one per line, on stdout.
        Empty stdout means "no tests selected" (callers treat as
        success with zero tests run).

    python scripts/select_tests/__main__.py --why <test_path>
        Print the import chain that selected the given test.

    python scripts/select_tests/__main__.py --record-good [--sha <sha>]
        Atomically write the LKG sidecar to the given sha (default
        HEAD).  Validates the sha (40-hex regex, object exists,
        ancestor-of-HEAD); refuses non-zero on failure.

    python scripts/select_tests/__main__.py --full-suite
        Emit all test directories on stdout.  Used internally by
        `make test-all`.

    python scripts/select_tests/__main__.py --patch-selection-json --head <sha> \\
                            --pytest-ms <int>
        Append `pytest_ms` to the existing
        `.egg-state/selection/<head>.json` record.  Called by the
        Makefile `test` wrapper after pytest returns.

EXIT CODES — fail-open contract

    0  default / --full-suite / --why / --patch-selection-json:
       SUCCESS in all cases except argparse syntax errors.  An
       unhandled exception inside main() is caught, the traceback
       is printed to stderr, the full test-root list is emitted on
       stdout (equivalent to --full-suite), and the process exits
       0.  A selector bug must NEVER block iteration — correctness
       is preserved by widening to the full suite.
    !=0  --record-good only, on validation failure (typo'd sha,
         non-existent sha, non-ancestor sha).  --record-good is a
         pure write operation; silent success on bad input would
         poison LKG, so this single mode is allowed to exit
         non-zero.

PACKAGE LAYOUT (issue #2261)

    Decomposed from a single ``scripts/select_tests.py`` (1,875 lines)
    into a sub-package as the canonical worked reference for the
    file-size-allowlist program.  The decomposition follows the
    pattern documented in ``docs/guides/decomposition-pattern.md``:

      ``__init__.py``   — explicit per-symbol re-export barrel
                          (decision-5).  External callers MUST keep
                          using ``from select_tests import …`` — the
                          barrel is the stable surface (decision-7).
      ``__main__.py``   — path-style entry point used by the
                          Makefile and subprocess-based tests.
      ``_constants.py`` — module-level constants (PACKAGES, regex
                          tables, sidecar paths, stderr notices).
      ``_io.py``        — git, atomic-write, sidecar/LKG, baseline,
                          changed-files helpers.
      ``_graph.py``     — grimp graph + dynamic-import scan + bare-name
                          AST resolver + reverse closure.
      ``_cli.py``       — selection records, fallback triggers,
                          ``--why`` introspection, ``_main_inner`` and
                          fail-open ``main``.

DESIGN REFERENCES

    - Plan: .egg-state/drafts/1973-plan.md (sections "Approach",
      "Architecture", "Risk summary").
    - Decisions: .egg-state/contracts/issue-1973.json (decisions
      d1-d15, feedback Q1-Q16).
    - Risks: .egg-state/agent-outputs/1973-risk_analyst-output.json
      (R1 gateway-importlib mitigation; R2 source-file staleness
      guard; R5 PYTEST_ARGS classifier; R14 read-only role).
    - File-size decomposition pattern: docs/guides/decomposition-pattern.md (#2261).
"""

from __future__ import annotations

# Import the submodules eagerly so monkeypatching via attribute access
# (e.g. ``selector._io._run_git``) works without an explicit submodule
# import in the test file.
from . import _cli, _constants, _graph, _io  # noqa: F401 — re-export targets

# Selection records, fallback evaluator, --why, CLI surface.
from ._cli import (
    _build_arg_parser,
    _fnmatch,
    _format_chain,
    _main_inner,
    _now_iso,
    _run_narrow_or_fallback,
    _short_sha,
    _split_pytest_args_env,
    _strip_pythonpath_from_sys_path,
    emit_full_suite,
    evaluate_fallback_triggers,
    explain_why,
    main,
    patch_selection_record,
    write_selection_record,
)

# Constants — every module-level value the test suite asserts on
# (PACKAGES, paths, glob tables, regex constants, stderr notices).
from ._constants import (
    _SHA_HEX_RE,
    BARE_NAME_STRIP_PREFIXES,
    DYNAMIC_IMPORT_PATTERNS,
    FALLBACK_PATH_PATTERNS,
    GRIMP_CACHE_DIR,
    PACKAGES,
    SELECTION_LOG_DIR,
    SELECTION_SCHEMA_VERSION,
    SIDECAR_DIR,
    SOURCE_PACKAGES,
    SOURCE_ROOTS,
    STDERR_DETACHED_HEAD_NOTICE,
    STDERR_DETACHED_HEAD_RECORD_NOTICE,
    STDERR_READONLY_RECORD_NOTICE,
    TEST_PACKAGES,
    TEST_ROOT_DIRS,
)

# Graph + closure + bare-name resolver + PYTEST_ARGS classifier.
from ._graph import (
    _TEST_ROOT_PREFIXES,
    GraphBundle,
    _enumerate_source_paths,
    _extract_imports,
    _module_to_filesystem_path,
    _scan_dynamic_imports,
    _walk_upstream_combined,
    build_bare_name_index,
    build_bare_name_upstream_edges,
    build_graph,
    is_dynamic_import_touched,
    map_modules_to_test_files,
    pytest_args_have_explicit_path,
    reverse_closure,
)

# Git, sidecar, baseline, and changed-files helpers.
from ._io import (
    RecordGoodValidationError,
    _atomic_write_text,
    _git_current_branch,
    _git_is_ancestor,
    _git_object_exists,
    _git_repo_root,
    _is_valid_sha,
    _log,
    _resolve_root,
    _run_git,
    _sidecar_path,
    changed_files,
    is_role_readonly,
    lkg_is_stale,
    path_to_module,
    read_sidecar_lkg,
    record_good,
    resolve_baseline,
    write_sidecar_lkg,
)

# The set of names re-exported from this package.  Listed explicitly
# (decision-5) so any drift between the submodule layout and the
# external surface is visible at code-review time.  Underscore-
# prefixed entries are private helpers tests reference directly
# (``selector._extract_imports``, ``selector._main_inner`` etc.); the
# package-private convention is preserved at the call site, but the
# barrel re-exports them so test patches and reads against the
# package object still resolve.
__all__ = (
    # Constants (public)
    "BARE_NAME_STRIP_PREFIXES",
    "DYNAMIC_IMPORT_PATTERNS",
    "FALLBACK_PATH_PATTERNS",
    "GRIMP_CACHE_DIR",
    "PACKAGES",
    "SELECTION_LOG_DIR",
    "SELECTION_SCHEMA_VERSION",
    "SIDECAR_DIR",
    "SOURCE_PACKAGES",
    "SOURCE_ROOTS",
    "STDERR_DETACHED_HEAD_NOTICE",
    "STDERR_DETACHED_HEAD_RECORD_NOTICE",
    "STDERR_READONLY_RECORD_NOTICE",
    "TEST_PACKAGES",
    "TEST_ROOT_DIRS",
    "_SHA_HEX_RE",
    # I/O helpers (public + private)
    "RecordGoodValidationError",
    "_atomic_write_text",
    "_git_current_branch",
    "_git_is_ancestor",
    "_git_object_exists",
    "_git_repo_root",
    "_is_valid_sha",
    "_log",
    "_resolve_root",
    "_run_git",
    "_sidecar_path",
    "changed_files",
    "is_role_readonly",
    "lkg_is_stale",
    "path_to_module",
    "read_sidecar_lkg",
    "record_good",
    "resolve_baseline",
    "write_sidecar_lkg",
    # Graph helpers (public + private)
    "GraphBundle",
    "_TEST_ROOT_PREFIXES",
    "_enumerate_source_paths",
    "_extract_imports",
    "_module_to_filesystem_path",
    "_scan_dynamic_imports",
    "_walk_upstream_combined",
    "build_bare_name_index",
    "build_bare_name_upstream_edges",
    "build_graph",
    "is_dynamic_import_touched",
    "map_modules_to_test_files",
    "pytest_args_have_explicit_path",
    "reverse_closure",
    # CLI helpers (public + private)
    "_build_arg_parser",
    "_fnmatch",
    "_format_chain",
    "_main_inner",
    "_now_iso",
    "_run_narrow_or_fallback",
    "_short_sha",
    "_split_pytest_args_env",
    "_strip_pythonpath_from_sys_path",
    "emit_full_suite",
    "evaluate_fallback_triggers",
    "explain_why",
    "main",
    "patch_selection_record",
    "write_selection_record",
)


# NOTE: there is intentionally no ``if __name__ == "__main__":`` block
# here.  ``__init__.py`` is loaded as a module under the package's
# import name, never as a script — Python's ``-m`` flag would still
# resolve ``__main__.py`` as the entry point.  Path-style invocations
# go through ``scripts/select_tests/__main__.py``, which knows how to
# manipulate ``sys.path`` so the package import resolves correctly.
