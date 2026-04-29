#!/usr/bin/env python3
"""
Changeset-aware test selection for `make test` (issue #1973).

OVERVIEW

`make test` historically ran the full unit-test suite (~356 test files)
on every invocation regardless of what changed on the branch.  This
script narrows that default to the transitive reverse-import closure
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

    scripts/select_tests.py
        Print the selected test file paths, one per line, on stdout.
        Empty stdout means "no tests selected" (callers treat as
        success with zero tests run).

    scripts/select_tests.py --why <test_path>
        Print the import chain that selected the given test.

    scripts/select_tests.py --record-good [--sha <sha>]
        Atomically write the LKG sidecar to the given sha (default
        HEAD).  Validates the sha (40-hex regex, object exists,
        ancestor-of-HEAD); refuses non-zero on failure.

    scripts/select_tests.py --full-suite
        Emit all test directories on stdout.  Used internally by
        `make test-all`.

    scripts/select_tests.py --patch-selection-json --head <sha> \
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

DESIGN REFERENCES

    - Plan: .egg-state/drafts/1973-plan.md (sections "Approach",
      "Architecture", "Risk summary").
    - Decisions: .egg-state/contracts/issue-1973.json (decisions
      d1-d15, feedback Q1-Q16).
    - Risks: .egg-state/agent-outputs/1973-risk_analyst-output.json
      (R1 gateway-importlib mitigation; R2 source-file staleness
      guard; R5 PYTEST_ARGS classifier; R14 read-only role).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    "shared.egg_harness",
    "shared.egg_harness_integration",
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
# `gateway.` is intentionally absent: `gateway/` is NOT on sys.path
# during `build_graph` (the importlib test-loader pattern in
# `gateway/tests/conftest.py` would shadow grimp's view), and
# `gateway/*.py` changes are handled by their own dedicated widening
# trigger.
BARE_NAME_STRIP_PREFIXES: tuple[str, ...] = (
    "shared.",
    "orchestrator.",
    "sandbox.tools.",  # checked before "sandbox." so the longer prefix wins
    "sandbox.",
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

# Re-export all module-level constants for the test suite.
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


# ----------------------------------------------------------------------
# Tiny helpers
# ----------------------------------------------------------------------

_SHA_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


def _log(msg: str) -> None:
    """Write a stderr line — kept centralised so tests can monkeypatch."""
    print(msg, file=sys.stderr)


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run `git <args>`; return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _is_valid_sha(s: str) -> bool:
    return bool(_SHA_HEX_RE.match(s.strip()))


def _git_object_exists(sha: str, cwd: Path | None = None) -> bool:
    rc, _, _ = _run_git(["cat-file", "-e", sha], cwd=cwd)
    return rc == 0


def _git_is_ancestor(sha: str, descendant: str = "HEAD", cwd: Path | None = None) -> bool:
    rc, _, _ = _run_git(["merge-base", "--is-ancestor", sha, descendant], cwd=cwd)
    return rc == 0


def _git_current_branch(cwd: Path | None = None) -> str | None:
    """Return the current branch name, or None on detached HEAD.

    Uses `git rev-parse --abbrev-ref HEAD` (not `git symbolic-ref`)
    because the egg gateway sidecar blocks `symbolic-ref` on agent
    sandboxes (allowlist enforcement).  `rev-parse --abbrev-ref HEAD`
    returns the literal string `HEAD` on detached HEAD, which we
    canonicalise to None.
    """
    rc, stdout, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if rc != 0:
        return None
    name = stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def _git_repo_root(cwd: Path | None = None) -> Path:
    rc, stdout, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if rc != 0:
        return Path.cwd()
    return Path(stdout.strip())


# ----------------------------------------------------------------------
# Read-only role detection (Q13 / R14)
# ----------------------------------------------------------------------


def is_role_readonly(repo_root: Path | None = None) -> bool:
    """Return True iff the current sandbox is in a read-only role.

    Two signals (either fires the read-only path):
      - EGG_AGENT_ROLE env var starts with `reviewer_` or equals `refiner`.
      - `.egg-readonly` marker file present in the repo root (sandbox
        primitive — catches read-only sandboxes launched without the
        env var set, per risk_analyst R14).

    When EGG_AGENT_ROLE is unset or names a writer role (coder, tester,
    documenter, planner, anything else), and no marker is present,
    returns False — the LKG-preferred path applies.
    """
    role = os.environ.get("EGG_AGENT_ROLE", "")
    if role.startswith("reviewer_") or role == "refiner":
        return True
    root = repo_root if repo_root is not None else _git_repo_root()
    if (root / ".egg-readonly").exists():
        return True
    return False


# ----------------------------------------------------------------------
# Atomic file I/O
# ----------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via tempfile + os.replace.

    A concurrent reader will either see the previous content or the new
    content, never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        # On any error, clean up the tempfile so we don't litter
        # `.tmp` artifacts in the sidecar dir.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ----------------------------------------------------------------------
# Sidecar LKG I/O (TASK-2-4a)
# ----------------------------------------------------------------------


def _resolve_root(repo_root: Path | None) -> Path:
    """Default repo_root to the resolved git toplevel when callers
    don't pass one explicitly.  Centralised so every sidecar I/O
    call site goes through the same fallback (tester blocking #2)."""
    return repo_root if repo_root is not None else _git_repo_root()


def _sidecar_path(branch: str, repo_root: Path | None = None) -> Path:
    return _resolve_root(repo_root) / SIDECAR_DIR / f"{branch}.sha"


def read_sidecar_lkg(branch: str | None, repo_root: Path | None = None) -> str | None:
    """Read the LKG sidecar for `branch`; return the sha or None.

    Returns None if:
      - branch is None (detached HEAD)
      - sidecar file is missing
      - sidecar contents fail the 40-hex regex (treated as absent;
        matches "no LKG" semantics).

    `repo_root` defaults to the git toplevel — passing None when the
    caller is running from a non-repo-root CWD would previously
    write/read under the wrong directory (tester blocking #2).
    """
    if branch is None:
        return None
    path = _sidecar_path(branch, repo_root)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _is_valid_sha(content):
        return None
    return content


def write_sidecar_lkg(branch: str, sha: str, repo_root: Path | None = None) -> None:
    """Atomically write `sha` to the LKG sidecar for `branch`."""
    _atomic_write_text(_sidecar_path(branch, repo_root), sha + "\n")


# ----------------------------------------------------------------------
# --record-good implementation (TASK-2-4a)
# ----------------------------------------------------------------------


class RecordGoodValidationError(Exception):
    """Raised when --record-good cannot proceed.  The CLI converts to exit !=0."""


def record_good(sha_arg: str | None, repo_root: Path | None = None) -> int:
    """Implement `--record-good [--sha <sha>]`.

    Returns 0 on success or skip-with-notice (detached HEAD, read-only
    role, missing branch).  Raises RecordGoodValidationError on a typo'd
    sha (regex/cat-file/ancestor failure); the caller converts that to
    exit 1.
    """
    if is_role_readonly(repo_root):
        _log(STDERR_READONLY_RECORD_NOTICE)
        return 0

    branch = _git_current_branch(cwd=repo_root)
    if branch is None:
        _log(STDERR_DETACHED_HEAD_RECORD_NOTICE)
        return 0

    # Determine the sha being recorded.  When --sha is omitted, default
    # to HEAD — but resolve HEAD to its 40-char form so the sidecar
    # contents are always normalised.
    if sha_arg is None:
        rc, stdout, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
        if rc != 0:
            raise RecordGoodValidationError("could not resolve HEAD to a sha")
        sha = stdout.strip()
    else:
        sha = sha_arg.strip()

    # (a) regex
    if not _is_valid_sha(sha):
        raise RecordGoodValidationError(f"sha is not 40 lowercase hex chars: {sha!r}")
    # (b) object exists
    if not _git_object_exists(sha, cwd=repo_root):
        raise RecordGoodValidationError(f"sha {sha} not found in object database")
    # (c) ancestor of HEAD
    if not _git_is_ancestor(sha, "HEAD", cwd=repo_root):
        raise RecordGoodValidationError(f"sha {sha} is not an ancestor of HEAD")

    write_sidecar_lkg(branch, sha, repo_root=repo_root)
    return 0


# ----------------------------------------------------------------------
# Baseline resolution + diff (TASK-2-2)
# ----------------------------------------------------------------------


def resolve_baseline(
    repo_root: Path | None = None,
    base_branch: str | None = None,
) -> tuple[str | None, str, str | None]:
    """Resolve the diff baseline.

    Returns (baseline_sha, source, branch):
      - baseline_sha: 40-char sha or None when unresolvable.
      - source: one of "LKG", "BASE_BRANCH", "UNRESOLVABLE".
      - branch: current branch name, or None on detached HEAD.

    Resolution order (Q13 / R14):
      1. If read-only role (EGG_AGENT_ROLE starts with reviewer_, equals
         refiner, or `.egg-readonly` marker present) → SKIP sidecar
         entirely; proceed to base-branch.
      2. Else: try `.egg-state/last-known-good/<branch>.sha`; accept
         only if 40-hex AND ancestor-of-HEAD.
      3. Else: `git merge-base HEAD origin/<base_branch>` (default
         BASE_BRANCH env var → fallback to "main").
      4. Else: UNRESOLVABLE → caller widens to full suite.
    """
    branch = _git_current_branch(cwd=repo_root)
    if branch is None:
        _log(STDERR_DETACHED_HEAD_NOTICE)

    readonly = is_role_readonly(repo_root)

    # (1) LKG sidecar — skipped on read-only role.
    if not readonly and branch is not None:
        sidecar_sha = read_sidecar_lkg(branch, repo_root=repo_root)
        if sidecar_sha is not None and _git_is_ancestor(sidecar_sha, "HEAD", cwd=repo_root):
            return sidecar_sha, "LKG", branch
        # Sidecar exists but fails ancestry — caller will surface this
        # as the "LKG not ancestor of HEAD" trigger via diff-side logic;
        # we fall through here so the trigger comes from the same place
        # as any other "use base branch" path.

    # (2) Base branch.
    base = base_branch or os.environ.get("BASE_BRANCH", "main")
    rc, stdout, _ = _run_git(["merge-base", "HEAD", f"origin/{base}"], cwd=repo_root)
    if rc != 0:
        return None, "UNRESOLVABLE", branch
    base_sha = stdout.strip()
    if not _is_valid_sha(base_sha):
        return None, "UNRESOLVABLE", branch
    return base_sha, "BASE_BRANCH", branch


def lkg_is_stale(repo_root: Path | None = None) -> bool:
    """Return True iff the sidecar exists for the current branch but
    its sha is NOT an ancestor of HEAD (force-push / reset case)."""
    if is_role_readonly(repo_root):
        return False
    branch = _git_current_branch(cwd=repo_root)
    if branch is None:
        return False
    sidecar_sha = read_sidecar_lkg(branch, repo_root=repo_root)
    if sidecar_sha is None:
        return False
    return not _git_is_ancestor(sidecar_sha, "HEAD", cwd=repo_root)


def changed_files(baseline_sha: str, repo_root: Path | None = None) -> list[str]:
    """Return the union of committed-since-baseline + uncommitted files.

    Uncommitted changes ALWAYS participate; a dirty tree cannot have a
    clean LKG effect.  Paths are repo-relative POSIX strings.
    """
    diff_paths: set[str] = set()

    rc, stdout, _ = _run_git(["diff", "--name-only", f"{baseline_sha}...HEAD"], cwd=repo_root)
    if rc == 0:
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                diff_paths.add(line)

    rc, stdout, _ = _run_git(["status", "--porcelain"], cwd=repo_root)
    if rc == 0:
        for line in stdout.splitlines():
            # `git status --porcelain` lines are `XY <path>` where XY
            # is a 2-char status code followed by a space and the path.
            # Renames look like `R  old -> new`; we want both.
            if len(line) < 4:
                continue
            payload = line[3:]
            if " -> " in payload:
                old, new = payload.split(" -> ", 1)
                diff_paths.add(old.strip())
                diff_paths.add(new.strip())
            else:
                diff_paths.add(payload.strip())

    return sorted(diff_paths)


# ----------------------------------------------------------------------
# Path → grimp module resolution (TASK-2-3 helper)
# ----------------------------------------------------------------------


def path_to_module(path: str) -> str | None:
    """Resolve a repo-relative path to a grimp-compatible module id.

    Returns None when the path cannot be mapped (caller treats as a
    fallback trigger).
    """
    if not path.endswith(".py"):
        return None
    p = Path(path)
    # Drop leading "./" if present.
    parts = p.with_suffix("").parts
    if not parts:
        return None
    # Special case: `__init__.py` — module is the parent package.
    if parts[-1] == "__init__":
        parts = parts[:-1]
        if not parts:
            return None
    # gateway/foo.py → "gateway.foo"
    # shared/egg_config/bar.py → "shared.egg_config.bar"
    # tests/test_x.py → "tests.test_x"
    # orchestrator/tests/conftest.py → "orchestrator.tests.conftest"
    return ".".join(parts)


# ----------------------------------------------------------------------
# Grimp graph construction (TASK-2-1) + dynamic-import scan (TASK-2-3)
# ----------------------------------------------------------------------


class GraphBundle:
    """Bundles a built grimp graph with derived analysis sets.

    Held attributes:
      - graph:                  grimp.adaptors.graph-like object.
      - all_modules:            set[str] — every node in the graph.
      - all_test_modules:       set[str] — `test_*.py` nodes only.
      - dynamic_import_modules: set[str] — modules whose source contains
                                a dynamic-import primitive (regex match
                                during construction).
      - missing_source_paths:   list[str] — repo-relative .py paths under
                                SOURCE_ROOTS that did NOT resolve to a
                                graph node (R2 staleness guard).
      - bare_name_upstream:     dict[str, set[str]] — reverse-edge map
                                produced by the AST resolver.  Keys are
                                fully-qualified production modules; the
                                set is the modules that import that
                                production module via bare name.
                                Supplements grimp for the 406/407 test
                                files that use the bare-name pattern.
    """

    def __init__(
        self,
        graph: Any,
        all_modules: set[str],
        all_test_modules: set[str],
        dynamic_import_modules: set[str],
        missing_source_paths: list[str],
        bare_name_upstream: dict[str, set[str]] | None = None,
    ) -> None:
        self.graph = graph
        self.all_modules = all_modules
        self.all_test_modules = all_test_modules
        self.dynamic_import_modules = dynamic_import_modules
        self.missing_source_paths = missing_source_paths
        self.bare_name_upstream = bare_name_upstream if bare_name_upstream is not None else {}


def _enumerate_source_paths(repo_root: Path) -> Iterable[Path]:
    """Walk SOURCE_ROOTS yielding every `.py` file outside test dirs.

    Skips `__pycache__`, `.venv`, and any directory named `tests`.
    """
    for root_name in SOURCE_ROOTS:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "__pycache__" in parts or ".venv" in parts or "tests" in parts:
                continue
            yield path.relative_to(repo_root)


def _scan_dynamic_imports(graph: Any, repo_root: Path) -> set[str]:
    """Walk every module in the graph; mark those whose source matches
    any of DYNAMIC_IMPORT_PATTERNS."""
    marked: set[str] = set()
    # grimp graphs expose .modules as an iterable of module ids.
    try:
        modules = list(graph.modules)
    except AttributeError:
        return marked
    for module in modules:
        # grimp supports get_metadata / find_module — we just resolve
        # the source path manually since both APIs vary across versions.
        rel_path = module.replace(".", os.sep) + ".py"
        candidate = repo_root / rel_path
        if not candidate.exists():
            # Try the package __init__ form.
            candidate = repo_root / module.replace(".", os.sep) / "__init__.py"
            if not candidate.exists():
                continue
        try:
            source = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in DYNAMIC_IMPORT_PATTERNS:
            if pattern.search(source):
                marked.add(module)
                break
    return marked


# ----------------------------------------------------------------------
# Bare-name AST resolver
#
# This codebase imports in-repo modules by bare name almost universally
# (`from action_guards import …`, `from egg_logging.signatures import …`),
# leaning on the sys.path injections in `build_graph` and the various
# `conftest.py` files.  Grimp registers production modules under their
# fully-qualified names (`orchestrator.action_guards`,
# `shared.egg_logging.signatures`), so the bare-name imports resolve
# to "external" nodes that grimp filters out — the test→production
# edges become invisible and changeset-aware narrowing widens to the
# full suite for nearly every source change.
#
# The resolver below supplements grimp by AST-scanning every file in
# the graph, mapping each bare-name import target to the set of
# fully-qualified production modules it could resolve to (using the
# same prefix-stripping rules grimp's sys.path injections imply), and
# emitting a reverse-edge map keyed on the FQ production module.
# `reverse_closure` then walks both grimp's transitive closure AND
# these synthetic edges.  Failures (SyntaxError, OSError) are
# swallowed per the fail-open contract — an unscanable file simply
# contributes no extra edges.
# ----------------------------------------------------------------------


def _module_to_filesystem_path(module: str, repo_root: Path) -> Path | None:
    """Inverse of `path_to_module`: given a grimp module id, return
    the source file path (leaf module `.py` or package `__init__.py`).
    Returns None when neither candidate exists on disk."""
    leaf = repo_root / (module.replace(".", os.sep) + ".py")
    if leaf.is_file():
        return leaf
    init = repo_root / module.replace(".", os.sep) / "__init__.py"
    if init.is_file():
        return init
    return None


def _extract_imports(tree: ast.Module) -> set[str]:
    """Yield every top-level absolute-import target from `tree`.

    For ``import X`` we yield ``X``.  For ``import X.Y.Z`` we yield
    every dotted prefix — ``X``, ``X.Y``, AND ``X.Y.Z`` — because
    Python's import machinery actually loads each parent package on
    the way down, so a change to ``X/__init__.py`` is a real
    dependency of any module that does ``import X.Y.Z`` (not just
    ones that do ``import X`` directly).

    For ``from X import Y`` we yield BOTH ``X`` (because the parent
    package is loaded) AND ``X.Y`` (because Y may itself be a
    submodule — common in this repo, e.g. ``from egg_logging.signatures
    import …`` or ``from egg_logging import signatures``).

    Relative imports (``from . import …``) are skipped — grimp already
    sees those because they preserve the package context.

    Star imports yield only the parent (no per-name expansion possible).
    """
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name:
                    continue
                # Add every dotted prefix so a change to the parent
                # package's __init__.py reaches importers of a deeper
                # submodule.  ``import a.b.c`` -> {"a", "a.b", "a.b.c"}.
                parts = alias.name.split(".")
                for i in range(1, len(parts) + 1):
                    targets.add(".".join(parts[:i]))
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                continue
            if node.module is None:
                continue
            targets.add(node.module)
            for alias in node.names:
                if alias.name and alias.name != "*":
                    targets.add(f"{node.module}.{alias.name}")
    return targets


def build_bare_name_index(all_modules: set[str]) -> dict[str, set[str]]:
    """Map every plausible import name back to the FQ production modules
    it could resolve to.

    Each FQ module appears under its own name (a no-op self-lookup) and,
    for every prefix in `BARE_NAME_STRIP_PREFIXES` it starts with, also
    under the prefix-stripped form.  Test modules are excluded because
    bare-name-importing a test module is not a pattern in this repo
    (and we only need the resolver to bridge test→production edges).

    A bare-name string with multiple FQ candidates is over-included by
    the closure (safer than under-narrowing).
    """
    test_prefixes = tuple(t + "." for t in TEST_PACKAGES)
    index: dict[str, set[str]] = {}
    for fq in all_modules:
        if fq in TEST_PACKAGES or any(fq.startswith(p) for p in test_prefixes):
            continue
        index.setdefault(fq, set()).add(fq)
        # Policy: record EVERY applicable prefix-stripped view (not
        # just the longest match).  Both `sandbox.` and `sandbox.tools.`
        # may apply to `sandbox.tools.foo`, and either short form
        # (`tools.foo` or `foo`) is a valid runtime bare-name import
        # in this repo — the resolver records both so the closure
        # widens through whichever shape an importer wrote.  Ambiguity
        # here over-includes consumers, which is the safer side of the
        # narrow-vs-widen trade-off.
        for prefix in BARE_NAME_STRIP_PREFIXES:
            if fq.startswith(prefix):
                bare = fq[len(prefix) :]
                if bare:
                    index.setdefault(bare, set()).add(fq)
    return index


def build_bare_name_upstream_edges(all_modules: set[str], repo_root: Path) -> dict[str, set[str]]:
    """Return a reverse-edge map ``{fq_production_module: {importer, …}}``
    derived from an AST scan of every module's source file.

    The map captures bare-name imports that grimp does NOT see because
    the imported short name is not a registered top-level package.
    Self-edges are dropped.  Imports that don't resolve to any FQ
    production module are silently ignored (likely third-party).

    Errors during file read or AST parse are swallowed — the resolver
    fails open like the rest of the selector.
    """
    leaf_index = build_bare_name_index(all_modules)
    upstream: dict[str, set[str]] = {}

    for module in all_modules:
        source_path = _module_to_filesystem_path(module, repo_root)
        if source_path is None:
            continue
        try:
            source = source_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(source_path))
        except (SyntaxError, OSError, ValueError):
            # ValueError covers null-byte source etc.
            continue
        for imported in _extract_imports(tree):
            for fq in leaf_index.get(imported, ()):
                if fq == module:
                    continue
                upstream.setdefault(fq, set()).add(module)
    return upstream


