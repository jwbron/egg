"""Tests for the bare-name AST resolver in ``scripts/select_tests.py``.

The codebase imports in-repo modules by bare name almost universally
(verified 406/407 test files and 33/33 sampled production files).
Grimp registers production under fully-qualified names, so without
the resolver virtually every source-file change widens to the full
suite via the ``no downstream tests for changed module`` trigger.

These tests pin the resolver's three building blocks
(``_extract_imports``, ``build_bare_name_index``,
``build_bare_name_upstream_edges``) plus the combined upstream walk
that ``reverse_closure`` and the zero-downstream check rely on.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()


# ----------------------------------------------------------------------
# `_extract_imports` — AST → import-target set.
# ----------------------------------------------------------------------


def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


def test_extract_imports_plain_import() -> None:
    targets = selector._extract_imports(_parse("import action_guards\n"))
    assert targets == {"action_guards"}


def test_extract_imports_dotted_import() -> None:
    targets = selector._extract_imports(_parse("import egg_logging.signatures\n"))
    assert targets == {"egg_logging.signatures"}


def test_extract_imports_from_module_yields_parent_and_attribute() -> None:
    """`from egg_logging import signatures` could be loading the
    `signatures` submodule OR the `signatures` attribute exported from
    the `egg_logging` package.  The resolver yields BOTH so the
    leaf-name index can match either reading."""
    targets = selector._extract_imports(_parse("from egg_logging import signatures\n"))
    assert targets == {"egg_logging", "egg_logging.signatures"}


def test_extract_imports_from_dotted_module() -> None:
    targets = selector._extract_imports(
        _parse("from egg_logging.signatures import get_workflow_signature\n")
    )
    assert "egg_logging.signatures" in targets
    assert "egg_logging.signatures.get_workflow_signature" in targets


def test_extract_imports_multiple_aliases() -> None:
    targets = selector._extract_imports(_parse("from action_guards import (foo, bar, baz)\n"))
    assert targets >= {
        "action_guards",
        "action_guards.foo",
        "action_guards.bar",
        "action_guards.baz",
    }


def test_extract_imports_skips_relative() -> None:
    """Relative imports preserve package context — grimp can already
    follow them, and they have no bare-name interpretation."""
    targets = selector._extract_imports(_parse("from . import foo\n"))
    assert targets == set()


def test_extract_imports_skips_relative_with_module() -> None:
    targets = selector._extract_imports(_parse("from ..pkg import foo\n"))
    assert targets == set()


def test_extract_imports_star_import_yields_only_module() -> None:
    """`from X import *` can't expand per-name (we don't know what's
    exported), so only X itself is recorded."""
    targets = selector._extract_imports(_parse("from action_guards import *\n"))
    assert targets == {"action_guards"}


def test_extract_imports_inside_function_body() -> None:
    """Imports nested inside functions / try / if blocks must still be
    captured — `ast.walk` is depth-first, but the resolver doesn't
    care about scope, only reachability of the name."""
    source = """
        def f():
            try:
                import action_guards  # late import for cycle break
            except ImportError:
                from egg_logging import logger
    """
    targets = selector._extract_imports(_parse(source))
    assert "action_guards" in targets
    assert "egg_logging" in targets
    assert "egg_logging.logger" in targets


# ----------------------------------------------------------------------
# `build_bare_name_index` — leaf-name → FQ module set.
# ----------------------------------------------------------------------


def test_index_includes_self_lookup() -> None:
    """Every FQ production module is its own lookup key."""
    index = selector.build_bare_name_index({"orchestrator.action_guards"})
    assert "orchestrator.action_guards" in index
    assert index["orchestrator.action_guards"] == {"orchestrator.action_guards"}


def test_index_strips_orchestrator_prefix() -> None:
    """`orchestrator.action_guards` is also reachable as `action_guards`
    because `orchestrator/` is on sys.path during graph build."""
    index = selector.build_bare_name_index({"orchestrator.action_guards"})
    assert index["action_guards"] == {"orchestrator.action_guards"}


def test_index_strips_shared_prefix() -> None:
    """`shared.egg_logging.signatures` is also reachable as
    `egg_logging.signatures` because `shared/` is on sys.path."""
    index = selector.build_bare_name_index({"shared.egg_logging.signatures"})
    assert index["egg_logging.signatures"] == {"shared.egg_logging.signatures"}


def test_index_strips_sandbox_prefix() -> None:
    index = selector.build_bare_name_index({"sandbox.entrypoint"})
    assert index["entrypoint"] == {"sandbox.entrypoint"}


def test_index_strips_sandbox_tools_prefix() -> None:
    """`sandbox.tools.X` is reachable as `tools.X` (via `sandbox/` on
    sys.path) AND as `X` (via `sandbox/tools/` on sys.path).  Both
    views must be recorded."""
    index = selector.build_bare_name_index({"sandbox.tools.foo"})
    assert "sandbox.tools.foo" in index
    assert index["tools.foo"] == {"sandbox.tools.foo"}
    assert index["foo"] == {"sandbox.tools.foo"}


def test_index_does_not_strip_gateway_prefix() -> None:
    """`gateway/` is NOT on sys.path during build_graph — gateway
    bare-name imports are handled by the dedicated widening trigger,
    not the AST resolver.  The index should NOT include `policy` as
    an alias for `gateway.policy`."""
    index = selector.build_bare_name_index({"gateway.policy"})
    assert "policy" not in index
    assert index["gateway.policy"] == {"gateway.policy"}


def test_index_excludes_test_modules() -> None:
    """Test modules are never index keys — the resolver only needs to
    map test imports to PRODUCTION modules."""
    index = selector.build_bare_name_index(
        {
            "tests.test_action_guards",
            "orchestrator.tests.test_action_guards",
            "shared.tests.test_signatures",
            "gateway.tests.test_policy",
        }
    )
    assert index == {}


def test_index_handles_ambiguous_bare_names() -> None:
    """If two FQ modules share a bare-name view (e.g., `orchestrator/foo.py`
    and `sandbox/foo.py` both reachable as `foo`), both are recorded —
    the closure over-includes, which is safer than missing a real
    consumer."""
    index = selector.build_bare_name_index({"orchestrator.foo", "sandbox.foo"})
    assert index["foo"] == {"orchestrator.foo", "sandbox.foo"}


# ----------------------------------------------------------------------
# `build_bare_name_upstream_edges` — full AST scan against on-disk source.
# ----------------------------------------------------------------------


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_upstream_edges_resolves_bare_name_test_to_production(tmp_path: Path) -> None:
    """A test that does `from action_guards import X` and a production
    file at `orchestrator/action_guards.py` produce a synthetic edge:
    the resolver records the test as an importer of
    `orchestrator.action_guards`."""
    _write(
        tmp_path,
        {
            "orchestrator/action_guards.py": "X = 1\n",
            "orchestrator/tests/test_action_guards.py": "from action_guards import X\n",
        },
    )
    all_modules = {
        "orchestrator.action_guards",
        "orchestrator.tests.test_action_guards",
    }
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    assert "orchestrator.action_guards" in edges
    assert "orchestrator.tests.test_action_guards" in edges["orchestrator.action_guards"]


def test_upstream_edges_resolves_dotted_bare_name(tmp_path: Path) -> None:
    """`from egg_logging.signatures import …` resolves through the
    `shared.` prefix-stripping rule to `shared.egg_logging.signatures`."""
    _write(
        tmp_path,
        {
            "shared/egg_logging/signatures.py": "def f(): pass\n",
            "tests/shared/egg_logging/test_signatures.py": (
                "from egg_logging.signatures import f\n"
            ),
        },
    )
    all_modules = {
        "shared.egg_logging.signatures",
        "tests.shared.egg_logging.test_signatures",
    }
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    assert "tests.shared.egg_logging.test_signatures" in edges["shared.egg_logging.signatures"]


def test_upstream_edges_drops_self_edge(tmp_path: Path) -> None:
    """A file that bare-name imports itself (uncommon, but possible
    via re-exports) doesn't produce a self-loop."""
    _write(
        tmp_path,
        {
            "orchestrator/action_guards.py": "from action_guards import X\nX = 1\n",
        },
    )
    all_modules = {"orchestrator.action_guards"}
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    # No edge to itself.
    assert edges.get("orchestrator.action_guards", set()) == set()


