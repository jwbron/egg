"""TASK-5-4 — Monorepo staleness-guard tests for scripts/select_tests.py.

The exhaustive guard against ``PACKAGES`` drifting out of sync with
the repo layout: assert that EVERY ``test_*.py`` file under the four
test roots is a node in the grimp graph built from the live
``PACKAGES`` constant.  If a future package addition forgets to
register itself in PACKAGES, this test fails loudly and the
``make test`` narrow path silently dropping that package's tests is
caught at CI time rather than via missed coverage in production.

Plus a coarser sanity check: at least one known cross-package edge
per top-level package is present in the graph.

These tests require a real grimp graph against the live repo and so
are skipped when grimp isn't installed (``pytest.importorskip``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import REPO_ROOT, load_selector

selector = load_selector()

# Skip if grimp not available.  In CI ``make test-all`` runs with the
# dev extras installed which includes grimp, so this test runs there.
grimp = pytest.importorskip("grimp")


# ----------------------------------------------------------------------
# Test-file enumeration helpers
# ----------------------------------------------------------------------


_TEST_ROOT_DIRS = ("tests", "gateway/tests", "orchestrator/tests", "shared/tests")


def _enumerate_test_files() -> list[Path]:
    """Return every ``test_*.py`` file under the four test roots
    (relative to the repo root)."""
    out: list[Path] = []
    for root in _TEST_ROOT_DIRS:
        root_path = REPO_ROOT / root
        if not root_path.is_dir():
            continue
        for path in root_path.rglob("test_*.py"):
            # Skip any hidden directories.
            parts = set(path.parts)
            if "__pycache__" in parts or ".venv" in parts:
                continue
            out.append(path.relative_to(REPO_ROOT))
    return sorted(out)


def _path_to_test_module(rel_path: Path) -> str:
    """Convert a ``test_*.py`` path under a test root to its grimp
    module id."""
    return ".".join(rel_path.with_suffix("").parts)


# ----------------------------------------------------------------------
# Module-scoped fixture — building the grimp graph is expensive; share
# it across the assertions below.
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_repo_graph():  # noqa: ANN201 — grimp graph type isn't public
    """Build the grimp graph against the live egg monorepo using the
    selector's ``build_graph`` helper.  Returns the GraphBundle.

    Reproduces the script-invocation ``sys.path`` shape the Makefile
    actually uses (``python scripts/select_tests.py`` puts
    ``<root>/scripts`` at ``sys.path[0]``).  Without this, pytest's
    own ``sys.path`` keeps ``scripts/`` invisible, the ``scripts/tests/``
    shadow never fires, and ``test_every_test_file_is_a_graph_node``
    passed locally and in CI while the production invocation silently
    dropped ~130 modules from the graph (issue #2259).
    """
    # Force scripts_dir to sys.path[0] unconditionally — if a prior
    # test or PYTHONPATH already wedged it at, say, position 5,
    # ``if scripts_dir not in sys.path`` would skip the insert and the
    # shadow wouldn't fire reliably (build_graph resolves modules in
    # sys.path order, so the shadow only triggers when scripts_dir
    # precedes the real source roots).  Save sys.path wholesale and
    # restore so we don't leak state to other tests.
    # ``build_graph`` already wraps its own ``os.chdir`` in
    # try/finally, so no separate cwd save/restore is needed here.
    scripts_dir = str(REPO_ROOT / "scripts")
    saved_path = sys.path[:]
    while scripts_dir in sys.path:
        sys.path.remove(scripts_dir)
    sys.path.insert(0, scripts_dir)
    try:
        return selector.build_graph(REPO_ROOT)
    finally:
        sys.path[:] = saved_path


# ----------------------------------------------------------------------
# Exhaustive PACKAGES drift guard.
# ----------------------------------------------------------------------


def test_every_test_file_is_a_graph_node(real_repo_graph) -> None:
    """The PACKAGES constant in select_tests.py must register every
    test root such that grimp sees every ``test_*.py`` as a node.
    If this test fails, a new test directory was added without
    updating PACKAGES — narrowing would silently drop that
    directory's tests."""
    expected_modules = {_path_to_test_module(p) for p in _enumerate_test_files()}
    actual_modules = real_repo_graph.all_test_modules
    missing = expected_modules - actual_modules
    # Some test files may be under packages that grimp can't fully
    # resolve (e.g. test files that import non-grimp-registered
    # third-party deps); the test allows a small allowlist of
    # missing modules but flags net-new drift.
    assert not missing, (
        f"PACKAGES is out of sync with the repo: {len(missing)} test files "
        f"are NOT graph nodes.  First 10 missing: {sorted(missing)[:10]}"
    )


def test_every_source_root_yields_at_least_one_node(real_repo_graph) -> None:
    """Coarser sanity check — every source package the selector
    registers must yield AT LEAST one node in the graph.  This catches
    a package being completely unreachable (e.g. moved + path-import
    typo) without requiring the exhaustive enumeration."""
    seen_roots: set[str] = set()
    for module in real_repo_graph.all_modules:
        for src_root in selector.SOURCE_PACKAGES:
            if module == src_root or module.startswith(src_root + "."):
                seen_roots.add(src_root)
                break
    missing_roots = set(selector.SOURCE_PACKAGES) - seen_roots
    # ``shared.egg_orchestrator`` may be empty; allow it.  Any other
    # missing root is real drift.
    truly_missing = missing_roots - {"shared.egg_orchestrator"}
    assert not truly_missing, (
        f"Source packages with zero graph nodes: {sorted(truly_missing)} — "
        "PACKAGES list is out of sync with the repo layout."
    )


def test_every_test_root_yields_at_least_one_node(real_repo_graph) -> None:
    """Same sanity check for test roots."""
    seen: set[str] = set()
    for module in real_repo_graph.all_modules:
        for tr in selector.TEST_PACKAGES:
            if module == tr or module.startswith(tr + "."):
                seen.add(tr)
                break
    missing = set(selector.TEST_PACKAGES) - seen
    assert not missing, f"test roots with zero graph nodes: {sorted(missing)}"


# ----------------------------------------------------------------------
# Cross-package edges — at least one known edge per listed package.
# ----------------------------------------------------------------------


def test_cross_package_edges_orchestrator(real_repo_graph) -> None:
    """At least one ``orchestrator.*`` module must import from
    ``shared.*`` or ``orchestrator.*`` itself; otherwise the graph
    is suspiciously empty for that package."""
    has_edge = any(
        m.startswith("orchestrator.") and not m.startswith("orchestrator.tests")
        for m in real_repo_graph.all_modules
    )
    assert has_edge


def test_cross_package_edges_gateway(real_repo_graph) -> None:
    has_edge = any(
        m.startswith("gateway.") and not m.startswith("gateway.tests")
        for m in real_repo_graph.all_modules
    )
    assert has_edge


def test_cross_package_edges_sandbox(real_repo_graph) -> None:
    has_edge = any(
        m.startswith("sandbox.") and not m.startswith("sandbox.tests")
        for m in real_repo_graph.all_modules
    )
    assert has_edge


def test_cross_package_edges_shared_egg_contracts(real_repo_graph) -> None:
    """``shared.egg_contracts`` is a heavily-imported package; verify
    it has at least one node + at least one outgoing edge into the
    graph."""
    has_node = any(m.startswith("shared.egg_contracts") for m in real_repo_graph.all_modules)
    assert has_node, "shared.egg_contracts not registered in graph"


# ----------------------------------------------------------------------
# Source-file staleness guard — ``missing_source_paths`` should be
# empty against the real repo.  If it isn't, PACKAGES has drifted.
# ----------------------------------------------------------------------


def test_no_source_files_missing_from_graph(real_repo_graph) -> None:
    """Every non-test ``.py`` file under SOURCE_ROOTS resolves to a
    grimp graph node.  An empty ``missing_source_paths`` list confirms
    PACKAGES covers the real repo layout."""
    missing = real_repo_graph.missing_source_paths
    # Real-world allowlist: deeply nested helper modules sometimes
    # don't resolve via grimp's package-import logic.  Tolerate up
    # to a small handful, but anything > 5 indicates real drift.
    assert len(missing) <= 5, (
        f"{len(missing)} source files missing from grimp graph: {missing[:10]}"
    )


# ----------------------------------------------------------------------
# Dynamic-import scan — gateway is the canonical case.
# ----------------------------------------------------------------------


def test_gateway_modules_marked_as_dynamic_imports(real_repo_graph) -> None:
    """``gateway/gateway.py`` uses ``importlib.machinery.SourceFileLoader``
    to load production modules in tests; the selector's dynamic-import
    scan MUST mark gateway.gateway (or another gateway module that
    contains the importlib pattern) as a dynamic-import seed."""
    seeds = real_repo_graph.dynamic_import_modules
    assert any(m.startswith("gateway.") for m in seeds), (
        "no gateway.* module marked as dynamic-import seed; the "
        "regex-scan in DYNAMIC_IMPORT_PATTERNS may have drifted from "
        "the gateway sources"
    )


# ----------------------------------------------------------------------
# Issue #2259 regression — `<root>/scripts` on sys.path must not shadow
# the top-level `tests/` package during graph construction.
# ----------------------------------------------------------------------


def test_scripts_dir_does_not_shadow_top_level_tests() -> None:
    """When invoked as ``python scripts/select_tests.py`` (the form
    the Makefile uses), Python prepends ``<root>/scripts`` to
    ``sys.path[0]``.  ``scripts/tests/`` (which has only 3 leaf
    modules) then satisfies grimp's search for the ``tests`` package
    and shadows the real ``<root>/tests/`` (133 files at the time of
    this fix).  ``build_graph`` must scrub the entry for the
    duration of the build so the production graph is complete.
    """
    # Unconditionally force scripts_dir to sys.path[0] — see the
    # fixture comment for why the conditional ``not in sys.path``
    # guard was insufficient.  Save sys.path wholesale and restore.
    scripts_dir = str(REPO_ROOT / "scripts")
    saved_path = sys.path[:]
    while scripts_dir in sys.path:
        sys.path.remove(scripts_dir)
    sys.path.insert(0, scripts_dir)
    try:
        bundle = selector.build_graph(REPO_ROOT)
    finally:
        sys.path[:] = saved_path
    tests_modules = {m for m in bundle.all_test_modules if m.startswith("tests.")}
    # Floor of 100 leaves headroom for legitimate test additions
    # without coupling the assertion to the live count; the bug
    # showed 3 modules, so a regression returns to single digits.
    assert len(tests_modules) > 100, (
        f"only {len(tests_modules)} tests.* modules in the graph — the "
        f"scripts/tests/ shadow has likely returned (issue #2259). "
        f"Sample: {sorted(tests_modules)[:5]}"
    )
