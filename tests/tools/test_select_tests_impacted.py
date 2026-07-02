"""`--impacted-tests <file>...` mode tests (#3411).

The mode answers the reverse question of default narrowing — "which
test files statically reach these source files?" — seeded from an
explicit file list instead of `git diff`.  It is the plan-phase test
co-location lens: a slice that removes or reshapes the named files
must carry the updates to every printed test file in the same slice,
or the per-slice green gate (#3398) blocks its PR.

Exit contract under test (deliberately NOT the default mode's
fail-open widen-to-full-suite posture — emitting every test root here
would read as "everything is impacted"):
  * 0 — closure computed; stdout may legitimately be empty.
  * 2 — closure could NOT be computed (graph build failed, closure
    walk failed, no argument resolved to a graph module, or an
    internal error reached the ``main()`` wrapper).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()


# ----------------------------------------------------------------------
# Contract tests with a stubbed graph — no grimp required.
# ----------------------------------------------------------------------


def _stub_graph(
    monkeypatch: pytest.MonkeyPatch,
    *,
    all_modules: set[str],
    closure: set[str] | Exception | None = None,
    test_files: list[str] | None = None,
) -> None:
    """Patch the module-global graph seams `impacted_tests` reaches by
    bare name inside ``selector._cli``."""
    bundle = SimpleNamespace(all_modules=all_modules)
    monkeypatch.setattr(selector._cli, "build_graph", lambda root: bundle)

    def fake_reverse_closure(b, pairs):
        assert b is bundle
        if isinstance(closure, Exception):
            raise closure
        return closure or set()

    monkeypatch.setattr(selector._cli, "reverse_closure", fake_reverse_closure)
    monkeypatch.setattr(
        selector._cli,
        "map_modules_to_test_files",
        lambda b, modules, root: list(test_files or []),
    )


def test_no_resolvable_args_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Non-.py paths never resolve to a module → exit 2, empty stdout."""
    _stub_graph(monkeypatch, all_modules=set())
    rc = selector.impacted_tests(["README.md"], repo_root=Path("."))
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot resolve path to module" in captured.err
    assert "no argument resolved to a graph module" in captured.err