def test_upstream_edges_handles_syntax_error(tmp_path: Path) -> None:
    """A malformed source file is silently skipped — fail-open."""
    _write(
        tmp_path,
        {
            "orchestrator/broken.py": "def f(:\n",  # syntax error
            "orchestrator/action_guards.py": "X = 1\n",
            "orchestrator/tests/test_action_guards.py": "from action_guards import X\n",
        },
    )
    all_modules = {
        "orchestrator.broken",
        "orchestrator.action_guards",
        "orchestrator.tests.test_action_guards",
    }
    # Should not raise.
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    # The valid file's edge is still present.
    assert "orchestrator.tests.test_action_guards" in edges["orchestrator.action_guards"]


def test_upstream_edges_handles_missing_file(tmp_path: Path) -> None:
    """A module name with no matching file (e.g., grimp registered a
    namespace package without `__init__.py` materially) is skipped."""
    _write(tmp_path, {"orchestrator/action_guards.py": "X = 1\n"})
    all_modules = {"orchestrator.action_guards", "orchestrator.does_not_exist"}
    # Must not raise.
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    assert "orchestrator.action_guards" in edges or edges == {}


def test_upstream_edges_ignores_third_party_imports(tmp_path: Path) -> None:
    """Imports that don't resolve to any FQ production module are
    silently dropped (likely third-party)."""
    _write(
        tmp_path,
        {
            "orchestrator/api.py": "import requests\nfrom flask import Flask\n",
        },
    )
    all_modules = {"orchestrator.api"}
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    # `requests` and `flask` are not in `all_modules` — no edges.
    assert "requests" not in edges
    assert "flask" not in edges


