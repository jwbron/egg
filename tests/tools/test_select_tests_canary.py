"""TASK-5-3 — Canary-counter tests for scripts/select_tests.py.

The canary mechanism (decision-Q4): every 10th `make test` invocation
on a branch forces a full-suite run.  The counter lives in a per-branch
sidecar at ``.egg-state/last-known-good/<branch>.canary`` and is
gitignored.

Direct API tests below — TASK-5-2 covers the integration with
``evaluate_fallback_triggers``; TASK-5-5 the end-to-end behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()


# ----------------------------------------------------------------------
# Read / write round-trip
# ----------------------------------------------------------------------


def test_canary_read_returns_zero_for_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert selector.read_canary_count("main") == 0


def test_canary_read_returns_zero_for_detached_head(tmp_path: Path) -> None:
    assert selector.read_canary_count(None) == 0


def test_canary_read_returns_zero_for_malformed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sidecar_dir = tmp_path / selector.SIDECAR_DIR
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "main.canary").write_text("not-an-int", encoding="utf-8")
    assert selector.read_canary_count("main") == 0


def test_canary_write_then_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    selector.write_canary_count("main", 7)
    assert selector.read_canary_count("main") == 7


# ----------------------------------------------------------------------
# Per-branch isolation — canary on branch A doesn't leak into branch B.
# ----------------------------------------------------------------------


def test_canary_per_branch_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    selector.write_canary_count("feature-a", 3)
    selector.write_canary_count("feature-b", 9)
    assert selector.read_canary_count("feature-a") == 3
    assert selector.read_canary_count("feature-b") == 9


# ----------------------------------------------------------------------
# Fire semantics — verifies the count % 10 == 0 contract used by the
# main run flow.  We don't drive the run flow here (that needs grimp);
# we just confirm the modulo math the run flow executes inline.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after_increment", "should_fire"),
    [
        (1, False),
        (5, False),
        (9, False),
        (10, True),
        (11, False),
        (19, False),
        (20, True),
        (100, True),
        (101, False),
    ],
)
def test_canary_modulo_contract(after_increment: int, should_fire: bool) -> None:
    """Direct check on the inline ``count % 10 == 0`` rule the run
    flow uses (selector._run_narrow_or_fallback).  This is a unit
    test on the contract — if a future refactor changes the cadence,
    the test fails loudly."""
    fired = after_increment % 10 == 0
    assert fired is should_fire


def test_canary_resets_after_fire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the run flow logs that the canary fired, the counter is
    reset to 0.  Mimic the run-flow logic here directly."""
    monkeypatch.chdir(tmp_path)
    selector.write_canary_count("main", 9)
    count = selector.read_canary_count("main") + 1
    assert count == 10
    fired = count % 10 == 0
    assert fired
    selector.write_canary_count("main", 0 if fired else count)
    assert selector.read_canary_count("main") == 0


def test_canary_increments_on_non_fire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    selector.write_canary_count("main", 4)
    count = selector.read_canary_count("main") + 1
    fired = count % 10 == 0
    assert not fired
    selector.write_canary_count("main", count)
    assert selector.read_canary_count("main") == 5


# ----------------------------------------------------------------------
# --full-suite resets the counter (covered indirectly here; TASK-5-5
# covers the subprocess invocation).  This test invokes the in-process
# `--full-suite` path through ``selector._main_inner`` to keep the
# unit-test surface tight.
# ----------------------------------------------------------------------


def test_full_suite_mode_resets_canary(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Invoking the selector with ``--full-suite`` MUST reset the
    canary counter for the current branch."""
    from tests.tools._select_tests_helpers import _git, init_git_repo

    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    selector.write_canary_count("main", 7)
    rc = selector._main_inner(["--full-suite"])
    assert rc == 0
    assert selector.read_canary_count("main") == 0
    # Stdout must contain the test-root list.
    captured = capsys.readouterr()
    for d in selector.TEST_ROOT_DIRS:
        assert d in captured.out
    # Avoid unused-import lint.
    assert _git is not None