def _walk_upstream_combined(bundle: GraphBundle, seeds: Iterable[str]) -> set[str]:
    """BFS over importers of every seed, combining grimp's transitive
    closure (`find_downstream_modules`, which in grimp's terminology
    means consumers — modules that import the given module) with the
    AST resolver's bare-name reverse edges.

    Returns the set of every module reachable from any seed via either
    edge source, including the seeds themselves.
    """
    closure: set[str] = set(seeds)
    frontier: set[str] = set(closure)
    while frontier:
        module = frontier.pop()
        try:
            grimp_consumers = bundle.graph.find_downstream_modules(module, as_package=False)
        except Exception:  # noqa: BLE001 — fail-open
            grimp_consumers = set()
        for c in grimp_consumers:
            if c not in closure:
                closure.add(c)
                frontier.add(c)
        for c in bundle.bare_name_upstream.get(module, ()):
            if c not in closure:
                closure.add(c)
                frontier.add(c)
    return closure


def build_graph(repo_root: Path | None = None, packages: tuple[str, ...] = PACKAGES) -> GraphBundle:
    """Construct the grimp graph + derived sets.

    Configures grimp's on-disk cache via `cache_dir=GRIMP_CACHE_DIR` so
    successive sandbox invocations reuse the warm graph.  The caller's
    fail-open wrapper handles ImportError / build failures.
    """
    import grimp  # imported lazily so the fail-open wrapper catches its absence

    root = repo_root if repo_root is not None else _git_repo_root()

    # Ensure the cache dir exists and is rooted at the repo (relative
    # paths get resolved against $CWD which may differ from the
    # repo root in subagent contexts).
    cache_dir = root / GRIMP_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    # The grimp Python entrypoint accepts:
    #   build_graph(*packages, include_external_packages=False,
    #               cache_dir=str)
    # We invoke it from the repo root so bare-name imports
    # (orchestrator/tests/conftest.py:22-29) resolve.
    cwd = os.getcwd()
    try:
        os.chdir(str(root))
        # Mirror the per-conftest sys.path injections so grimp can resolve
        # bare-name imports the same way Python does at test time.
        # Without this, imports like `from models import ...` (orchestrator)
        # or `from egg_lib.config import ...` (sandbox) are filtered as
        # external-by-`include_external_packages=False`, leaving the graph
        # without test→production edges and silently selecting zero tests
        # for changes under those source roots (reviewer_code blocking
        # finding #1).
        #
        # Source of truth for each entry:
        #   - root                       — top-level packages (`tests.*`,
        #                                  `gateway.tests.*`, `orchestrator.*`,
        #                                  `sandbox.*`).
        #   - root / "shared"            — `egg_config`, `egg_logging`, etc.
        #                                  (matches Makefile's PYTHONPATH and
        #                                  `tests/conftest.py:13`).
        #   - root / "orchestrator"      — bare-name imports inside
        #                                  orchestrator/*.py and
        #                                  orchestrator/tests/test_*.py
        #                                  (matches
        #                                  `orchestrator/tests/conftest.py:25-29`).
        #   - root / "sandbox"           — `from egg_lib.* import ...` used
        #                                  throughout sandbox/ and tests/
        #                                  (matches `tests/conftest.py:14`).
        #   - root / "sandbox" / "tools" — `from egg_agent_tools.* import ...`
        #                                  (matches `tests/conftest.py:15`).
        #   - root / "config"            — `import host_config` etc.
        #                                  (matches `tests/conftest.py:16`).
        #
        # Asymmetry note: an externally-set ``PYTHONPATH=shared:...``
        # makes grimp abort with ``NotATopLevelModule: shared.egg_agent``
        # because ``egg_agent`` becomes reachable as both
        # ``shared.egg_agent`` (registered in PACKAGES) and a bare
        # top-level ``egg_agent`` via ``shared/``.  The defense-in-depth
        # scrub at the top of ``main()`` (``_strip_pythonpath_from_sys_path``)
        # removes any PYTHONPATH-derived entries from sys.path before
        # grimp is imported, so the internal ``sys.path.insert(0, ...)``
        # calls below cannot collide with one Python added at startup.
        # Empirically the internal injection of ``root/shared`` does
        # not trigger the same ``NotATopLevelModule`` failure, but the
        # mechanism for that asymmetry is not fully understood — a
        # future grimp release could become more sensitive.  If that
        # ever happens, the right move is to stop injecting the
        # subpackage source roots here and instead rely on the same
        # ``PACKAGES`` registration grimp already uses.
        # Defense against the script-invocation tests/ shadow (#2259).
        # Running ``python scripts/select_tests.py`` (the form the
        # Makefile uses) makes Python prepend ``<root>/scripts`` to
        # ``sys.path[0]``.  ``scripts/tests/`` then satisfies grimp's
        # search for the top-level ``tests`` package and shadows
        # ``<root>/tests/`` — the graph silently loses ~130 test
        # modules under ``tests/<subdir>/`` (every file outside the
        # 3 leaf modules in ``scripts/tests/``).  Pop every copy of
        # ``<root>/scripts`` for the duration of the build and
        # restore them after; PYTHONPATH-derived copies are already
        # handled by ``_strip_pythonpath_from_sys_path`` above.
        scripts_dir = str(root / "scripts")
        scripts_dir_removed = 0
        while scripts_dir in sys.path:
            sys.path.remove(scripts_dir)
            scripts_dir_removed += 1
        added_paths: list[str] = []
        for entry in (
            str(root),
            str(root / "shared"),
            str(root / "orchestrator"),
            str(root / "sandbox"),
            str(root / "sandbox" / "tools"),
            str(root / "config"),
        ):
            if entry not in sys.path:
                sys.path.insert(0, entry)
                added_paths.append(entry)
        try:
            graph = grimp.build_graph(
                *packages,
                include_external_packages=False,
                cache_dir=str(cache_dir),
            )
        finally:
            for entry in added_paths:
                try:
                    sys.path.remove(entry)
                except ValueError:
                    pass
            # Restoration preserves multiplicity but not position —
            # entries are re-inserted at sys.path[0] regardless of
            # where they sat originally.  The production trigger
            # (``python scripts/select_tests.py``) puts scripts_dir
            # at position 0, so the round-trip is faithful for the
            # case that matters; an exotic caller that wedged
            # scripts_dir mid-path will see it shift to the front.
            for _ in range(scripts_dir_removed):
                sys.path.insert(0, scripts_dir)
    finally:
        os.chdir(cwd)

    all_modules = set(graph.modules)

    # Test modules: any node under a test root whose final component
    # starts with "test_".
    test_root_prefixes = tuple(t + "." for t in TEST_PACKAGES)
    all_test_modules: set[str] = set()
    for module in all_modules:
        if not any(module.startswith(p) for p in test_root_prefixes):
            continue
        leaf = module.rsplit(".", 1)[-1]
        if leaf.startswith("test_") or leaf.endswith("_test"):
            all_test_modules.add(module)

    # Dynamic-import scan.
    dynamic_import_modules = _scan_dynamic_imports(graph, root)

    # Source-file staleness guard — every .py file under SOURCE_ROOTS
    # (excluding __pycache__/.venv/tests) MUST be a node in `graph`.
    missing_source_paths: list[str] = []
    for src_path in _enumerate_source_paths(root):
        module = path_to_module(str(src_path))
        if module is None:
            continue
        if module not in all_modules:
            missing_source_paths.append(str(src_path))

    # Bare-name AST resolver — supplements grimp's edges for the
    # codebase's universal bare-name import pattern.  See the section
    # comment above `_module_to_filesystem_path` for context.
    bare_name_upstream = build_bare_name_upstream_edges(all_modules, root)

    return GraphBundle(
        graph=graph,
        all_modules=all_modules,
        all_test_modules=all_test_modules,
        dynamic_import_modules=dynamic_import_modules,
        missing_source_paths=missing_source_paths,
        bare_name_upstream=bare_name_upstream,
    )