def test_upstream_edges_follows_production_to_production(tmp_path: Path) -> None:
    """Bare-name edges aren't only test→production — a production
    module that bare-name imports a peer also produces a reverse edge."""
    _write(
        tmp_path,
        {
            "orchestrator/action_guards.py": "X = 1\n",
            "orchestrator/api.py": "from action_guards import X\n",
        },
    )
    all_modules = {"orchestrator.action_guards", "orchestrator.api"}
    edges = selector.build_bare_name_upstream_edges(all_modules, tmp_path)
    assert "orchestrator.api" in edges["orchestrator.action_guards"]


# ----------------------------------------------------------------------
# `_walk_upstream_combined` — BFS over grimp + bare-name edges.
# ----------------------------------------------------------------------


class _FakeGraph:
    """Minimal stub for the grimp graph — only exposes the
    `find_downstream_modules` API the walker calls."""

    def __init__(self, downstream: dict[str, set[str]]) -> None:
        self._downstream = downstream

    def find_downstream_modules(self, module: str, *, as_package: bool = False) -> set[str]:
        return set(self._downstream.get(module, set()))


def _bundle(
    graph_downstream: dict[str, set[str]],
    bare_name_upstream: dict[str, set[str]],
    all_modules: set[str] | None = None,
    all_test_modules: set[str] | None = None,
) -> object:
    return selector.GraphBundle(
        graph=_FakeGraph(graph_downstream),
        all_modules=all_modules or set(),
        all_test_modules=all_test_modules or set(),
        dynamic_import_modules=set(),
        missing_source_paths=[],
        bare_name_upstream=bare_name_upstream,
    )


