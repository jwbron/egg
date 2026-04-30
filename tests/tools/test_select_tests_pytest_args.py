"""TASK-5-4 — PYTEST_ARGS classifier tests for scripts/select_tests/.

The classifier (``selector.pytest_args_have_explicit_path``) decides
whether the user-supplied PYTEST_ARGS contains a positional path
argument under one of the four test roots.  When YES, the selector
bypasses narrowing and passes args straight through to pytest.  When
NO, narrowing applies and PYTEST_ARGS compose with the selected test
files.

Risk_analyst R5: ambiguous tokens like
``--hypothesis-seed=gateway/tests/helper.py`` contain a path-shaped
substring but are flag values, not positional args, and MUST be
classified as intersect (narrow), NOT bypass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()


# ----------------------------------------------------------------------
# Bypass class — args contain a real positional path under a test root.
# ----------------------------------------------------------------------


@pytest.fixture
def repo_with_test_files(tmp_path: Path) -> Path:
    """Build a tmp directory with files at the four test-root paths so
    ``pytest_args_have_explicit_path`` can resolve them."""
    for d in selector.TEST_ROOT_DIRS:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
        (tmp_path / d / "subdir").mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "subdir" / "test_y.py").write_text("def test_y(): pass\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "args",
    [
        ["tests/test_x.py"],
        ["gateway/tests/test_x.py"],
        ["orchestrator/tests/test_x.py"],
        ["shared/tests/test_x.py"],
        ["tests/subdir/test_y.py"],
        ["tests"],
        ["gateway/tests"],
        ["-k", "foo", "tests/test_x.py"],
        ["tests/test_x.py", "-v"],
    ],
)
def test_args_with_explicit_test_path_are_bypass(
    repo_with_test_files: Path, args: list[str]
) -> None:
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is True


# ----------------------------------------------------------------------
# Intersect class — pure flags / flag values.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["-k", "foo"],
        ["-x"],
        ["-v"],
        ["-m", "not functional"],
        ["-m", "foo"],
        ["--tb=short"],
        ["--cov=gateway"],
        ["--cov-report=term-missing"],
        ["--cov-fail-under=80"],
        # Stacked-marker composition: multiple ``-m`` args reach pytest
        # composed correctly (pytest itself handles the OR).  The
        # classifier just shouldn't bypass on a marker arg.
        ["-m", "not functional", "-m", "foo"],
        [],
    ],
)
def test_args_with_only_flags_are_intersect(repo_with_test_files: Path, args: list[str]) -> None:
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is False


# ----------------------------------------------------------------------
# Ambiguous class (R5) — flag values that contain a path-shaped substring.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        # `--hypothesis-seed=gateway/tests/helper.py` — flag value with `/`.
        ["--hypothesis-seed=gateway/tests/helper.py"],
        ["--cov-report=html:gateway/tests/report/"],
        # Equals-form options
        ["--rootdir=tests"],
        ["--basetemp=tests/tmp"],
        # Long form with path-like value
        ["--ignore=tests/integration"],
    ],
)
def test_args_ambiguous_flag_values_are_intersect(
    repo_with_test_files: Path, args: list[str]
) -> None:
    """Flag values with `/`, `=`, or test-root-shaped substrings are
    NOT positional path arguments — they must be classified as
    intersect (narrow), not bypass."""
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is False


# ----------------------------------------------------------------------
# Mixed-class — bypass wins when ANY arg is a real positional path
# under a test root.
# ----------------------------------------------------------------------


def test_mixed_flag_and_positional_path_is_bypass(repo_with_test_files: Path) -> None:
    args = ["-v", "--cov=gateway", "tests/test_x.py", "--tb=short"]
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is True


def test_mixed_flag_value_and_positional_path_is_bypass(repo_with_test_files: Path) -> None:
    """When the user mixes an ambiguous flag value AND a real positional
    path, the positional wins — overall classification is bypass."""
    args = ["--hypothesis-seed=gateway/tests/helper.py", "tests/test_x.py"]
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is True


# ----------------------------------------------------------------------
# Non-test-root positional paths are NOT bypass.
# ----------------------------------------------------------------------


def test_non_test_root_positional_path_is_intersect(tmp_path: Path) -> None:
    """A positional ``scripts/foo.py`` resolves on disk but is OUTSIDE
    the four test roots — should NOT be classified as bypass.  This
    pins the test-root-prefix rule against accidental over-broadening."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    args = ["scripts/foo.py"]
    assert selector.pytest_args_have_explicit_path(args, tmp_path) is False


def test_nonexistent_positional_path_is_intersect(repo_with_test_files: Path) -> None:
    """A path that doesn't exist on disk is NOT a real positional
    arg — classified as intersect."""
    args = ["tests/no_such_file.py"]
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is False


# ----------------------------------------------------------------------
# Empty / whitespace edge cases.
# ----------------------------------------------------------------------


def test_empty_args_are_intersect(repo_with_test_files: Path) -> None:
    assert selector.pytest_args_have_explicit_path([], repo_with_test_files) is False


def test_args_with_empty_string_are_intersect(repo_with_test_files: Path) -> None:
    """An empty string in PYTEST_ARGS (often from shell-splitting an
    empty env var) must not crash the classifier."""
    args = ["", ""]
    assert selector.pytest_args_have_explicit_path(args, repo_with_test_files) is False
