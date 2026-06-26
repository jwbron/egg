#!/usr/bin/env python3
"""Guard against orphaned test roots (issue #3302; #3298 class 2).

Every Python test root in the repo — any ``*/tests`` directory that
contains at least one ``test_*.py`` file — must be wired into *all* of
the places that enumerate test roots, atomically:

1. ``pyproject.toml`` ``[tool.pytest.ini_options] testpaths``
   (what bare ``pytest`` collects).
2. The ``make test-all`` ``pytest`` invocation in the ``Makefile``
   (what CI runs as the unit-test ground truth).
3. The full-suite fallback root list in the ``make test`` recipe
   (what the changeset-aware selector widens to).
4. ``TEST_ROOT_DIRS`` in ``scripts/select_tests/_constants.py``
   (what the selector emits on ``--full-suite`` / fallback).

…and, for any test root that lives under a package root ``mypy``
traverses (``make lint-python`` runs ``mypy gateway shared sandbox``),
into that command's ``--exclude`` list — otherwise the untyped test code
trips ``mypy`` under ``--strict``.

A new test root that is wired into only some of these is silently
uncollected by CI (so its tests never run) and/or breaks ``mypy``. This
check makes that impossible: add a ``*/tests`` dir and CI fails until
every list above is updated. See issue #3298 for the incident
(``shared/egg_agent/tests`` shipped uncollected and untyped).
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories we never descend into when discovering test roots.
_SKIP_DIRS = {
    ".git",
    ".venv",
    ".claude",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def discover_test_roots() -> set[str]:
    """Every ``*/tests`` dir (repo-relative, posix) holding a ``test_*.py``.

    ``integration_tests`` is intentionally *not* matched (different
    basename; it has its own CI job and ``mypy`` override). Shell-only
    test dirs such as ``action/tests`` are skipped because they contain
    no ``test_*.py``.
    """
    roots: set[str] = set()
    for dirpath, dirnames, _filenames in os.walk(REPO):
        # Prune skipped dirs in place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        path = Path(dirpath)
        if path.name != "tests" or path == REPO:
            continue
        if any(path.rglob("test_*.py")):
            roots.add(path.relative_to(REPO).as_posix())
    return roots


def parse_testpaths() -> set[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return {p.rstrip("/") for p in data["tool"]["pytest"]["ini_options"]["testpaths"]}


def parse_test_root_dirs() -> set[str]:
    """``TEST_ROOT_DIRS`` from ``scripts/select_tests/_constants.py`` (via ast)."""
    src = (REPO / "scripts" / "select_tests" / "_constants.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0].id
        if target == "TEST_ROOT_DIRS" and node.value is not None:
            return {p.rstrip("/") for p in ast.literal_eval(node.value)}
    raise SystemExit("check-test-roots: could not find TEST_ROOT_DIRS in _constants.py")


def _makefile_text() -> str:
    return (REPO / "Makefile").read_text(encoding="utf-8")


def parse_testall_roots(makefile: str) -> set[str]:
    """Root args of the ``make test-all`` ``$(PYTEST) ... -v`` line."""
    m = re.search(r"\$\(PYTEST\)\s+(tests/.*?)\s+-v\s+\$\(PYTEST_ARGS\)", makefile)
    if not m:
        raise SystemExit(
            "check-test-roots: could not parse the test-all $(PYTEST) line in Makefile"
        )
    return {tok.rstrip("/") for tok in m.group(1).split()}


def parse_fallback_roots(makefile: str) -> set[str]:
    """Roots of the ``printf '%s\\n' ... >"$selected_file"`` full-suite fallback.

    Anchored on the ``>"$selected_file"`` redirect target so it cannot
    accidentally match the unrelated ``printf '%s\\n' "$cur_id" >`` marker
    write elsewhere in the Makefile if the recipes are ever reordered.
    """
    m = re.search(r"""printf '%s\\n'\s+(.*?)\s*>\s*"\$\$selected_file""", makefile)
    if not m:
        raise SystemExit(
            "check-test-roots: could not parse the full-suite fallback printf in Makefile"
        )
    return {tok.rstrip("/") for tok in m.group(1).split()}


def parse_mypy_invocation(makefile: str) -> tuple[set[str], set[str]]:
    """(package roots mypy traverses, --exclude values) from lint-python."""
    m = re.search(r"\$\(MYPY\)\s+(.*?--exclude.*?);", makefile)
    if not m:
        raise SystemExit("check-test-roots: could not parse the $(MYPY) invocation in Makefile")
    line = m.group(1)
    # NB: ``--exclude`` values are mypy regexes, but every current entry is a
    # plain literal path (e.g. ``shared/egg_contracts/tests/``), so we compare
    # them literally against discovered roots. A future grouped/regex exclude
    # (e.g. ``'(shared|gateway)/tests/'``) would not match this assumption and
    # would need this parser taught to expand it.
    excludes = {e.rstrip("/") for e in re.findall(r"--exclude\s+'([^']+)'", line)}
    # Package roots = leading bare words before the first option flag.
    roots: set[str] = set()
    for tok in line.split():
        if tok.startswith("-"):
            break
        roots.add(tok)
    return roots, excludes


def main() -> int:
    discovered = discover_test_roots()

    sources = {
        "pyproject.toml::testpaths": parse_testpaths(),
        "Makefile (make test-all roots)": parse_testall_roots(_makefile_text()),
        "Makefile (make test full-suite fallback)": parse_fallback_roots(_makefile_text()),
        "scripts/select_tests/_constants.py::TEST_ROOT_DIRS": parse_test_root_dirs(),
    }

    errors: list[str] = []
    for label, wired in sources.items():
        missing = discovered - wired
        extra = wired - discovered
        if missing:
            errors.append(f"{label}: missing test root(s) {sorted(missing)}")
        if extra:
            errors.append(
                f"{label}: lists {sorted(extra)}, which is not a discovered Python test root"
            )

    # mypy: only test roots under a package root mypy traverses need exclusion.
    mypy_roots, mypy_excludes = parse_mypy_invocation(_makefile_text())
    mypy_required = {
        r for r in discovered if any(r == root or r.startswith(root + "/") for root in mypy_roots)
    }
    mypy_missing = mypy_required - mypy_excludes
    if mypy_missing:
        errors.append(
            "Makefile lint-python ($(MYPY) --exclude): missing exclude(s) "
            f"{sorted(mypy_missing)} (test roots under {sorted(mypy_roots)} must be "
            "excluded so untyped test code does not trip mypy --strict)"
        )

    if errors:
        print("check-test-roots: orphaned / mis-wired test roots detected:\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nA new */tests directory must be wired into testpaths, the test-all "
            "roots, the make test fallback, TEST_ROOT_DIRS, and (if under a mypy "
            "package root) the mypy --exclude list — all atomically. See "
            "scripts/check-test-roots.py and issue #3302.",
            file=sys.stderr,
        )
        return 1

    print(f"check-test-roots: OK ({len(discovered)} test roots wired consistently)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