def test_walk_combined_includes_seed() -> None:
    bundle = _bundle({}, {})
    assert selector._walk_upstream_combined(bundle, ["m"]) == {"m"}


def test_walk_combined_follows_grimp_edges_only() -> None:
    bundle = _bundle({"m": {"a", "b"}}, {})
    assert selector._walk_upstream_combined(bundle, ["m"]) == {"m", "a", "b"}


def test_walk_combined_follows_bare_name_edges_only() -> None:
    bundle = _bundle({}, {"m": {"t1", "t2"}})
    assert selector._walk_upstream_combined(bundle, ["m"]) == {"m", "t1", "t2"}


def test_walk_combined_unions_both_sources() -> None:
    bundle = _bundle({"m": {"a"}}, {"m": {"t1"}})
    assert selector._walk_upstream_combined(bundle, ["m"]) == {"m", "a", "t1"}


def test_walk_combined_transitively_follows_bare_name_chain() -> None:
    """Bare-name edge from `m → consumer1`, then `consumer1 → consumer2`
    must extend the closure.  Without the BFS, only `m → consumer1`
    would land in the closure."""
    bundle = _bundle(
        graph_downstream={},
        bare_name_upstream={
            "m": {"consumer1"},
            "consumer1": {"consumer2"},
        },
    )
    closure = selector._walk_upstream_combined(bundle, ["m"])
    assert closure == {"m", "consumer1", "consumer2"}


def test_walk_combined_picks_up_grimp_consumers_of_bare_name_modules() -> None:
    """If `m → bare-name → consumer1`, grimp may have its own edges
    out of `consumer1` (modules that import `consumer1` via FQ
    paths).  Those grimp-tracked edges must be followed too."""
    bundle = _bundle(
        graph_downstream={"consumer1": {"grimp_consumer"}},
        bare_name_upstream={"m": {"consumer1"}},
    )
    closure = selector._walk_upstream_combined(bundle, ["m"])
    assert closure == {"m", "consumer1", "grimp_consumer"}


def test_walk_combined_handles_grimp_exception_gracefully() -> None:
    """If grimp's API raises (e.g., unknown module), the walker keeps
    going and uses bare-name edges only."""

    class _RaisingGraph:
        def find_downstream_modules(self, module: str, *, as_package: bool = False) -> set[str]:
            raise KeyError(module)

    bundle = selector.GraphBundle(
        graph=_RaisingGraph(),
        all_modules=set(),
        all_test_modules=set(),
        dynamic_import_modules=set(),
        missing_source_paths=[],
        bare_name_upstream={"m": {"t1"}},
    )
    assert selector._walk_upstream_combined(bundle, ["m"]) == {"m", "t1"}


# ----------------------------------------------------------------------
# Zero-downstream check now consults the resolver — synthetic stub
# bundle with bare-name edges only must NOT trigger the widening.
# ----------------------------------------------------------------------


def test_zero_downstream_offender_check_clears_with_bare_name_path() -> None:
    """Smoke test of the integration point: when the only path from a
    changed module to a test runs through bare-name edges, the
    combined-walker reaches the test and the offender check passes —
    so `evaluate_fallback_triggers` does NOT widen on this module."""
    # Setup: changed module is `orchestrator.action_guards`, tests
    # reach it ONLY via bare-name (mimicking the real repo layout).
    test_module = "orchestrator.tests.test_action_guards"
    bundle = _bundle(
        graph_downstream={},  # grimp sees no edges
        bare_name_upstream={"orchestrator.action_guards": {test_module}},
        all_modules={"orchestrator.action_guards", test_module},
        all_test_modules={test_module},
    )
    closure = selector._walk_upstream_combined(bundle, ["orchestrator.action_guards"])
    assert test_module in closure
    # The intersection with all_test_modules is non-empty — the
    # ``evaluate_fallback_triggers`` zero-downstream check would NOT
    # add this module to the offender list.
    assert closure & bundle.all_test_modules
