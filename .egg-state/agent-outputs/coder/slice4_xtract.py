#!/usr/bin/env python3
"""One-shot extraction helper for #3312 slice-4 (decompose routes/pipelines).

Moves a set of contiguous top-level symbols out of
``orchestrator/routes/pipelines/__init__.py`` into a ``_<cluster>.py``
submodule, rewriting every *barrel-resident* free name reference to
``_pkg.<name>`` so test patches on ``routes.pipelines.<name>`` keep
intercepting and shared module state stays single-sourced in the barrel.

Bodies are moved VERBATIM: rewrites are pure text insertions of the
``_pkg.`` prefix at AST-Name positions — no reformatting, no unparse.

Usage:
    python scripts/_slice4_extract.py <submodule> <title> sym1 sym2 ...
e.g.
    python scripts/_slice4_extract.py _overseer "overseer detection-plane" \
        _spawn_overseer_agent _teardown_phase_overseer ...
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BARREL = Path("orchestrator/routes/pipelines/__init__.py")

# typing constructs whose string subscript args ruff reads as forward refs;
# never prefix these with _pkg. — import them directly from typing instead.
_SKIP_REWRITE = {"Literal", "Annotated"}


def _local_bound_names(fn: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound in this function-like scope, and names forced-global here.

    Scans only the *direct* body (does not descend into nested function or
    class scopes), matching Python's rule that a name assigned anywhere in a
    function is local to that function unless declared global/nonlocal.
    """
    bound: set[str] = set()
    forced_global: set[str] = set()
    forced_nonlocal: set[str] = set()

    # Parameters (functions/lambdas).
    args = getattr(fn, "args", None)
    if isinstance(args, ast.arguments):
        for a in (
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *( [args.vararg] if args.vararg else [] ),
            *( [args.kwarg] if args.kwarg else [] ),
        ):
            bound.add(a.arg)

    class _Scan(ast.NodeVisitor):
        def _skip(self, node: ast.AST) -> None:
            # Do not descend into nested scopes.
            return None

        visit_FunctionDef = _skip  # type: ignore[assignment]
        visit_AsyncFunctionDef = _skip  # type: ignore[assignment]
        visit_Lambda = _skip  # type: ignore[assignment]
        visit_ClassDef = _skip  # type: ignore[assignment]

        def visit_Global(self, node: ast.Global) -> None:
            forced_global.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            forced_nonlocal.update(node.names)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)

        def visit_arg(self, node: ast.arg) -> None:
            bound.add(node.arg)

    scanner = _Scan()
    # For a real function node, scan its body/decorators-excluded children.
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, ast.arguments):
            continue
        scanner.visit(child)

    # Nested def/class NAMES are bound in this scope.
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)

    bound -= forced_global
    bound |= forced_nonlocal  # nonlocal names are NOT global refs here
    return bound, forced_global


