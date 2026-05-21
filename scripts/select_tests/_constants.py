"""Module-level constants for the changeset-aware test selector.

Everything here is data — no side effects, no functions — so this
file remains parse-cheap and ruff/mypy-friendly even on cold cache.

The package's ``__init__.py`` eagerly imports every submodule so
test code can reach the ``selector._io._run_git`` / ``selector._cli._main_inner``
attribute paths needed for monkeypatching internal helpers; that
means ``import select_tests`` does pull in the I/O / graph / CLI
code paths regardless of which symbols the consumer actually
references.  Both production code and the test suite read from
this module through the package's re-export barrel.
"""

from __future__ import annotations

import re
from pathlib import Path

# ----------------------------------------------------------------------
# PACKAGES — single source of truth for grimp graph construction.
#
# grimp only reports edges between modules it has been told about, so
# omitting test roots would make the test-file mapping return an empty
# intersection and `make test` would always print "no tests selected".
#
# Both source AND test packages are registered here.  The TASK-5-4
# monorepo test asserts that EVERY test_*.py under the four test roots
# is a node in the graph built from this constant — a runtime + CI
# guard against drift.
# ----------------------------------------------------------------------

SOURCE_PACKAGES: tuple[str, ...] = (
    "gateway",
    "orchestrator",
    "sandbox",
    "shared.egg_agent",
    "shared.egg_anchor",
    "shared.egg_config",
    "shared.egg_container",
    "shared.egg_contracts",
    "shared.egg_git",
    "shared.egg_health",
    "shared.egg_logging",
    "shared.egg_orchestrator",
    "shared.egg_overseer",
    "shared.egg_restrictions",
)

TEST_PACKAGES: tuple[str, ...] = (
    "tests",
    "gateway.tests",
    "orchestrator.tests",
    "shared.tests",
)

PACKAGES: tuple[str, ...] = SOURCE_PACKAGES + TEST_PACKAGES

# Source-root directories the runtime staleness guard (R2 mitigation,
# task-2-3) walks to confirm every .py file is a node in the grimp
# graph.  Excludes test directories (those are walked separately).
SOURCE_ROOTS: tuple[str, ...] = ("gateway", "orchestrator", "sandbox", "shared")

# Top-level prefixes to strip when synthesising a "bare-name" view of
# every fully-qualified module id.  Mirrors the sys.path injections
# done in `build_graph` (and the matching conftest.py setups): every
# directory listed here is at the head of sys.path at test time, so a
# file like `shared/egg_logging/signatures.py` is reachable both as
# `shared.egg_logging.signatures` (the form grimp registers under
# `PACKAGES`) AND as `egg_logging.signatures` (the form virtually
# every test/production file in the repo actually writes — verified
# 406/407 test files and 33/33 sampled production files use bare
# names).  The AST resolver in `build_bare_name_upstream_edges` uses
# this list to bridge that gap so changeset-aware narrowing can
# follow test→production edges in a codebase that grimp alone cannot
# trace.
#
# `gateway.` is included even though `gateway/` is NOT on sys.path
# during `build_graph`: gateway tests reach production through
# `gateway/tests/conftest.py`'s `_load_module_with_replaced_imports`
# (importlib `spec_from_file_location`), which makes every gateway
# production module importable by its bare name (`from policy import
# X`, `import auth`).  The AST resolver only inspects source — it
# does not import — so the runtime importlib pattern does not affect
# its view.  Adding `gateway.` here lets the resolver record the
# test→production edges that grimp cannot see, replacing the
# previous blanket "any `gateway/*.py` edit widens to full suite"
# fallback.
#
# Important: parity with `shared.`/`orchestrator.`/`sandbox.` requires
# that the dynamic-import seed set (R6, `dynamic-import reachability`)
# does not also pull every gateway production module back into a
# full-suite fallback.  Because `_scan_dynamic_imports` source-greps
# for `__import__(`, `importlib.util.spec_from_file_location`, etc.,
# any gateway module that contains those primitives becomes a seed —
# and `is_dynamic_import_touched` then widens for every module in
# that seed's `find_upstream_modules` closure.  When `gateway.gateway`
# itself was a seed (its source contained `__import__(module_name)`
# and `__import__("threading")`), the closure spanned ~32 of the 41
# gateway production modules, so R6 fired in place of the deleted
# R1 — narrowing in name only.  The fix is to keep the dynamic-import
# primitives in `gateway/_module_loader.py` (a leaf bootstrap that
# imports only stdlib), so its upstream closure is empty and R6 only
# fires when the loader itself is edited.  See
# `tests/tools/test_select_tests_fallbacks.py::
# test_gateway_source_change_does_not_widen_with_module_loader_seed`
# for the regression pin and
# `tests/tools/test_select_tests_fallbacks.py::
# test_gateway_source_change_widens_if_gateway_gateway_becomes_seed`
# for the failure mode being guarded against.
BARE_NAME_STRIP_PREFIXES: tuple[str, ...] = (
    "shared.",
    "orchestrator.",
    "sandbox.tools.",  # checked before "sandbox." so the longer prefix wins
    "sandbox.",
    "gateway.",
)