# ----------------------------------------------------------------------
# Reverse-closure + test-file mapping (TASK-2-3)
# ----------------------------------------------------------------------


def reverse_closure(bundle: GraphBundle, module_path_pairs: Iterable[tuple[str, str]]) -> set[str]:
    """Return the transitive set of modules that import any changed module.

    Mixed `as_package` strategy (algorithm §6):
      - If the changed path is an `__init__.py`, treat the module as a
        package and call `find_downstream_modules(pkg, as_package=True)`.
      - Otherwise (regular leaf), call with `as_package=False`.

    Callers MUST pass aligned `(module, path)` tuples — building the
    pairing inside the function (rather than zipping two lists at the
    call site) prevents any chance of `__init__.py` detection misfiring
    when the unresolvable-paths filter shortens one list relative to
    the other (reviewer_contract feedback on the v1 proposal).

    The walk combines grimp's transitive closure with the AST
    resolver's bare-name reverse edges (`bundle.bare_name_upstream`),
    so consumers that import the changed module via bare name —
    grimp's structural blind spot in this repo — are still picked up.
    """
    init_modules: set[str] = set()
    leaf_modules: set[str] = set()
    for module, path in module_path_pairs:
        if path.endswith("__init__.py"):
            init_modules.add(module)
        else:
            leaf_modules.add(module)

    # Step 1: grimp's package-mode closure for `__init__.py` seeds (a
    # package edit can affect anything downstream of the WHOLE package,
    # not just the __init__ leaf).  Leaf seeds are handled by the
    # combined walker below.
    closure: set[str] = set(init_modules) | set(leaf_modules)
    for module in init_modules:
        try:
            closure |= set(bundle.graph.find_downstream_modules(module, as_package=True))
        except Exception:  # noqa: BLE001 — fail-open at upper layer
            continue

    # Step 2: combined BFS — extends `closure` via grimp's leaf-mode
    # closure AND the bare-name resolver's reverse edges.  Re-walking
    # from every node already in `closure` is correct (set-membership
    # checks short-circuit visited nodes) and ensures bare-name edges
    # discovered downstream of the package-mode closure are still
    # followed transitively.
    return _walk_upstream_combined(bundle, closure)


