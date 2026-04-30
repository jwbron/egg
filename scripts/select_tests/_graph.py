"""Grimp import-graph construction, bare-name AST resolver, closures.

This submodule owns every helper that talks to the grimp graph: the
``GraphBundle`` container, ``build_graph`` itself, the dynamic-import
scanner, the bare-name AST resolver that bridges grimp's blind spot in
this codebase, and the reverse-closure / test-file mapping helpers
``_run_narrow_or_fallback`` consumes.

Tests import these symbols through the package's re-export barrel
(``selector._walk_upstream_combined`` etc.); internal callers within
this module reference each helper by bare name so module-level
``monkeypatch.setattr`` continues to reach every callsite.
"""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ._constants import (
    BARE_NAME_STRIP_PREFIXES,
    DYNAMIC_IMPORT_PATTERNS,
    GRIMP_CACHE_DIR,
    PACKAGES,
    SOURCE_ROOTS,
    TEST_PACKAGES,
    TEST_ROOT_DIRS,
)
from ._io import _git_repo_root, path_to_module


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
        except SyntaxError, OSError, ValueError:  # noqa: B014 — PEP 758 form
            # ValueError covers null-byte source etc.  PEP 758 (Python
            # 3.14+) makes the unparenthesised tuple form the canonical
            # ``except`` shape and ruff format normalises
            # ``except (A, B, C):`` to this bare form on every save —
            # the parens cannot be pinned via inline directives.  The
            # visual collision with the Python-2 ``except E, e``
            # migration hazard is noted, but the project's
            # ``requires-python = ">=3.14"`` floor (pyproject.toml:7)
            # makes the bare form unambiguous to the language grammar.
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
        # Running ``python scripts/select_tests/__main__.py`` (the form
        # the Makefile uses) makes Python prepend ``<root>/scripts`` to
        # ``sys.path[0]`` via the entry-point shim.  ``scripts/tests/``
        # then satisfies grimp's search for the top-level ``tests``
        # package and shadows ``<root>/tests/`` — the graph silently
        # loses ~130 test modules under ``tests/<subdir>/`` (every file
        # outside the 3 leaf modules in ``scripts/tests/``).  Pop every
        # copy of ``<root>/scripts`` for the duration of the build and
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
            # (``python scripts/select_tests/__main__.py``) puts
            # scripts_dir at position 0, so the round-trip is faithful
            # for the case that matters; an exotic caller that wedged
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


__all__ = (
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
)