def test_module_not_in_graph_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A .py path that maps to a module id absent from the graph is
    skipped with a notice; if nothing else resolves → exit 2."""
    _stub_graph(monkeypatch, all_modules={"orchestrator.other"})
    rc = selector.impacted_tests(["orchestrator/ghost.py"], repo_root=Path("."))
    assert rc == 2
    captured = capsys.readouterr()
    assert "orchestrator.ghost is not a node in the graph" in captured.err


def test_graph_build_failure_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def boom(root):
        raise RuntimeError("no grimp here")

    monkeypatch.setattr(selector._cli, "build_graph", boom)
    rc = selector.impacted_tests(["orchestrator/models.py"], repo_root=Path("."))
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "graph build failed" in captured.err


def test_closure_walk_failure_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _stub_graph(
        monkeypatch,
        all_modules={"orchestrator.models"},
        closure=RuntimeError("walk exploded"),
    )
    rc = selector.impacted_tests(["orchestrator/models.py"], repo_root=Path("."))
    assert rc == 2
    captured = capsys.readouterr()
    assert "closure walk failed" in captured.err


def test_happy_path_prints_test_files_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _stub_graph(
        monkeypatch,
        all_modules={"orchestrator.models"},
        closure={"orchestrator.models", "orchestrator.tests.test_models"},
        test_files=["orchestrator/tests/test_models.py"],
    )
    rc = selector.impacted_tests(["orchestrator/models.py"], repo_root=Path("."))
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["orchestrator/tests/test_models.py"]


def test_empty_closure_is_exit_0_not_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Exit 0 + empty stdout is the honest 'no test reaches these
    files' answer — distinct from exit 2 'could not compute'."""
    _stub_graph(monkeypatch, all_modules={"orchestrator.models"}, closure=set())
    rc = selector.impacted_tests(["orchestrator/models.py"], repo_root=Path("."))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_partially_resolvable_args_still_compute(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A mix of resolvable and unresolvable paths computes the closure
    for the resolvable subset (with per-path notices) — exit 0."""
    _stub_graph(
        monkeypatch,
        all_modules={"orchestrator.models"},
        closure={"orchestrator.tests.test_models"},
        test_files=["orchestrator/tests/test_models.py"],
    )
    rc = selector.impacted_tests(["not-a-file.txt", "orchestrator/models.py"], repo_root=Path("."))
    assert rc == 0
    captured = capsys.readouterr()
    assert "cannot resolve path to module: not-a-file.txt" in captured.err
    assert "orchestrator/tests/test_models.py" in captured.out


def test_partial_resolution_emits_loud_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Partial resolution stays exit 0 (the resolvable-subset closure is
    still useful) but must NOT be silent — a caller trusting the exit
    code needs the closure flagged as possibly-incomplete (reviewer
    non-blocking #2)."""
    _stub_graph(
        monkeypatch,
        all_modules={"orchestrator.models"},
        closure={"orchestrator.tests.test_models"},
        test_files=["orchestrator/tests/test_models.py"],
    )
    rc = selector.impacted_tests(["not-a-file.txt", "orchestrator/models.py"], repo_root=Path("."))
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING partial resolution" in err
    assert "1 of 2 path(s)" in err
    assert "not-a-file.txt" in err


def test_full_resolution_emits_no_partial_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """When every path resolves, the partial-resolution warning must
    not fire — the closure is complete."""
    _stub_graph(
        monkeypatch,
        all_modules={"orchestrator.models"},
        closure={"orchestrator.tests.test_models"},
        test_files=["orchestrator/tests/test_models.py"],
    )
    rc = selector.impacted_tests(["orchestrator/models.py"], repo_root=Path("."))
    assert rc == 0
    assert "partial resolution" not in capsys.readouterr().err


# ----------------------------------------------------------------------
# CLI wiring + the main() wrapper's mode-aware exception posture.
# ----------------------------------------------------------------------


def test_cli_dispatches_impacted_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(paths, repo_root=None):
        seen["paths"] = paths
        return 0

    monkeypatch.setattr(selector._cli, "impacted_tests", fake)
    rc = selector._main_inner(["--impacted-tests", "a.py", "b.py"])
    assert rc == 0
    assert seen["paths"] == ["a.py", "b.py"]


def test_main_wrapper_exits_2_without_full_suite_widening(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An unexpected exception in `--impacted-tests` mode must NOT fall
    open to the full test-root list (that would read as 'everything is
    impacted') — the main() wrapper honours the exit-2 contract."""

    def boom(argv=None):
        raise RuntimeError("internal error")

    monkeypatch.setattr(selector._cli, "_main_inner", boom)
    rc = selector.main(["--impacted-tests", "orchestrator/models.py"])
    assert rc == 2
    captured = capsys.readouterr()
    for test_root in selector.TEST_ROOT_DIRS:
        assert test_root not in captured.out.splitlines()
    assert "closure unavailable" in captured.err


def test_main_wrapper_still_widens_for_default_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Regression pin: the mode-aware branch must not weaken the
    default mode's fail-open widen-to-full-suite contract."""

    def boom(argv=None):
        raise RuntimeError("internal error")

    monkeypatch.setattr(selector._cli, "_main_inner", boom)
    rc = selector.main([])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    for test_root in selector.TEST_ROOT_DIRS:
        assert test_root in out_lines


@pytest.mark.parametrize(
    "argv",
    [
        ["--impacted-tests", "a.py"],  # canonical space-separated form
        ["--impacted-tests=a.py"],  # single-value `=` form
        ["--impacted-test", "a.py"],  # argparse prefix abbreviation
        ["--impacted", "a.py"],  # shorter unambiguous abbreviation
        ["--impacted=a.py"],  # abbreviation + `=` value
    ],
)
def test_argv_requests_impacted_tests_matches_all_forms(argv: list[str]) -> None:
    """The wrapper's mode detector must recognise every form argparse
    accepts for `--impacted-tests`, not just the canonical bare flag
    (reviewer non-blocking #1)."""
    assert selector._argv_requests_impacted_tests(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--full-suite"],
        ["--why", "tests/x.py"],
        ["--include", "a.py"],  # `--include` is NOT a prefix of --impacted-tests
        ["impacted-tests"],  # missing the leading `--`
    ],
)
def test_argv_requests_impacted_tests_rejects_other_modes(argv: list[str]) -> None:
    assert selector._argv_requests_impacted_tests(argv) is False


def test_main_wrapper_exits_2_for_equals_form(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An exception under the `--impacted-tests=file.py` single-value
    form must still honour the exit-2 contract, not fall open to the
    full suite (reviewer non-blocking #1)."""

    def boom(argv=None):
        raise RuntimeError("internal error")

    monkeypatch.setattr(selector._cli, "_main_inner", boom)
    rc = selector.main(["--impacted-tests=orchestrator/models.py"])
    assert rc == 2
    captured = capsys.readouterr()
    for test_root in selector.TEST_ROOT_DIRS:
        assert test_root not in captured.out.splitlines()
    assert "closure unavailable" in captured.err


# ----------------------------------------------------------------------
# Real-graph smoke test (requires grimp; mirrors test_select_tests_why's
# end-to-end posture).
# ----------------------------------------------------------------------


def test_impacted_tests_against_real_repo(capsys: pytest.CaptureFixture) -> None:
    """End-to-end against the actual egg repo: the plan-parser
    validators module is imported (transitively, barrel-transparently)
    by orchestrator plan-ingestion tests, so the closure must be
    non-empty, every line must be an existing test file, and exit 0."""
    pytest.importorskip("grimp")
    # Pin the repo root explicitly rather than relying on the process
    # cwd: another test in the suite can leave cwd in a non-git temp dir,
    # and `_git_repo_root()` then falls back to `Path.cwd()`, so the graph
    # is built (via sys.path) but test files are mapped against the wrong
    # root — yielding an empty closure with exit 0.  Passing repo_root
    # makes this end-to-end check order-independent.
    repo_root = Path(__file__).resolve().parent.parent.parent
    rc = selector.impacted_tests(
        ["shared/egg_contracts/plan_parser/_validators.py"], repo_root=repo_root
    )
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines, "expected a non-empty impacted-test closure"
    for line in out_lines:
        assert (repo_root / line).is_file(), f"non-existent path emitted: {line}"
        assert Path(line).name.startswith("test_") or Path(line).name.endswith("_test.py"), (
            f"non-test path emitted: {line}"
        )