def is_dynamic_import_touched(bundle: GraphBundle, changed_modules: Iterable[str]) -> bool:
    """Return True iff any changed module is in (or reverse-reachable
    from) the dynamic-import seed set."""
    seeds = bundle.dynamic_import_modules
    for module in changed_modules:
        if module in seeds:
            return True
    # Reverse-reachability: a changed module that imports a dynamic-
    # import seed can also indirectly trigger dynamic loading.
    for seed in seeds:
        try:
            upstream = bundle.graph.find_upstream_modules(seed, as_package=False)
        except Exception:  # noqa: BLE001
            continue
        for module in changed_modules:
            if module in upstream:
                return True
    return False


def map_modules_to_test_files(
    bundle: GraphBundle, modules: Iterable[str], repo_root: Path
) -> list[str]:
    """Convert a set of modules to repo-relative test file paths.

    Intersects `modules` with bundle.all_test_modules, then converts
    each module id back to a `.py` file path under the repo root.
    Sorted for deterministic output.
    """
    test_modules = set(modules) & bundle.all_test_modules
    paths: set[str] = set()
    for module in test_modules:
        candidate = Path(module.replace(".", os.sep) + ".py")
        if (repo_root / candidate).is_file():
            paths.add(str(candidate))
        else:
            # Package-form test module (very rare) — try __init__.
            init_candidate = Path(module.replace(".", os.sep)) / "__init__.py"
            if (repo_root / init_candidate).is_file():
                paths.add(str(init_candidate))
    return sorted(paths)


