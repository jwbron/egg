"""TASK-5-1 — Graph-construction tests for scripts/select_tests/.

Synthetic-monorepo fixtures with known import edges; assert that
grimp's reverse-closure primitive returns the expected module sets
for a variety of changed-file inputs.

Cases covered:
  * leaf-module diff → tests that import it are selected
    (``as_package=False``).
  * mid-layer module diff → both tests-importing-the-mid-layer and
    tests-importing-the-leaf are selected.
  * cross-package edge resolves.
  * ``TYPE_CHECKING`` import is followed.
  * package ``__init__.py`` edit triggers ``as_package=True`` and
    pulls in ALL downstream-of-the-package tests, not only
    downstream-of-the-init-module.
  * leaf-module edit pulls in ONLY downstream-of-the-leaf (confirms
    the mixed strategy).

These tests need grimp installed; we use ``pytest.importorskip`` to
skip cleanly when it's missing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()

grimp = pytest.importorskip("grimp")


# ----------------------------------------------------------------------
# Synthetic mini-repo builder.
# ----------------------------------------------------------------------


def _layout_repo(root: Path, files: dict[str, str]) -> None:
    """Lay out a tiny Python repo at ``root`` with the given files.

    Auto-creates an empty ``__init__.py`` for every directory in the
    ``files`` map so grimp recognizes them as packages.
    """
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Make every parent dir a package.
    seen_dirs: set[Path] = set()
    for rel in files:
        parent = (root / rel).parent
        while parent != root and parent not in seen_dirs:
            seen_dirs.add(parent)
            init = parent / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
            parent = parent.parent


def _build_synthetic_graph(root: Path, packages: tuple[str, ...]):  # noqa: ANN201
    """Build a grimp graph against a synthetic mini-repo at ``root``.

    Temporarily inserts ``root`` into ``sys.path`` so grimp's
    ``importlib.util.find_spec()`` resolves the synthetic packages,
    and evicts any already-cached modules whose names collide with the
    synthetic packages (e.g. ``tests`` imported by pytest itself).
    """
    cwd = os.getcwd()
    root_str = str(root)
    # Save and evict any cached modules that would shadow our synthetic
    # packages — grimp uses importlib which checks sys.modules first.
    saved_modules: dict[str, object] = {}
    for pkg in packages:
        keys = [k for k in sys.modules if k == pkg or k.startswith(pkg + ".")]
        for k in keys:
            saved_modules[k] = sys.modules.pop(k)
    try:
        os.chdir(root_str)
        sys.path.insert(0, root_str)
        return grimp.build_graph(*packages, include_external_packages=False)
    finally:
        os.chdir(cwd)
        try:
            sys.path.remove(root_str)
        except ValueError:
            pass
        # Evict synthetic modules and restore originals.
        for pkg in packages:
            for k in list(sys.modules):
                if k == pkg or k.startswith(pkg + "."):
                    del sys.modules[k]
        sys.modules.update(saved_modules)


# ----------------------------------------------------------------------
# Leaf-module diff — narrow as_package=False semantics.
# ----------------------------------------------------------------------


def test_leaf_module_change_selects_only_downstream_tests(tmp_path: Path) -> None:
    """A change to ``mypkg/leaf.py`` selects only tests that import
    the leaf, not other tests under the same package root."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/leaf.py": "X = 1\n",
            "mypkg/other.py": "Y = 2\n",
            "tests/test_leaf.py": "from mypkg.leaf import X\n\n\ndef test_x(): assert X\n",
            "tests/test_other.py": "from mypkg.other import Y\n\n\ndef test_y(): assert Y\n",
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "tests"))
    downstream = graph.find_downstream_modules("mypkg.leaf", as_package=False)
    assert "tests.test_leaf" in downstream
    assert "tests.test_other" not in downstream


def test_mid_layer_module_change_pulls_transitive_tests(tmp_path: Path) -> None:
    """A change to a mid-layer module pulls in tests that import it
    AND tests that import its downstream leaves transitively."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/mid.py": "from mypkg.leaf import X\n\nY = X + 1\n",
            "mypkg/leaf.py": "X = 1\n",
            "tests/test_mid.py": "from mypkg.mid import Y\n\n\ndef test_y(): assert Y\n",
            "tests/test_leaf.py": "from mypkg.leaf import X\n\n\ndef test_x(): assert X\n",
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "tests"))
    downstream_leaf = graph.find_downstream_modules("mypkg.leaf", as_package=False)
    # Editing leaf must pull in BOTH tests (mid imports leaf, so test_mid
    # transitively imports leaf).
    assert "tests.test_leaf" in downstream_leaf
    assert "tests.test_mid" in downstream_leaf
    # Editing mid pulls in test_mid, NOT test_leaf.
    downstream_mid = graph.find_downstream_modules("mypkg.mid", as_package=False)
    assert "tests.test_mid" in downstream_mid
    assert "tests.test_leaf" not in downstream_mid


# ----------------------------------------------------------------------
# `as_package=True` semantics — package __init__.py edit.
# ----------------------------------------------------------------------


def test_package_init_edit_pulls_all_downstream_via_as_package(tmp_path: Path) -> None:
    """Calling ``find_downstream_modules`` with ``as_package=True`` on
    a package returns the downstream of EVERY module in the package
    — not only the package's __init__ module.  This pins the mixed
    strategy from the selector's ``reverse_closure``."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/__init__.py": "VERSION = 1\n",
            "mypkg/a.py": "VAL = 1\n",
            "mypkg/b.py": "VAL = 2\n",
            "tests/test_a.py": "from mypkg.a import VAL\n\n\ndef test_a(): assert VAL\n",
            "tests/test_b.py": "from mypkg.b import VAL\n\n\ndef test_b(): assert VAL\n",
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "tests"))
    # as_package=True: ALL downstream of the package, regardless of
    # which module inside the package was edited.
    downstream_pkg = graph.find_downstream_modules("mypkg", as_package=True)
    assert "tests.test_a" in downstream_pkg
    assert "tests.test_b" in downstream_pkg
    # as_package=False on the bare package treats it as a single
    # module — so test_a and test_b that import children won't be
    # included.  This is what makes the mixed strategy semantically
    # important.
    downstream_init = graph.find_downstream_modules("mypkg", as_package=False)
    # test_a imports mypkg.a, not mypkg directly, so isn't downstream
    # of `mypkg` as a leaf module.
    assert "tests.test_a" not in downstream_init