def main() -> int:
    submodule = sys.argv[1]
    title = sys.argv[2]
    move = sys.argv[3:]
    if not move:
        print("no symbols given", file=sys.stderr)
        return 2

    src = BARREL.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    # Barrel top-level bound names.
    barrel_top: set[str] = set()
    node_by_name: dict[str, ast.AST] = {}

    def _collect(stmts: list[ast.stmt]) -> None:
        """Collect module-scope bound names, descending into top-level
        try/except (the flat/relative import fallback), if (TYPE_CHECKING /
        version guards), with, and loop bodies — but NOT into function or
        class bodies (those are separate scopes)."""
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                barrel_top.add(node.name)
                continue  # do not descend into a new scope
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    barrel_top.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    for n in ast.walk(tgt):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                            barrel_top.add(n.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                barrel_top.add(node.target.id)
            if isinstance(node, ast.Try):
                _collect(node.body)
                for h in node.handlers:
                    _collect(h.body)
                _collect(node.orelse)
                _collect(node.finalbody)
            elif isinstance(node, ast.If):
                _collect(node.body)
                _collect(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                _collect(node.body)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                _collect(node.body)
                _collect(node.orelse)

    _collect(tree.body)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node_by_name[node.name] = node

    missing = [m for m in move if m not in node_by_name]
    if missing:
        print(f"symbols not found at top level: {missing}", file=sys.stderr)
        return 2

    # Line span (1-based inclusive) per moved symbol, including decorators.
    spans: list[tuple[int, int, str]] = []
    for name in move:
        node = node_by_name[name]
        start = node.lineno
        deco = getattr(node, "decorator_list", [])
        if deco:
            start = min(start, deco[0].lineno)
        end = node.end_lineno
        spans.append((start, end, name))
    spans.sort()

    # Extract verbatim text of each span, then rewrite Name refs -> _pkg.
    def rewrite(segment: str) -> str:
        seg_tree = ast.parse(segment)
        seg_lines = segment.splitlines(keepends=True)
        # (lineno, col) insertion points, collected with scope awareness.
        inserts: list[tuple[int, int]] = []

        def walk(node: ast.AST, scope_stack: list[tuple[set[str], set[str]]]) -> None:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                nid = node.id
                # Local if bound in the nearest enclosing function scope that
                # binds it and does not force it global.
                is_local = False
                for bound, forced_global in reversed(scope_stack):
                    if nid in forced_global:
                        break  # forced global here -> treat as global
                    if nid in bound:
                        is_local = True
                        break
                if not is_local and nid in barrel_top and nid not in _SKIP_REWRITE:
                    inserts.append((node.lineno, node.col_offset))
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                bound, fg = _local_bound_names(node)
                # decorators & default arg values evaluate in the ENCLOSING scope
                for d in getattr(node, "decorator_list", []):
                    walk(d, scope_stack)
                args = node.args
                for default in [*args.defaults, *[x for x in args.kw_defaults if x]]:
                    walk(default, scope_stack)
                # annotations evaluate in enclosing scope (but are strings under
                # `from __future__ import annotations`; rewrite anyway is safe).
                for a in (
                    *args.posonlyargs, *args.args, *args.kwonlyargs,
                    *([args.vararg] if args.vararg else []),
                    *([args.kwarg] if args.kwarg else []),
                ):
                    if a.annotation:
                        walk(a.annotation, scope_stack)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
                    walk(node.returns, scope_stack)
                body = node.body if not isinstance(node, ast.Lambda) else [node.body]
                for child in body:
                    walk(child, [*scope_stack, (bound, fg)])
                return
            if isinstance(node, ast.ClassDef):
                for d in node.decorator_list:
                    walk(d, scope_stack)
                for b in node.bases:
                    walk(b, scope_stack)
                for kw in node.keywords:
                    walk(kw.value, scope_stack)
                # class body: a new (class) scope; names bound there are not
                # visible to methods, so treat class body with its own bound set
                clsbound: set[str] = set()
                for child in node.body:
                    for n in ast.walk(child):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                            clsbound.add(n.id)
                for child in node.body:
                    walk(child, [*scope_stack, (clsbound, set())])
                return
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                cbound: set[str] = set()
                for gen in node.generators:
                    for n in ast.walk(gen.target):
                        if isinstance(n, ast.Name):
                            cbound.add(n.id)
                new_stack = [*scope_stack, (cbound, set())]
                for child in ast.iter_child_nodes(node):
                    walk(child, new_stack)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, scope_stack)

        for node in seg_tree.body:
            walk(node, [])

        # Apply insertions right-to-left. NOTE: ast col_offset is a UTF-8 BYTE
        # offset, so slice on the encoded bytes (lines with multibyte chars —
        # em-dashes, curly quotes — otherwise shift the insert into the middle
        # of an identifier).
        inserts = sorted(set(inserts), reverse=True)
        buf = list(seg_lines)
        for lineno, col in inserts:
            lb = buf[lineno - 1].encode("utf-8")
            buf[lineno - 1] = (lb[:col] + b"_pkg." + lb[col:]).decode("utf-8")
        return "".join(buf)

    extracted_parts = []
    for start, end, _name in spans:
        segment = "".join(lines[start - 1 : end])
        extracted_parts.append(rewrite(segment))
    extracted = "\n".join(p.rstrip("\n") + "\n" for p in extracted_parts)

    # Typing constructs whose (string) subscript args ruff treats as forward
    # refs (Literal["a"], Annotated[...]) must NOT be prefixed with _pkg. —
    # doing so defeats ruff's special-casing and yields spurious F821. They
    # stay bare and are imported directly from typing.
    typing_needed = sorted(
        n for n in _SKIP_REWRITE if re.search(rf"\b{n}\b", extracted)
    )
    typing_line = (
        f"from typing import {', '.join(typing_needed)}  # noqa: F401\n"
        if typing_needed
        else ""
    )

    header = (
        f'"""{title} helpers for routes/pipelines (#3312 slice-4).\n\n'
        "Extracted verbatim from the pipelines barrel; barrel-resident and\n"
        "test-patched globals are reached via ``_pkg`` so\n"
        "``patch(\"routes.pipelines.<name>\")`` keeps intercepting.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        f"{typing_line}"
        "import routes.pipelines as _pkg  # noqa: E402,F401\n\n\n"
    )
    out = Path("orchestrator/routes/pipelines") / f"{submodule}.py"
    out.write_text(header + extracted)

    # Rewrite barrel: blank out moved spans, then append re-export block.
    kill: set[int] = set()
    for start, end, _name in spans:
        for ln in range(start, end + 1):
            kill.add(ln)
    kept = [ln for i, ln in enumerate(lines, start=1) if i not in kill]
    barrel_src = "".join(kept)
    # collapse >2 consecutive blank lines left by removal to exactly 2
    reexport = (
        f"\n\nfrom .{submodule} import (  # noqa: E402,F401\n"
        + "".join(f"    {n},\n" for n in sorted(move))
        + ")\n"
    )
    BARREL.write_text(barrel_src.rstrip("\n") + "\n" + reexport)
    print(f"wrote {out} ({len(move)} symbols); barrel updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