# ----------------------------------------------------------------------
# PYTEST_ARGS classifier (R5 / Q5)
# ----------------------------------------------------------------------


_TEST_ROOT_PREFIXES = tuple(
    {t + "/" for t in TEST_ROOT_DIRS} | {t + os.sep for t in TEST_ROOT_DIRS}
)


def pytest_args_have_explicit_path(args: Iterable[str], repo_root: Path) -> bool:
    """Return True iff PYTEST_ARGS contains a positional path argument
    that resolves to an existing file/dir under one of the four test
    roots.  Flag values like `--hypothesis-seed=gateway/tests/x.py`
    must NOT trigger this — they contain a path-shaped substring but
    are flag values, not positional args.
    """
    for raw in args:
        if not raw:
            continue
        # Skip explicit flag tokens.  Anything starting with `-` is a
        # flag; anything containing `=` before `/` is a flag value
        # (e.g. `--hypothesis-seed=gateway/tests/x.py`).
        if raw.startswith("-"):
            continue
        if "=" in raw:
            head = raw.split("=", 1)[0]
            if head.startswith("--") or head.startswith("-"):
                continue
        # Treat as a possible positional path arg.
        candidate = repo_root / raw
        if candidate.exists() and (
            raw.startswith(_TEST_ROOT_PREFIXES) or any(raw == t for t in TEST_ROOT_DIRS)
        ):
            return True
    return False


# ----------------------------------------------------------------------
# Selection JSON envelope (TASK-2-4b)
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _short_sha(sha: str | None) -> str:
    if not sha or not _is_valid_sha(sha):
        return "unknown"
    return sha[:7]


def write_selection_record(
    *,
    head: str,
    baseline_sha: str | None,
    baseline_source: str,
    branch: str | None,
    mode: str,
    trigger: str,
    selected_count: int,
    total_count: int,
    compute_ms: int,
    changed_files_list: list[str],
    changed_modules_list: list[str],
    dynamic_import_seeds_hit: list[str],
    repo_root: Path,
) -> Path:
    """Write the per-invocation JSON record.  Returns the written path."""
    record: dict[str, Any] = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "head": head,
        "baseline": {"sha": baseline_sha, "source": baseline_source},
        "branch": branch,
        "mode": mode,
        "trigger": trigger,
        "selected_count": selected_count,
        "total_count": total_count,
        "compute_ms": compute_ms,
        "pytest_ms": None,  # Populated by `--patch-selection-json` later.
        "timestamp": _now_iso(),
        "changed_files": list(changed_files_list),
        "changed_modules": list(changed_modules_list),
        "dynamic_import_seeds_hit": list(dynamic_import_seeds_hit),
    }
    log_dir = repo_root / SELECTION_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"{head}.json"
    _atomic_write_text(out_path, json.dumps(record, indent=2) + "\n")
    return out_path


def patch_selection_record(head: str, pytest_ms: int, repo_root: Path | None = None) -> int:
    """Append `pytest_ms` to the existing
    `.egg-state/selection/<head>.json` record.

    Called by the Makefile `test` wrapper after pytest returns.
    Returns 0 always — a missing record is logged to stderr but is not
    a failure (the wrapper should not abort on a missing log file).
    """
    root = repo_root if repo_root is not None else _git_repo_root()
    path = root / SELECTION_LOG_DIR / f"{head}.json"
    if not path.exists():
        _log(f"select-tests: no selection record at {path}; skipping pytest_ms patch")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"select-tests: could not parse {path}: {e}; skipping pytest_ms patch")
        return 0
    data["pytest_ms"] = pytest_ms
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return 0


# ----------------------------------------------------------------------
# Fallback-trigger evaluator (TASK-2-3)
# ----------------------------------------------------------------------


def _fnmatch(path: str, pattern: str) -> bool:
    """Lightweight ** glob matcher for the FALLBACK_PATH_PATTERNS table."""
    import fnmatch

    return fnmatch.fnmatch(path, pattern)


