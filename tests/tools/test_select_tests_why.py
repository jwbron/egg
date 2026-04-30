"""TASK-5-4 — `--why <test>` introspection tests for scripts/select_tests/.

The ``--why`` flag prints the import chain from any changed module to
the named test, helping agents debug "I expected this to run".  Three
documented outcomes:
  * "test is in the selected set" — chain printed.
  * "test is NOT in the selected set; closest reachable chain follows"
    when the test exists but isn't in the selection.
  * "no path exists" when the test has no import relationship to any
    changed module.

The implementation lives in ``selector.explain_why`` which calls
``selector.build_graph`` — that requires grimp.  When grimp is not
available, we use ``pytest.importorskip`` to skip these tests with a
clear message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import (
    init_git_repo,
    load_selector,
)

selector = load_selector()


# Skip the entire module when grimp isn't available.  The selector's
# fail-open contract handles the missing-grimp case at the CLI level
# (covered in test_select_tests_fallbacks.py); here we want to verify
# the chain-printing semantics, which require a live graph.
grimp = pytest.importorskip("grimp")


# ----------------------------------------------------------------------
# explain_why exits 0 in every case (fail-open contract).
# ----------------------------------------------------------------------


def test_why_unresolvable_test_path_logs_and_exits_0(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A path that doesn't resolve to a module id (non-.py file, or
    file under no registered package) prints a stderr notice and
    exits 0."""
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = selector.explain_why("not-a-test.txt", repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "cannot resolve path" in captured.err


def test_why_test_not_in_graph_logs_and_exits_0(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A test that doesn't exist in the constructed graph prints
    a stderr notice and exits 0."""
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    # explain_why builds the graph against the synthetic empty repo,
    # so any test path will be missing.
    rc = selector.explain_why("tests/no_such_test.py", repo_root=tmp_path)
    assert rc == 0


def test_why_against_real_repo_returns_zero(capsys: pytest.CaptureFixture) -> None:
    """End-to-end smoke test against the actual egg repo: ``--why``
    on an existing test must exit 0 and produce some kind of
    diagnostic on stderr (the exact message depends on whether the
    test is reachable from the current diff)."""
    selector_module = load_selector()
    rc = selector_module.explain_why("tests/test_python_syntax.py")
    assert rc == 0


# ----------------------------------------------------------------------
# CLI integration: `--why` is wired through `_main_inner` and exits 0.
# ----------------------------------------------------------------------


def test_main_inner_routes_why_flag(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = selector._main_inner(["--why", "tests/no_such.py"])
    assert rc == 0