# ----------------------------------------------------------------------
# Cross-package edge.
# ----------------------------------------------------------------------


def test_cross_package_edge_resolves(tmp_path: Path) -> None:
    """A test under ``tests/`` that imports from ``mypkg.utils`` is
    downstream of ``mypkg.utils``."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/utils.py": "def helper(): return 42\n",
            "myotherpkg/consumer.py": "from mypkg.utils import helper\n\nVAL = helper()\n",
            "tests/test_consumer.py": (
                "from myotherpkg.consumer import VAL\n\n\ndef test_v(): assert VAL\n"
            ),
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "myotherpkg", "tests"))
    downstream = graph.find_downstream_modules("mypkg.utils", as_package=False)
    assert "myotherpkg.consumer" in downstream
    assert "tests.test_consumer" in downstream


# ----------------------------------------------------------------------
# TYPE_CHECKING imports — grimp follows them by default.
# ----------------------------------------------------------------------


def test_type_checking_import_is_followed(tmp_path: Path) -> None:
    """grimp's static analysis sees ``if TYPE_CHECKING: import x`` as a
    real edge, so a downstream test that uses x for typing is still
    selected."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/types_only.py": "TYPE_NAME = 'X'\n",
            "mypkg/consumer.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from mypkg.types_only import TYPE_NAME\n"
                "VAL = 1\n"
            ),
            "tests/test_consumer.py": (
                "from mypkg.consumer import VAL\n\n\ndef test_v(): assert VAL\n"
            ),
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "tests"))
    downstream = graph.find_downstream_modules("mypkg.types_only", as_package=False)
    # consumer should be downstream because grimp follows the
    # TYPE_CHECKING import.  test_consumer transitively too.
    assert "mypkg.consumer" in downstream


# ----------------------------------------------------------------------
# `selector.reverse_closure` mixed-strategy contract.
# ----------------------------------------------------------------------


def test_reverse_closure_mixes_init_and_leaf_strategies(tmp_path: Path) -> None:
    """The selector's ``reverse_closure`` helper applies
    ``as_package=True`` for ``__init__.py`` paths and
    ``as_package=False`` otherwise.  Verify the union behaviour with
    a mixed input list."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/__init__.py": "VAL = 1\n",
            "mypkg/leaf.py": "X = 1\n",
            "tests/test_leaf.py": "from mypkg.leaf import X\n\n\ndef test_x(): assert X\n",
            "tests/test_pkg.py": "import mypkg\n\n\ndef test_v(): assert mypkg.VAL\n",
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "tests"))
    bundle = selector.GraphBundle(
        graph=graph,
        all_modules=set(graph.modules),
        all_test_modules=set(),
        dynamic_import_modules=set(),
        missing_source_paths=[],
    )
    closure = selector.reverse_closure(
        bundle,
        [("mypkg", "mypkg/__init__.py"), ("mypkg.leaf", "mypkg/leaf.py")],
    )
    assert "tests.test_leaf" in closure
    assert "tests.test_pkg" in closure


def test_reverse_closure_leaf_only_does_not_pull_pkg_consumers(tmp_path: Path) -> None:
    """Editing only a leaf module must NOT pull in tests that consume
    the parent package directly (those are downstream of __init__,
    which we didn't change)."""
    _layout_repo(
        tmp_path,
        {
            "mypkg/__init__.py": "VAL = 1\n",
            "mypkg/leaf.py": "X = 1\n",
            "tests/test_leaf.py": "from mypkg.leaf import X\n\n\ndef test_x(): assert X\n",
            "tests/test_pkg.py": "import mypkg\n\n\ndef test_v(): assert mypkg.VAL\n",
        },
    )
    graph = _build_synthetic_graph(tmp_path, ("mypkg", "tests"))
    bundle = selector.GraphBundle(
        graph=graph,
        all_modules=set(graph.modules),
        all_test_modules=set(),
        dynamic_import_modules=set(),
        missing_source_paths=[],
    )
    closure = selector.reverse_closure(
        bundle,
        [("mypkg.leaf", "mypkg/leaf.py")],
    )
    assert "tests.test_leaf" in closure
    # test_pkg imports `mypkg` (the __init__), NOT `mypkg.leaf`, so it
    # MUST NOT be in the closure of a leaf edit.
    assert "tests.test_pkg" not in closure