def evaluate_fallback_triggers(
    *,
    paths: list[str],
    bundle: GraphBundle | None,
    baseline_source: str,
    lkg_was_stale: bool,
) -> str | None:
    """Return an explicit trigger string when the changeset forces a
    full-suite fallback, or None when the narrow path is safe.

    Order matters: the first matching trigger wins so the stderr line
    names the most-specific reason rather than a generic catch-all.
    """
    # 1. Baseline resolution failed before we even got here.
    if baseline_source == "UNRESOLVABLE":
        return "unresolvable baseline"

    # 2. LKG sidecar exists but is no longer an ancestor of HEAD
    #    (force-push / reset).
    if lkg_was_stale:
        return "LKG not ancestor of HEAD"

    # 3. Path-pattern triggers.  Priority order: most-specific first
    #    so the stderr line names the most-informative reason
    #    rather than a generic "non-.py change" catch-all when a
    #    Makefile / pyproject.toml is in the same diff.

    # 3a. conftest at any level.  Match the literal filename or `/conftest.py`
    # at a path boundary so files like `myconftest.py` don't false-fire.
    for raw_path in paths:
        if raw_path == "conftest.py" or raw_path.endswith("/conftest.py"):
            return "conftest changed"

    # 3b. shared/tests/** (Q2 — fixture edits widen).
    for raw_path in paths:
        if raw_path.startswith("shared/tests/"):
            return "shared/tests/ changed"

    # 3c. Static path triggers (Makefile, pyproject.toml, uv.lock, etc.).
    for pattern, trigger_string in FALLBACK_PATH_PATTERNS:
        for raw_path in paths:
            if _fnmatch(raw_path, pattern):
                return trigger_string

    # 3d. Gateway importlib-test-loader mapping (R1).  Hits any
    # `gateway/<file>.py` that is NOT under `gateway/tests/`.  Checked
    # BEFORE the generic non-.py rule so a mixed diff names the
    # specific blind spot.
    #
    # Layout assumption (locked by current repo as of this PR): gateway/
    # production source is FLAT — every .py production file is directly
    # under `gateway/<file>.py`, no subdirectories (verified with
    # `ls gateway/*.py`).  The TASK-2-3 spec phrases the rule as "any
    # changed path matching `gateway/*.py`", which is what the
    # `"/" not in raw_path[len("gateway/") :]` guard implements.  If
    # gateway production code is ever reorganised into subdirectories
    # (e.g., `gateway/api/foo.py`), this check would NOT widen on those
    # subdirectory edits — extend the guard to drop the `"/" not in`
    # clause at that point.  TASK-5-2's parametrized cases cover the
    # current flat layout and would catch a change in semantics.
    for raw_path in paths:
        if (
            raw_path.startswith("gateway/")
            and not raw_path.startswith("gateway/tests/")
            and "/" not in raw_path[len("gateway/") :]
            and raw_path.endswith(".py")
        ):
            return "gateway source change (importlib test-loader)"

    # 3e. Non-.py changes (decision-5) — the catch-all when none of
    # the more-specific path triggers fired.
    for raw_path in paths:
        if not raw_path.endswith(".py"):
            return "non-.py change"

    # 4. Source-file staleness guard (R2).  Without a graph we cannot
    #    evaluate; defer to the unresolvable-module check below.
    if bundle is not None and bundle.missing_source_paths:
        return f"source file missing from graph: {bundle.missing_source_paths[0]}"

    # 5. Unresolvable module path (path doesn't map to a graph node).
    if bundle is not None:
        for raw_path in paths:
            module = path_to_module(raw_path)
            if module is None:
                # Non-.py path — already handled above.
                continue
            if module not in bundle.all_modules:
                return f"unresolvable module path: {raw_path}"

    # 6. Dynamic-import reachability (decision-10).
    if bundle is not None and bundle.dynamic_import_modules:
        resolved_modules: list[str] = [
            m for m in (path_to_module(p) for p in paths) if m is not None
        ]
        if is_dynamic_import_touched(bundle, resolved_modules):
            return "dynamic-import reachability"

    return None


# ----------------------------------------------------------------------
# `--why` introspection (TASK-2-4b)
# ----------------------------------------------------------------------


def _format_chain(chain: list[str]) -> str:
    return "\n".join(chain)


def explain_why(test_path: str, repo_root: Path | None = None) -> int:
    """Implement `--why <test_path>`.

    Loads the grimp graph, computes the selected set against the
    current diff, then either prints the import chain from any changed
    module to the test, or explains why the test is not selected.
    Always exits 0.
    """
    root = repo_root if repo_root is not None else _git_repo_root()
    test_module = path_to_module(test_path)
    if test_module is None:
        _log(f"select-tests: --why: cannot resolve path to module: {test_path}")
        return 0

    try:
        bundle = build_graph(root)
    except Exception as e:  # noqa: BLE001 — fail-open
        _log(f"select-tests: --why: graph build failed: {e}")
        return 0

    if test_module not in bundle.all_modules:
        _log(
            f"select-tests: --why: {test_module} is not a node in the graph; "
            "narrowing decisions cannot reach it"
        )
        return 0

    baseline_sha, baseline_source, _branch = resolve_baseline(repo_root=root)
    if baseline_sha is None:
        _log("select-tests: --why: cannot resolve baseline; full suite would run")
        return 0

    diff = changed_files(baseline_sha, repo_root=root)
    module_path_pairs: list[tuple[str, str]] = []
    for path in diff:
        module = path_to_module(path)
        if module is not None and module in bundle.all_modules:
            module_path_pairs.append((module, path))

    if not module_path_pairs:
        _log("select-tests: --why: no changed modules resolved")
        return 0

    changed_modules_list = [m for m, _ in module_path_pairs]
    closure = reverse_closure(bundle, module_path_pairs)
    is_selected = test_module in closure

    # Try grimp's shortest-chain primitive.
    best_chain: list[str] | None = None
    for src in changed_modules_list:
        try:
            chain = bundle.graph.find_shortest_chain(src, test_module)
        except Exception:  # noqa: BLE001 — fail-open
            chain = None
        if chain:
            chain_list = list(chain)
            if best_chain is None or len(chain_list) < len(best_chain):
                best_chain = chain_list

    if best_chain is None:
        _log("select-tests: --why: no path exists from any changed module")
        return 0

    pretty_chain = "\n".join(f"  → {m}" for m in best_chain)
    if is_selected:
        _log(f"select-tests: --why: {test_module} is in the selected set:")
    else:
        _log(
            f"select-tests: --why: {test_module} is NOT in the selected set; "
            "closest reachable chain follows:"
        )
    print(pretty_chain)
    return 0


# ----------------------------------------------------------------------
# Main entry — orchestrates the narrow / fallback flow (TASK-2-1..2-4b)
# ----------------------------------------------------------------------


def emit_full_suite(reason: str | None = None) -> int:
    """Emit the full test-root list on stdout.  Used by --full-suite,
    fallback paths, and the fail-open wrapper."""
    for d in TEST_ROOT_DIRS:
        print(d)
    if reason is not None:
        _log(reason)
    return 0


