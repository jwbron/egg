"""Regression guard: slice loop must dual-import ``orchestrator.*`` (#2901).

In the deployed orchestrator pod the source tree is flattened to ``/app``
with ``PYTHONPATH=/app``: ``slice_scheduler.py``, ``peer_consensus.py``,
``state_store.py``, ``global_slice_admit.py``, ``impasse_routing.py`` all
sit at the top level — there is no ``/app/orchestrator/`` package. A bare
``from orchestrator.X import Y`` inside a lazy in-function import resolves
fine in the repo-root / test-harness context (where the repo root is on
``sys.path``) but raises ``ModuleNotFoundError`` the first time the slice
loop reaches it in production, crashing every sliced-implement-phase
pipeline on entry.

The fix at the call sites is the same dual-import pattern the rest of
``pipelines.py`` uses::

    try:
        from orchestrator.X import Y
    except ImportError:
        from X import Y  # type: ignore[no-redef]

This test enforces that pattern statically on the two slice-loop entry
points so a contributor adding a fresh ``from orchestrator.X import Y``
inside them gets a failing test instead of a silently latent bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PIPELINES = Path(__file__).resolve().parent.parent / "routes" / "pipelines.py"

# Functions that run inside the implement-phase slice loop. A bare
# ``from orchestrator.X import Y`` inside any of these will crash the
# pod runtime on slice-loop entry; they must all use the dual-import
# try/except ImportError pattern. ``_run_concurrent_phase_with_impasse_retry``
# is called per slice from inside the loop, so it counts too.
_SLICE_LOOP_FUNCS = frozenset(
    {
        "_run_implement_phase_slices",
        "_run_concurrent_phase_with_impasse_retry",
    }
)


_IMPORT_ERROR_CATCHERS = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


def _is_import_error_handler(handler: ast.ExceptHandler) -> bool:
    """Return True iff ``except`` clause catches ``ImportError`` (directly,
    via a superclass, or bare). Bare ``except``, ``except Exception``,
    and ``except BaseException`` all swallow the import failure and so
    prevent the pod-runtime crash; the dual-import pattern with an
    explicit ``ImportError`` fallback is preferred for clarity but the
    broader catchers also satisfy the no-crash invariant the test
    enforces."""
    if handler.type is None:
        return True
    types: list[ast.AST] = (
        list(handler.type.elts) if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return any(isinstance(t, ast.Name) and t.id in _IMPORT_ERROR_CATCHERS for t in types)


def _orchestrator_imports_outside_try(func: ast.FunctionDef) -> list[int]:
    """Return line numbers of ``from orchestrator.X import Y`` (or
    ``from orchestrator import Y``) statements in ``func`` that are NOT
    nested inside a ``try:`` block guarded by ``except ImportError``."""
    offending: list[int] = []

    def visit(node: ast.AST, inside_import_try: bool) -> None:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "orchestrator" or module.startswith("orchestrator."):
                if not inside_import_try:
                    offending.append(node.lineno)
            return  # ImportFrom has no children worth descending into
        if isinstance(node, ast.Try):
            guards_import = any(_is_import_error_handler(h) for h in node.handlers)
            for child in node.body:
                visit(child, inside_import_try or guards_import)
            for handler in node.handlers:
                for child in handler.body:
                    visit(child, inside_import_try)
            for child in node.orelse:
                visit(child, inside_import_try)
            for child in node.finalbody:
                visit(child, inside_import_try)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, inside_import_try)

    for stmt in func.body:
        visit(stmt, inside_import_try=False)
    return offending


def test_slice_loop_orchestrator_imports_are_dual_guarded() -> None:
    """Every ``from orchestrator.X import Y`` inside a slice-loop function
    must be wrapped in ``try/except ImportError`` so the pod runtime
    (flat layout, no ``orchestrator/`` package) falls back to the
    top-level form. Catches the recurrence pattern that #2901 fixes."""
    tree = ast.parse(_PIPELINES.read_text())
    failures: dict[str, list[int]] = {}
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _SLICE_LOOP_FUNCS:
            seen.add(node.name)
            offending = _orchestrator_imports_outside_try(node)
            if offending:
                failures[node.name] = offending

    missing = _SLICE_LOOP_FUNCS - seen
    assert not missing, (
        f"Slice-loop functions not found in {_PIPELINES}: {sorted(missing)}. "
        "Update _SLICE_LOOP_FUNCS or the function names if the refactor "
        "of routes/pipelines.py moved them."
    )

    assert not failures, (
        "Unguarded `from orchestrator.X import Y` inside slice-loop "
        "functions — these will raise ModuleNotFoundError in the pod "
        "runtime (PYTHONPATH=/app, flat layout). Wrap each in:\n\n"
        "    try:\n"
        "        from orchestrator.X import Y\n"
        "    except ImportError:\n"
        "        from X import Y  # type: ignore[no-redef]\n\n"
        f"Offending lines: {failures}"
    )
