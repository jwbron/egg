"""Regression tests for the ``scripts/select_tests`` sub-package layout.

Slice-1 of issue #2261 decomposed the single 1,875-line
``scripts/select_tests.py`` into a ``scripts/select_tests/`` package
with five submodules (``_constants``, ``_io``, ``_graph``, ``_cli``,
plus ``__main__``) and an explicit per-symbol re-export barrel in
``__init__.py`` (decision-5).

These tests guard the package's externally observable shape so a
future refactor that removes a symbol from the barrel — or moves a
symbol between submodules without re-exporting it — fails fast
instead of surfacing as a mysterious ``AttributeError`` deep inside
the unit-test sweep.

Coverage:

* ``test_barrel_re_exports_every_symbol_used_by_tests`` — the test
  suite as a whole patches / reads ``selector.<sym>`` for ~25
  underscore-prefixed helpers.  This test asserts every one of those
  names resolves at the package barrel (decision-5: explicit per-
  symbol re-exports; feedback Q6: re-export everything externally
  referenced).
* ``test_barrel_exposes_submodules_for_qualified_patches`` — the
  ``_run_git`` patch fixture monkeypatches
  ``selector._io._run_git``; the ``_main_inner`` fail-open test
  monkeypatches ``selector._cli._main_inner``.  Both rely on the
  barrel making submodule attributes accessible (it imports them
  eagerly).  This test pins that contract.
* ``test_main_module_dunder_main_runs_full_suite`` — covers the
  ``python -m select_tests`` invocation form alongside the
  Makefile's ``python scripts/select_tests/__main__.py`` path-style
  form.  Both must work; ``__main__.py`` injects ``scripts/`` on
  ``sys.path`` for the path-style form.
* ``test_submodules_under_hard_size_cap`` — slice-1's allowlist
  drop is only valid if every submodule is below
  the ``hard_code_lines`` cap.  The cap is read from
  ``scripts/file-size-allowlist.yaml`` and the count comes from the
  lint itself, so this never drifts from ``make lint``.  This is a
  belt-and-braces guard against future growth re-tripping the
  global cap and re-introducing an allowlist entry.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import subprocess
import sys

import pytest

from tests.tools._select_tests_helpers import (
    REPO_ROOT,
    SELECTOR_PATH,
    find_python,
    load_selector,
)

selector = load_selector()


# Underscore-prefixed and public symbols accessed by the existing
# test suite via the package barrel (``selector.<name>``).  Drift
# between this list and the production codebase should be picked up
# by ``test_barrel_re_exports_every_symbol_used_by_tests``.
_BARREL_REQUIRED_SYMBOLS = (
    # _cli surface
    "_main_inner",
    "_run_narrow_or_fallback",
    "_strip_pythonpath_from_sys_path",
    "_short_sha",
    "main",
    # _constants surface
    "PACKAGES",
    "TEST_ROOT_DIRS",
    # _graph surface
    "_extract_imports",
    "_walk_upstream_combined",
    "build_graph",
    "build_bare_name_index",
    "reverse_closure",
    "pytest_args_have_explicit_path",
    # _io surface
    "_atomic_write_text",
    "_run_git",
    "record_good",
    "resolve_baseline",
    "lkg_is_stale",
    "changed_files",
    "is_role_readonly",
)


def test_barrel_re_exports_every_symbol_used_by_tests() -> None:
    """The package barrel re-exports every symbol the tests touch.

    Decision-5 / feedback Q6: explicit per-symbol re-exports for any
    name with external references.  ``hasattr`` is sufficient because
    the barrel's ``from ._<sub> import <sym>`` style binds the name on
    the package object — ``getattr`` would resolve through
    ``__getattr__`` if we ever switched to lazy imports, but the
    current implementation is eager so plain attribute access is the
    right test.
    """
    missing: list[str] = [name for name in _BARREL_REQUIRED_SYMBOLS if not hasattr(selector, name)]
    assert not missing, (
        f"barrel is missing re-exports for: {missing}.  Add them to "
        "scripts/select_tests/__init__.py to keep the patch-target "
        "surface stable."
    )


def test_barrel_exposes_submodules_for_qualified_patches() -> None:
    """``selector._io`` / ``selector._cli`` / ``selector._graph`` /
    ``selector._constants`` are accessible without an explicit
    submodule import in the test file.

    The ``real_git`` fixture relies on ``monkeypatch.setattr(
    selector._io, "_run_git", ...)`` reaching internal callers inside
    ``_io.py`` (Python resolves ``_run_git`` through ``_io``'s own
    namespace at call time).  If ``__init__.py`` ever stops importing
    the submodule eagerly the fixture would silently fall back to
    patching only the barrel attribute and leak the gateway-wrapped
    git binary into synthetic-repo tests.
    """
    for sub in ("_io", "_cli", "_graph", "_constants"):
        assert hasattr(selector, sub), (
            f"selector.{sub} is not accessible at the barrel; check "
            "that scripts/select_tests/__init__.py imports the "
            "submodule eagerly (e.g. ``from . import _io``)."
        )


def test_main_module_dunder_main_runs_full_suite() -> None:
    """``python -m select_tests --full-suite`` must work alongside
    the Makefile's path-style ``python __main__.py`` form.

    The package was extracted under ``scripts/`` (which is not on
    ``sys.path`` by default), so callers either go through the
    Makefile shim (path-style) or set ``PYTHONPATH=scripts`` to use
    ``python -m``.  Both forms are part of the contract — the
    Makefile uses path-style and downstream slices may use
    ``python -m`` once the helper goes on ``sys.path``.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "scripts") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [find_python(), "-m", "select_tests", "--full-suite"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"python -m select_tests --full-suite exited "
        f"{proc.returncode}\nstdout: {proc.stdout!r}\n"
        f"stderr: {proc.stderr!r}"
    )
    # The full-suite mode emits the four test-root directories.
    for d in selector.TEST_ROOT_DIRS:
        assert d in proc.stdout, f"missing test-root {d!r} in stdout: {proc.stdout!r}"