def _split_pytest_args_env() -> list[str]:
    """Split PYTEST_ARGS_RAW env var into shell tokens.

    The Makefile `test` recipe sets `PYTEST_ARGS_RAW="$(PYTEST_ARGS)"`
    so the selector can run the path-vs-flag classifier (R5 / Q5).
    Returns [] when the env var is unset or empty.  Falls open on
    shlex parse errors (treat as "no path arg" rather than a hard
    failure).
    """
    import shlex

    raw = os.environ.get("PYTEST_ARGS_RAW", "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=True)
    except ValueError:
        # Unbalanced quotes etc. — treat as "no path", caller will
        # narrow normally and pytest's own arg parser will surface
        # the real syntax error to the user.
        return []


def _run_narrow_or_fallback(repo_root: Path) -> int:
    """The full default-mode path, factored out so main() can call it
    from inside the blanket try/except wrapper without the wrapper
    growing arms-and-legs."""
    t0 = time.monotonic()

    baseline_sha, baseline_source, branch = resolve_baseline(repo_root=repo_root)
    lkg_was_stale = lkg_is_stale(repo_root=repo_root)

    # Resolve HEAD for the selection-record path.  HEAD is always
    # resolvable (sandboxes always have a HEAD) — if it isn't, fall
    # open to full suite.
    rc, head_stdout, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc != 0 or not _is_valid_sha(head_stdout.strip()):
        # Best-effort selection record so the telemetry trail still
        # captures the failure.  Fall-open exit 0.
        emit_full_suite("select-tests: full suite (trigger=cannot resolve HEAD)")
        try:
            write_selection_record(
                head="0" * 40,
                baseline_sha=baseline_sha,
                baseline_source=baseline_source,
                branch=branch,
                mode="full_suite",
                trigger="cannot resolve HEAD",
                selected_count=len(TEST_ROOT_DIRS),
                total_count=len(TEST_ROOT_DIRS),
                compute_ms=int((time.monotonic() - t0) * 1000),
                changed_files_list=[],
                changed_modules_list=[],
                dynamic_import_seeds_hit=[],
                repo_root=repo_root,
            )
        except OSError:
            pass
        return 0
    head_sha = head_stdout.strip()

    # Compute the diff against the baseline (or empty list when
    # baseline_source == "UNRESOLVABLE").
    if baseline_sha is not None:
        diff = changed_files(baseline_sha, repo_root=repo_root)
    else:
        diff = []

    # PYTEST_ARGS bypass classifier (R5 / Q5) — runs BEFORE both the
    # empty-diff short-circuit and the fallback evaluator so an
    # explicit-path PYTEST_ARGS short-circuits everything (the
    # developer is steering pytest manually and doesn't want
    # narrowing).  In particular, on a clean tree (`make test
    # PYTEST_ARGS=tests/foo/test_bar.py`) the empty-diff branch must
    # NOT fire — the user wants pytest to run that path.  Flag values
    # like `--hypothesis-seed=gateway/tests/x.py` correctly classify
    # as intersect (narrow) — see pytest_args_have_explicit_path
    # comments and TASK-5-4 for the regression cases.
    pytest_args_tokens = _split_pytest_args_env()
    bypass_narrowing = pytest_args_have_explicit_path(pytest_args_tokens, repo_root)

    if bypass_narrowing:
        # Bypass mode — emit nothing on stdout, let the Makefile fall
        # through to the user's explicit PYTEST_ARGS path.  Record
        # the decision so telemetry catches the override.  We
        # deliberately skip `build_graph` here because the bypass
        # ignores the closure entirely; total_count falls back to the
        # cheap TEST_ROOT_DIRS count.
        compute_ms = int((time.monotonic() - t0) * 1000)
        _log(
            "select-tests: bypass mode — PYTEST_ARGS contains an explicit "
            "test path; narrowing skipped, pytest runs only the user-"
            "supplied path(s)"
        )
        try:
            write_selection_record(
                head=head_sha,
                baseline_sha=baseline_sha,
                baseline_source=baseline_source,
                branch=branch,
                mode="bypass",
                trigger="PYTEST_ARGS explicit path",
                selected_count=0,
                total_count=len(TEST_ROOT_DIRS),
                compute_ms=compute_ms,
                changed_files_list=diff,
                changed_modules_list=[],
                dynamic_import_seeds_hit=[],
                repo_root=repo_root,
            )
        except OSError:
            pass
        return 0

    # Empty diff against a resolvable, current baseline = nothing to
    # test.  Skip pytest entirely — the Makefile prints "no tests
    # selected" when the selector emits zero stdout lines.
    # Unresolvable-baseline / stale-LKG still widen for safety; those
    # are evaluated by the trigger evaluator below.
    if not diff and baseline_source != "UNRESOLVABLE" and not lkg_was_stale:
        compute_ms = int((time.monotonic() - t0) * 1000)
        short_baseline = _short_sha(baseline_sha) if baseline_sha else "?"
        _log(
            f"select-tests: no changes since baseline {short_baseline}; "
            f"selected 0 tests (skipping pytest)"
        )
        try:
            write_selection_record(
                head=head_sha,
                baseline_sha=baseline_sha,
                baseline_source=baseline_source,
                branch=branch,
                mode="narrow",
                trigger="none",
                selected_count=0,
                total_count=0,
                compute_ms=compute_ms,
                changed_files_list=[],
                changed_modules_list=[],
                dynamic_import_seeds_hit=[],
                repo_root=repo_root,
            )
        except OSError:
            pass
        return 0

    # Build the grimp graph.  This is the expensive call; it's also the
    # one most likely to fail in a sandbox without grimp installed.  The
    # outer fail-open wrapper catches the ImportError.
    bundle: GraphBundle | None = None
    try:
        bundle = build_graph(repo_root)
    except Exception as e:  # noqa: BLE001 — fall through to fallback eval
        _log(f"select-tests: graph build failed: {type(e).__name__}: {e}")
        # Without a graph we still want to evaluate the
        # path-pattern triggers — but if none fire, we must widen to
        # full suite anyway because we cannot compute the closure.

    # Pre-compute changed-modules-list + seeds-hit ONCE so both the
    # narrow and full-suite branches can write a uniformly-detailed
    # JSON record (reviewer non-blocking #2).
    module_path_pairs: list[tuple[str, str]] = []
    if bundle is not None:
        for path in diff:
            module = path_to_module(path)
            if module is not None and module in bundle.all_modules:
                module_path_pairs.append((module, path))
    changed_modules_list = [m for m, _ in module_path_pairs]
    seeds_hit: list[str] = []
    if bundle is not None:
        seeds_hit = sorted(m for m in changed_modules_list if m in bundle.dynamic_import_modules)

    # Fallback triggers — first match wins.
    trigger = evaluate_fallback_triggers(
        paths=diff,
        bundle=bundle,
        baseline_source=baseline_source,
        lkg_was_stale=lkg_was_stale,
    )

    # If we have no graph and no explicit trigger, force a full suite
    # with an explicit reason (the graph is necessary for the closure
    # step; missing graph = missing analysis).
    if bundle is None and trigger is None:
        trigger = "graph unavailable"

    # Last-line-of-defense check: when narrowing IS possible (bundle
    # exists, no trigger fired), but neither grimp's import graph nor
    # the bare-name AST resolver can reach any test module from a
    # non-test changed module — widen to full suite.  Such a module is
    # a true blind spot: nothing in the graph claims to import it, so
    # narrowing risks skipping a real consumer that uses a pattern
    # neither analysis can see (e.g., subprocess invocation, runtime
    # plugin discovery, or a bare-name import we haven't taught the
    # resolver about).
    if bundle is not None and trigger is None and module_path_pairs:
        zero_downstream_offenders: list[str] = []
        for module, _path in module_path_pairs:
            if module in bundle.all_test_modules:
                continue  # editing a test pulls only itself; that's fine
            reachable = _walk_upstream_combined(bundle, [module])
            if not (reachable & bundle.all_test_modules):
                zero_downstream_offenders.append(module)
        if zero_downstream_offenders:
            trigger = f"no downstream tests for changed module: {zero_downstream_offenders[0]}"

    compute_ms = int((time.monotonic() - t0) * 1000)

    # Total test count (used for both narrow and full-suite log lines).
    total_count = len(bundle.all_test_modules) if bundle is not None else len(TEST_ROOT_DIRS)

    if trigger is not None:
        # Full-suite fallback path.  Write the SAME detailed record
        # (changed_modules + seeds_hit) the narrow path writes — the
        # operator wants the "why" detail when a fallback fires.
        emit_full_suite()
        _log(f"select-tests: full suite {total_count} tests (trigger={trigger})")
        write_selection_record(
            head=head_sha,
            baseline_sha=baseline_sha,
            baseline_source=baseline_source,
            branch=branch,
            mode="full_suite",
            trigger=trigger,
            selected_count=total_count,
            total_count=total_count,
            compute_ms=compute_ms,
            changed_files_list=diff,
            changed_modules_list=changed_modules_list,
            dynamic_import_seeds_hit=seeds_hit,
            repo_root=repo_root,
        )
        return 0

    # ---- Narrow path ----
    assert bundle is not None  # narrowed above

    closure = reverse_closure(bundle, module_path_pairs)
    test_files = map_modules_to_test_files(bundle, closure, repo_root)
    selected_count = len(test_files)

    for path in test_files:
        print(path)

    short_baseline = _short_sha(baseline_sha)
    elapsed_s = compute_ms / 1000.0
    _log(
        f"select-tests: narrowed {selected_count}/{total_count} tests "
        f"in {elapsed_s:.2f}s (baseline={short_baseline}, trigger=diff)"
    )

    write_selection_record(
        head=head_sha,
        baseline_sha=baseline_sha,
        baseline_source=baseline_source,
        branch=branch,
        mode="narrow",
        trigger="none",
        selected_count=selected_count,
        total_count=total_count,
        compute_ms=compute_ms,
        changed_files_list=diff,
        changed_modules_list=changed_modules_list,
        dynamic_import_seeds_hit=seeds_hit,
        repo_root=repo_root,
    )
    return 0


# ----------------------------------------------------------------------
# CLI entry
# ----------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="select_tests.py",
        description="Changeset-aware test selection (issue #1973)",
    )
    parser.add_argument(
        "--why",
        metavar="TEST_PATH",
        help="Print the import chain from any changed module to the named test.  Always exits 0.",
    )
    parser.add_argument(
        "--record-good",
        action="store_true",
        help="Atomically write the LKG sidecar.  Validates the sha and "
        "exits non-zero on validation failure.",
    )
    parser.add_argument(
        "--sha",
        metavar="SHA",
        help="40-char sha to record with --record-good (default HEAD).",
    )
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help="Emit all test directories on stdout (used by `make test-all`).",
    )
    parser.add_argument(
        "--patch-selection-json",
        action="store_true",
        help="Append --pytest-ms to the existing .egg-state/selection/<head>.json record.",
    )
    parser.add_argument(
        "--head",
        metavar="SHA",
        help="HEAD sha for --patch-selection-json.",
    )
    parser.add_argument(
        "--pytest-ms",
        metavar="MS",
        type=int,
        help="Pytest wall-clock duration in ms for --patch-selection-json.",
    )
    return parser


