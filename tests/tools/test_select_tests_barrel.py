"""Tests for barrel-transparent narrowing + import-distance ordering
in ``scripts/select_tests/`` (#3182).

The decomposition pattern (#3111 / docs/guides/decomposition-pattern.md)
turns oversize files into sub-packages fronted by a pure re-export
barrel ``__init__.py``.  Under a module-level reverse walk that barrel
reconstitutes the original file's full blast radius — a change to one
submodule taints every consumer of the barrel.  These tests pin:

  1. ``parse_barrel_exports`` — the purity classifier + re-export map;
  2. ``_used_symbols`` — the consumer-side symbol-usage scan;
  3. ``_walk_upstream_with_depth`` — the transparent closure walk and
     its conservative fallbacks;
  4. the never-zero ratchet and direct-importers-first output ordering
     in ``_run_narrow_or_fallback``.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()


# ----------------------------------------------------------------------
# `parse_barrel_exports` — purity classifier + re-export map.
# ----------------------------------------------------------------------


def _parse_barrel(source: str, package: str = "pkg") -> dict[str, set[str]] | None:
    return selector.parse_barrel_exports(textwrap.dedent(source), package)


def test_pure_barrel_relative_from_imports() -> None:
    exports = _parse_barrel(
        '''
        """Docstring."""

        from __future__ import annotations

        from ._sub import sub_func, OtherThing
        from ._other import other_func

        __all__ = ("OtherThing", "other_func", "sub_func")
        '''
    )
    assert exports == {
        "sub_func": {"pkg._sub"},
        "OtherThing": {"pkg._sub"},
        "other_func": {"pkg._other"},
    }


def test_pure_barrel_from_dot_import_binds_submodule() -> None:
    """``from . import _io`` re-exports the SUBMODULE object — tests
    reach internals through it (``selector._io._run_git``), so the
    bound name must map to the submodule itself."""
    exports = _parse_barrel("from . import _io, _cli\n")
    assert exports == {"_io": {"pkg._io"}, "_cli": {"pkg._cli"}}


def test_pure_barrel_asname_binds_the_alias() -> None:
    exports = _parse_barrel("from ._sub import inner as public\n")
    assert exports == {"public": {"pkg._sub"}}


def test_pure_barrel_dual_import_idiom() -> None:
    """The repo's ``try/except ImportError`` dual-import shape
    (non-negotiable #4 of #3111) must not disqualify a barrel; the
    relative arm provides the FQ backing."""
    exports = _parse_barrel(
        """
        try:
            from ._sub import sub_func
        except ImportError:
            from _sub import sub_func
        """
    )
    assert exports is not None
    assert exports["sub_func"] >= {"pkg._sub"}


def test_pure_barrel_allows_all_assignment_list_form() -> None:
    exports = _parse_barrel('from ._a import x\n__all__ = ["x"]\n')
    assert exports == {"x": {"pkg._a"}}


def test_absolute_imports_keep_external_backing() -> None:
    """Absolute imports don't disqualify a barrel; their bound names
    map to external targets that can never match a tainted submodule,
    so consumers of those symbols are not pulled in by package
    changes."""
    exports = _parse_barrel("from typing import Any\nfrom ._sub import x\n")
    assert exports is not None
    assert exports["x"] == {"pkg._sub"}
    assert exports["Any"] == {"typing.Any"}


@pytest.mark.parametrize(
    "source",
    [
        "from ._sub import *\n",  # star re-export — per-symbol map impossible
        "from ..sibling import x\n",  # multi-level relative — outside the package
        "def helper():\n    pass\n",  # def
        "from ._sub import x\nVERSION = '1.0'\n",  # non-__all__ assignment
        "import os\nif os.name == 'posix':\n    from ._a import x\n",  # conditional
        "from ._sub import x\nx.register()\n",  # call
        "try:\n    from ._a import x\nexcept Exception:\n    from _a import x\n",  # wrong exc
        "try:\n    from ._a import x\nexcept ImportError:\n    x = None\n",  # non-import arm
        "def x(:\n",  # syntax error
    ],
)
def test_impure_barrels_return_none(source: str) -> None:
    assert _parse_barrel(source) is None


def test_build_barrel_exports_scans_packages(tmp_path: Path) -> None:
    pure = tmp_path / "pure_pkg"
    pure.mkdir()
    (pure / "__init__.py").write_text("from ._sub import x\n", encoding="utf-8")
    (pure / "_sub.py").write_text("x = 1\n", encoding="utf-8")
    impure = tmp_path / "impure_pkg"
    impure.mkdir()
    (impure / "__init__.py").write_text("def f():\n    pass\n", encoding="utf-8")
    empty = tmp_path / "empty_pkg"
    empty.mkdir()
    (empty / "__init__.py").write_text("", encoding="utf-8")

    barrels = selector.build_barrel_exports(
        {"pure_pkg", "pure_pkg._sub", "impure_pkg", "empty_pkg"}, tmp_path
    )
    assert set(barrels) == {"pure_pkg"}
    assert barrels["pure_pkg"] == {"x": {"pure_pkg._sub"}}


# ----------------------------------------------------------------------
# `_barrel_symbols_backed_by` — edge-eligibility test.
# ----------------------------------------------------------------------


def _bundle(
    *,
    direct_importers: dict[str, set[str]] | None = None,
    package_downstream: dict[str, set[str]] | None = None,
    all_modules: set[str] | None = None,
    all_test_modules: set[str] | None = None,
    bare_name_upstream: dict[str, set[str]] | None = None,
    barrel_exports: dict[str, dict[str, set[str]]] | None = None,
    repo_root: Path | None = None,
) -> object:
    return selector.GraphBundle(
        graph=_DirectEdgeGraph(direct_importers or {}, package_downstream or {}),
        all_modules=all_modules or set(),
        all_test_modules=all_test_modules or set(),
        dynamic_import_modules=set(),
        missing_source_paths=[],
        bare_name_upstream=bare_name_upstream or {},
        barrel_exports=barrel_exports,
        repo_root=repo_root,
    )


class _DirectEdgeGraph:
    """Fake grimp graph exposing the direct-importers API the
    barrel-aware walk prefers, plus the package-mode transitive call
    `reverse_closure` uses for `__init__.py` seeds."""

    def __init__(
        self,
        direct_importers: dict[str, set[str]],
        package_downstream: dict[str, set[str]] | None = None,
    ) -> None:
        self._direct = direct_importers
        self._package_downstream = package_downstream or {}

    def find_modules_that_directly_import(self, module: str) -> set[str]:
        return set(self._direct.get(module, set()))

    def find_downstream_modules(self, module: str, *, as_package: bool = False) -> set[str]:
        if as_package:
            return set(self._package_downstream.get(module, set()))
        # Transitive leaf closure over the direct edges.
        closure: set[str] = set()
        frontier = [module]
        while frontier:
            for consumer in self._direct.get(frontier.pop(), set()):
                if consumer not in closure:
                    closure.add(consumer)
                    frontier.append(consumer)
        return closure


def test_barrel_symbols_backed_by_matches_exact_and_subtree() -> None:
    bundle = _bundle(
        barrel_exports={"pkg": {"x": {"pkg._sub"}, "y": {"pkg._other"}, "z": {"pkg._sub.deep"}}}
    )
    assert selector._barrel_symbols_backed_by(bundle, "pkg", "pkg._sub") == {"x", "z"}
    assert selector._barrel_symbols_backed_by(bundle, "pkg", "pkg._sub.deep") == {"x", "z"}
    assert selector._barrel_symbols_backed_by(bundle, "pkg", "pkg._other") == {"y"}


def test_barrel_symbols_backed_by_rejects_non_barrels_and_outsiders() -> None:
    bundle = _bundle(barrel_exports={"pkg": {"x": {"pkg._sub"}}})
    # Not a barrel at all:
    assert selector._barrel_symbols_backed_by(bundle, "other", "other._sub") is None
    # Tainted module outside the barrel's package:
    assert selector._barrel_symbols_backed_by(bundle, "pkg", "elsewhere._sub") is None


# ----------------------------------------------------------------------
# `_used_symbols` — consumer-side usage scan.
# ----------------------------------------------------------------------


def _usage_fixture(tmp_path: Path, consumer_source: str) -> object:
    """A bundle whose graph contains the barrel ``shared.mypkg`` and a
    single consumer ``tests.test_consumer`` with the given source."""
    pkg = tmp_path / "shared" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "from ._sub import sub_func\nfrom ._other import other_func\n", encoding="utf-8"
    )
    (pkg / "_sub.py").write_text("def sub_func():\n    pass\n", encoding="utf-8")
    (pkg / "_other.py").write_text("def other_func():\n    pass\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_consumer.py").write_text(textwrap.dedent(consumer_source), encoding="utf-8")
    return _bundle(
        all_modules={
            "shared.mypkg",
            "shared.mypkg._sub",
            "shared.mypkg._other",
            "tests.test_consumer",
        },
        barrel_exports={
            "shared.mypkg": {
                "sub_func": {"shared.mypkg._sub"},
                "other_func": {"shared.mypkg._other"},
            }
        },
        repo_root=tmp_path,
    )


def test_used_symbols_from_import_via_bare_name(tmp_path: Path) -> None:
    """``from mypkg import sub_func`` — the dominant repo idiom; the
    bare name must resolve to the FQ barrel."""
    bundle = _usage_fixture(tmp_path, "from mypkg import sub_func\n")
    assert selector._used_symbols(bundle, "tests.test_consumer", "shared.mypkg") == {"sub_func"}


def test_used_symbols_attribute_access_on_module_alias(tmp_path: Path) -> None:
    bundle = _usage_fixture(
        tmp_path,
        """
        import mypkg as m

        def test_x():
            m.other_func()
            m.other_func.cache_clear()
        """,
    )
    assert selector._used_symbols(bundle, "tests.test_consumer", "shared.mypkg") == {"other_func"}


def test_used_symbols_patch_string_target(tmp_path: Path) -> None:
    """``patch("mypkg._sub.helper")``-style string references must
    count as usage of the first component under the barrel."""
    bundle = _usage_fixture(
        tmp_path,
        """
        from unittest.mock import patch
        import mypkg

        def test_x():
            with patch("mypkg._sub"):
                mypkg.sub_func()
        """,
    )
    used = selector._used_symbols(bundle, "tests.test_consumer", "shared.mypkg")
    assert used == {"sub_func", "_sub"}


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("import mypkg\nimportlib = None\nx = mypkg\n", "module object escapes"),
        ("from mypkg import *\n", "star import"),
        ("def broken(:\n", "unparsable source"),
        ("import os\n", "edge exists but no visible reference"),
    ],
)
def test_used_symbols_unbounded_cases_return_none(tmp_path: Path, source: str, reason: str) -> None:
    bundle = _usage_fixture(tmp_path, source)
    assert selector._used_symbols(bundle, "tests.test_consumer", "shared.mypkg") is None, reason


def test_used_symbols_without_repo_root_is_opaque(tmp_path: Path) -> None:
    bundle = _bundle(barrel_exports={"pkg": {"x": {"pkg._sub"}}}, repo_root=None)
    assert selector._used_symbols(bundle, "anything", "pkg") is None


# ----------------------------------------------------------------------
# `_walk_upstream_with_depth` — transparent closure.
# ----------------------------------------------------------------------


def _closure_fixture(tmp_path: Path) -> object:
    """Barrel ``shared.mypkg`` with two submodules; three test
    consumers: one uses the ``_sub``-backed symbol, one the
    ``_other``-backed symbol, one lets the module object escape."""
    pkg = tmp_path / "shared" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "from ._sub import sub_func\nfrom ._other import other_func\n", encoding="utf-8"
    )
    (pkg / "_sub.py").write_text("def sub_func():\n    pass\n", encoding="utf-8")
    (pkg / "_other.py").write_text("def other_func():\n    pass\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_uses_sub.py").write_text("from mypkg import sub_func\n", encoding="utf-8")
    (tests_dir / "test_uses_other.py").write_text(
        "from mypkg import other_func\n", encoding="utf-8"
    )
    (tests_dir / "test_escape.py").write_text("import mypkg\nx = mypkg\n", encoding="utf-8")
    consumers = {"tests.test_uses_sub", "tests.test_uses_other", "tests.test_escape"}
    return _bundle(
        direct_importers={
            "shared.mypkg._sub": {"shared.mypkg"},
            "shared.mypkg._other": {"shared.mypkg"},
            "shared.mypkg": set(consumers),
        },
        package_downstream={"shared.mypkg": set(consumers)},
        all_modules={"shared.mypkg", "shared.mypkg._sub", "shared.mypkg._other"} | consumers,
        all_test_modules=consumers,
        barrel_exports={
            "shared.mypkg": {
                "sub_func": {"shared.mypkg._sub"},
                "other_func": {"shared.mypkg._other"},
            }
        },
        repo_root=tmp_path,
    )


def test_transparent_walk_filters_unrelated_barrel_consumers(tmp_path: Path) -> None:
    bundle = _closure_fixture(tmp_path)
    closure = selector._walk_upstream_combined(bundle, ["shared.mypkg._sub"])
    # Symbol user of the tainted submodule: selected.
    assert "tests.test_uses_sub" in closure
    # Unbounded consumer (module object escapes): conservatively selected.
    assert "tests.test_escape" in closure
    # Consumer of the OTHER submodule's symbol: skipped — the win.
    assert "tests.test_uses_other" not in closure


def test_opaque_walk_keeps_pre_3182_behavior(tmp_path: Path) -> None:
    bundle = _closure_fixture(tmp_path)
    closure = selector._walk_upstream_combined(bundle, ["shared.mypkg._sub"], barrel_aware=False)
    assert {"tests.test_uses_sub", "tests.test_uses_other", "tests.test_escape"} <= closure


def test_transparent_walk_is_subset_of_opaque_walk(tmp_path: Path) -> None:
    bundle = _closure_fixture(tmp_path)
    transparent = selector._walk_upstream_combined(bundle, ["shared.mypkg._sub"])
    opaque = selector._walk_upstream_combined(bundle, ["shared.mypkg._sub"], barrel_aware=False)
    assert transparent <= opaque


def test_walk_depths_count_import_distance(tmp_path: Path) -> None:
    bundle = _closure_fixture(tmp_path)
    depths = selector._walk_upstream_with_depth(bundle, {"shared.mypkg._sub": 0})
    assert depths["shared.mypkg._sub"] == 0
    assert depths["shared.mypkg"] == 1
    assert depths["tests.test_uses_sub"] == 2


def test_changed_barrel_init_taints_every_consumer(tmp_path: Path) -> None:
    """Editing the barrel `__init__.py` itself must keep the full
    package-mode blast radius — transparency applies only to changes
    in submodules BEHIND the barrel."""
    bundle = _closure_fixture(tmp_path)
    closure = selector.reverse_closure(bundle, [("shared.mypkg", "shared/mypkg/__init__.py")])
    assert {"tests.test_uses_sub", "tests.test_uses_other", "tests.test_escape"} <= closure


def test_barrel_with_unmapped_import_edge_taints_fully(tmp_path: Path) -> None:
    """A barrel that imports a submodule but re-exports nothing from
    it (analysis gap — empty backed-symbol set on a real edge) must
    fall back to full taint."""
    bundle = _bundle(
        direct_importers={
            "pkg._hidden": {"pkg"},
            "pkg": {"tests.test_x"},
        },
        all_modules={"pkg", "pkg._hidden", "tests.test_x"},
        all_test_modules={"tests.test_x"},
        # Exports exist, but none are backed by _hidden.
        barrel_exports={"pkg": {"y": {"pkg._y"}}},
        repo_root=tmp_path,
    )
    closure = selector._walk_upstream_combined(bundle, ["pkg._hidden"])
    assert "tests.test_x" in closure


def test_walk_without_direct_api_falls_back_to_transitive(tmp_path: Path) -> None:
    """Graphs lacking ``find_modules_that_directly_import`` (older
    grimp, hand-rolled stubs) must keep the pre-#3182 transitive
    behavior: transparency silently off, closure unchanged."""

    class _TransitiveOnlyGraph:
        def find_downstream_modules(self, module: str, *, as_package: bool = False) -> set[str]:
            return {"consumer_a", "consumer_b"} if module == "m" else set()

    bundle = selector.GraphBundle(
        graph=_TransitiveOnlyGraph(),
        all_modules=set(),
        all_test_modules=set(),
        dynamic_import_modules=set(),
        missing_source_paths=[],
        bare_name_upstream={},
    )
    assert selector._walk_upstream_combined(bundle, ["m"]) == {"m", "consumer_a", "consumer_b"}


# ----------------------------------------------------------------------
# `_run_narrow_or_fallback` — never-zero ratchet + output ordering.
# ----------------------------------------------------------------------


def _drive_narrow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bundle: object,
    changed_paths: list[str],
) -> tuple[int, str]:
    """Run ``_run_narrow_or_fallback`` in-process against a synthetic
    repo: stub git (clean baseline, given diff) and graph build."""
    import contextlib
    import io as _io_mod

    fake_head = "0" * 39 + "a"
    fake_baseline = "0" * 39 + "b"

    def fake_run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        if args[:2] == ["rev-parse", "HEAD"]:
            return 0, fake_head + "\n", ""
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n", ""
        if args[:2] == ["merge-base", "HEAD"]:
            return 0, fake_baseline + "\n", ""
        if args[:1] == ["merge-base"] and "--is-ancestor" in args:
            return 0, "", ""
        if args[:2] == ["cat-file", "-e"]:
            return 0, "", ""
        if args[:1] == ["diff"]:
            return 0, "".join(p + "\n" for p in changed_paths), ""
        return 0, "", ""

    monkeypatch.setattr(selector._io, "_run_git", fake_run_git)
    monkeypatch.setattr(selector._cli, "build_graph", lambda repo_root: bundle)
    monkeypatch.delenv("PYTEST_ARGS_RAW", raising=False)
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    monkeypatch.chdir(tmp_path)

    stdout = _io_mod.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = selector._run_narrow_or_fallback(tmp_path)
    return rc, stdout.getvalue()


def test_never_zero_ratchet_falls_back_to_opaque_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When transparency filters EVERY test for a changed module but
    the opaque walk still reaches some, the opaque selection must be
    used — transparency may sharpen a selection, never zero it out."""
    bundle = _closure_fixture(tmp_path)
    # Remove the sub-symbol consumer and the escape consumer so the
    # transparent walk from _sub reaches no test at all.
    (tmp_path / "tests" / "test_uses_sub.py").write_text(
        "from mypkg import other_func\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_escape.py").write_text(
        "from mypkg import other_func\n", encoding="utf-8"
    )
    bundle._usage_cache.clear()

    rc, out = _drive_narrow(monkeypatch, tmp_path, bundle, ["shared/mypkg/_sub.py"])
    assert rc == 0
    selected = out.splitlines()
    # Opaque rescue: all three barrel consumers selected, no full-suite
    # fallback (which would emit the four test ROOT directories).
    assert sorted(selected) == [
        "tests/test_escape.py",
        "tests/test_uses_other.py",
        "tests/test_uses_sub.py",
    ]
    record = json.loads(
        (tmp_path / ".egg-state" / "selection" / ("0" * 39 + "a.json")).read_text(encoding="utf-8")
    )
    assert record["mode"] == "narrow"
    assert record["trigger"] == "none"


def test_truly_zero_downstream_still_triggers_full_suite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed module with no test consumers under EITHER walk keeps
    the pre-#3182 blind-spot trigger."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "orphan.py").write_text("x = 1\n", encoding="utf-8")
    bundle = _bundle(all_modules={"shared.orphan"}, repo_root=tmp_path)

    rc, out = _drive_narrow(monkeypatch, tmp_path, bundle, ["shared/orphan.py"])
    assert rc == 0
    assert out.splitlines() == list(selector.TEST_ROOT_DIRS)
    record = json.loads(
        (tmp_path / ".egg-state" / "selection" / ("0" * 39 + "a.json")).read_text(encoding="utf-8")
    )
    assert record["mode"] == "full_suite"
    assert record["trigger"] == "no downstream tests for changed module: shared.orphan"


def test_selected_tests_emitted_direct_importers_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#3182 ordering: a direct importer of the changed module must be
    emitted before a transitively-reached test even when alphabetical
    order says otherwise; pytest collects in the order given."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_aa_far.py").write_text("import mid\n", encoding="utf-8")
    (tests_dir / "test_zz_direct.py").write_text("import changed\n", encoding="utf-8")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "changed.py").write_text("x = 1\n", encoding="utf-8")
    bundle = _bundle(
        direct_importers={
            "shared.changed": {"tests.test_zz_direct", "shared.mid"},
            "shared.mid": {"tests.test_aa_far"},
        },
        all_modules={
            "shared.changed",
            "shared.mid",
            "tests.test_zz_direct",
            "tests.test_aa_far",
        },
        all_test_modules={"tests.test_zz_direct", "tests.test_aa_far"},
        repo_root=tmp_path,
    )

    rc, out = _drive_narrow(monkeypatch, tmp_path, bundle, ["shared/changed.py"])
    assert rc == 0
    assert out.splitlines() == ["tests/test_zz_direct.py", "tests/test_aa_far.py"]