# Test-root directories (relative paths) the selector emits when
# falling back to the full suite OR when the user invokes
# --full-suite explicitly.
TEST_ROOT_DIRS: tuple[str, ...] = (
    "tests",
    "gateway/tests",
    "orchestrator/tests",
    "shared/tests",
)

# Paths that, when changed, force a full-suite fallback regardless of
# import-graph reachability.  Glob-style; matched on the repo-relative
# changed path.
FALLBACK_PATH_PATTERNS: tuple[tuple[str, str], ...] = (
    # (glob, trigger-string)
    ("Makefile", "Makefile changed"),
    ("pyproject.toml", "pyproject.toml changed"),
    ("uv.lock", "uv.lock changed"),
    (".python-version", ".python-version changed"),
    (".github/workflows/test.yml", ".github/workflows/test.yml changed"),
)

# Regex patterns scanned during graph construction to mark modules as
# containing dynamic-import primitives (decision-10).  When a changed
# module is in or reverse-reachable from this set, fall back to the
# full suite with trigger "dynamic-import reachability".
DYNAMIC_IMPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bimportlib\.import_module\b"),
    re.compile(r"\bimportlib\.util\.spec_from_file_location\b"),
    re.compile(r"\bimportlib\.util\.module_from_spec\b"),
    re.compile(r"\bimportlib\.machinery\."),
    re.compile(r"\bSourceFileLoader\b"),
    # `__import__(` as a token anywhere in the file.  The leading `\b`
    # avoids matching `_my__import__variable` substrings; the un-anchored
    # form picks up real callers like `mod = __import__(...)` and
    # `_X = __import__("re").compile(...)` (reviewer_code blocking #3).
    re.compile(r"\b__import__\s*\("),
    # Entry-point plugin loading (importlib.metadata).
    re.compile(r"\bimportlib\.metadata\.entry_points\b"),
    re.compile(r"\bpkg_resources\.iter_entry_points\b"),
)

# Sidecar / log file locations.
SIDECAR_DIR = Path(".egg-state/last-known-good")
SELECTION_LOG_DIR = Path(".egg-state/selection")
GRIMP_CACHE_DIR = Path(".egg-state/grimp-cache")

# Selection-record schema version.  Bump only on backward-incompatible
# changes to the per-invocation JSON envelope.
SELECTION_SCHEMA_VERSION = 1

# Stderr notice strings — kept here as constants so tests can match
# verbatim rather than fishing through fuzzy substrings.
STDERR_DETACHED_HEAD_NOTICE = (
    "select-tests: detached HEAD; using base branch baseline, sidecar reads/writes skipped"
)
STDERR_DETACHED_HEAD_RECORD_NOTICE = "select-tests: detached HEAD; sidecar write skipped"
STDERR_READONLY_RECORD_NOTICE = "select-tests: read-only role; sidecar write skipped"

# Compiled regex for 40-char lower-case hex sha validation.
_SHA_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


__all__ = (
    "PACKAGES",
    "SOURCE_PACKAGES",
    "TEST_PACKAGES",
    "SOURCE_ROOTS",
    "TEST_ROOT_DIRS",
    "BARE_NAME_STRIP_PREFIXES",
    "FALLBACK_PATH_PATTERNS",
    "DYNAMIC_IMPORT_PATTERNS",
    "SIDECAR_DIR",
    "SELECTION_LOG_DIR",
    "GRIMP_CACHE_DIR",
    "SELECTION_SCHEMA_VERSION",
    "STDERR_DETACHED_HEAD_NOTICE",
    "STDERR_DETACHED_HEAD_RECORD_NOTICE",
    "STDERR_READONLY_RECORD_NOTICE",
)