def _main_inner(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    repo_root = _git_repo_root()

    # --patch-selection-json is the cheapest mode; handle first.
    if args.patch_selection_json:
        if not args.head or args.pytest_ms is None:
            print(
                "select-tests: --patch-selection-json requires --head and --pytest-ms",
                file=sys.stderr,
            )
            return 0  # fail-open
        return patch_selection_record(args.head, args.pytest_ms, repo_root=repo_root)

    if args.record_good:
        try:
            return record_good(args.sha, repo_root=repo_root)
        except RecordGoodValidationError as e:
            _log(f"select-tests: --record-good validation failed: {e}")
            return 1

    if args.full_suite:
        return emit_full_suite()

    if args.why is not None:
        return explain_why(args.why, repo_root=repo_root)

    # Default mode — narrow or fallback.
    return _run_narrow_or_fallback(repo_root)


def _strip_pythonpath_from_sys_path() -> None:
    """Pop ``PYTHONPATH`` from the env AND remove its entries from
    ``sys.path``.

    A ``PYTHONPATH=shared:gateway:orchestrator`` (the value the Makefile
    exports for pytest) leaves ``shared/`` at the head of ``sys.path``,
    which causes grimp's ``build_graph`` to abort with
    ``NotATopLevelModule: shared.egg_agent`` — ``egg_agent`` becomes
    reachable as a bare top-level package via ``shared/`` AND as
    ``shared.egg_agent`` via the namespace package, and grimp refuses
    the ambiguity.  Python has already resolved the env var to absolute
    paths and prepended them to sys.path at interpreter startup, so
    just popping the env var is not enough — we also strip the
    resolved entries from sys.path.
    """
    pythonpath = os.environ.pop("PYTHONPATH", None)
    if not pythonpath:
        return
    for entry in pythonpath.split(os.pathsep):
        if not entry:
            continue
        # Python prepends the resolved absolute path; try both forms.
        candidates = {entry}
        try:
            candidates.add(str(Path(entry).resolve()))
        except OSError:
            pass
        for candidate in candidates:
            while candidate in sys.path:
                sys.path.remove(candidate)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with the **fail-open** wrapper.

    Any unhandled exception inside _main_inner() is caught here, the
    traceback is printed to stderr, the full test-root list is emitted
    on stdout, and the process exits 0.  A selector bug must NEVER
    block iteration — correctness is preserved by widening to the
    full suite.

    The ONLY exception is the explicit `return 1` from
    --record-good's RecordGoodValidationError handler above, which is
    a pure write operation where silent success on bad input would
    poison LKG.

    To verify the fail-open contract by hand: temporarily remove the
    try/except below and run `scripts/select_tests.py` after deleting
    grimp from the venv — the selector should NOW raise; with the
    try/except in place, it should emit the full test-root list and
    exit 0 instead.  TASK-5-2 includes a regression test that locks
    this behaviour.
    """
    # Defense-in-depth: a `PYTHONPATH` containing source roots like
    # `shared` makes subpackages reachable as both `shared.egg_agent`
    # and bare `egg_agent`, which causes grimp to abort the graph
    # build with `NotATopLevelModule`.  The Makefile already strips
    # the env via `env -u PYTHONPATH`, but anyone invoking the
    # script directly (or any future caller that forgets the
    # wrapper) gets the same protection here.
    #
    # Popping the env var alone is not enough — Python has already
    # prepended the PYTHONPATH entries to sys.path at interpreter
    # startup (resolved to absolute paths), and grimp inspects
    # sys.path directly during `build_graph`.  Scrub both.
    _strip_pythonpath_from_sys_path()
    try:
        return _main_inner(argv)
    except SystemExit:
        # argparse's `sys.exit` for `--help` etc. — let through.
        raise
    except BaseException:
        # noqa: BLE001 — the WHOLE point is "catch anything".
        traceback.print_exc()
        emit_full_suite("select-tests: full suite (trigger=selector exception)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