def test_dunder_main_path_style_invocation_works() -> None:
    """``python scripts/select_tests/__main__.py --full-suite``
    matches the Makefile invocation shape.

    ``__main__.py`` inserts ``scripts/`` on ``sys.path`` before
    importing the package so the path-style form does NOT require a
    ``PYTHONPATH=scripts`` env var; the Makefile relies on this.
    """
    proc = subprocess.run(
        [find_python(), str(SELECTOR_PATH), "--full-suite"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"python __main__.py --full-suite exited {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    for d in selector.TEST_ROOT_DIRS:
        assert d in proc.stdout, f"missing test-root {d!r} in stdout: {proc.stdout!r}"


@functools.cache
def _load_file_size_checker():
    """Load ``scripts/check-file-sizes.py`` as a module.

    Measuring with the lint's own helpers (rather than re-implementing a
    line count here) keeps this test honest: the cap and the counting
    rule both come from the thing ``make lint`` actually runs.

    Cached: the caller is parametrized, and re-executing the module (and
    rebinding ``sys.modules``) once per case buys nothing.
    """
    path = REPO_ROOT / "scripts" / "check-file-sizes.py"
    spec = importlib.util.spec_from_file_location("check_file_sizes_shape", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_file_sizes_shape"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "submodule",
    ["__init__.py", "__main__.py", "_constants.py", "_io.py", "_graph.py", "_cli.py"],
)
def test_submodules_under_hard_size_cap(submodule: str) -> None:
    """Every ``scripts/select_tests/<submodule>`` is under the global
    ``check-file-sizes`` hard cap.

    Slice-1's allowlist drop only holds if every new file ships under
    the cap.  This is a belt-and-braces guard so future commits that
    bloat ``_cli.py`` or ``_graph.py`` past the cap fail the unit
    sweep before they hit ``make lint``.

    The cap counts code lines only (#3671): docstrings, comment-only
    lines and blank lines are excluded, so documenting these modules
    cannot trip this assertion and trimming their prose cannot rescue
    one that has genuinely outgrown the cap.
    """
    pkg_dir = REPO_ROOT / "scripts" / "select_tests"
    target = pkg_dir / submodule
    assert target.is_file(), f"missing submodule: {target}"
    checker = _load_file_size_checker()
    cap = checker.load_config().caps.hard_code_lines
    code_lines = checker.measure(target).code_lines
    # This assertion reads the cap only, never allowlist membership, so
    # re-adding an exemption cannot make it pass -- splitting is the only
    # way out, which is the point of the belt-and-braces guard.
    assert code_lines <= cap, (
        f"{submodule}: {code_lines} code lines > {cap} hard cap; split it further."
    )


def test_select_tests_py_file_is_gone() -> None:
    """The original ``scripts/select_tests.py`` must NOT exist at the
    same time as the ``scripts/select_tests/`` package directory —
    Python forbids the two from coexisting.

    Slice-1's first action is ``git mv scripts/select_tests.py
    scripts/select_tests/__init__.py``; if a future revert mistakenly
    restores the .py file alongside the directory, ``import
    select_tests`` resolves ambiguously (the file wins) and the
    barrel re-exports never run.
    """
    legacy = REPO_ROOT / "scripts" / "select_tests.py"
    assert not legacy.is_file(), (
        f"{legacy} still exists alongside the select_tests/ package; "
        "remove it (Python's package-vs-module resolution gives the "
        ".py file precedence and the re-export barrel never loads)."
    )


def test_allowlist_no_longer_lists_select_tests() -> None:
    """``scripts/file-size-allowlist.yaml`` no longer grandfathers
    the legacy ``scripts/select_tests.py`` entry.

    Slice-1's ratchet step removes the allowlist entry; without this
    test, a partial revert that restores the entry would silently
    survive lint while the package is already under the cap.
    """
    allowlist_text = (REPO_ROOT / "scripts" / "file-size-allowlist.yaml").read_text(
        encoding="utf-8"
    )
    # The legacy filename must not appear as a key.
    legacy_key = "scripts/select_tests.py:"
    assert legacy_key not in allowlist_text, (
        f"{legacy_key!r} still present in file-size-allowlist.yaml; "
        "slice-1's ratchet step requires removing this entry once "
        "every submodule is under the hard cap."
    )
