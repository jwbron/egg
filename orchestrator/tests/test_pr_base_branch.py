"""Tests for ``get_pr_base_branch`` in ``routes/pipelines.py``.

Covers:
- gh CLI happy paths (main, develop) — no ``origin/`` prefix returned.
- gh non-zero exit, invalid JSON, and raised-exception fallback paths.
- ``pr_number=None`` behaviour with and without a worktree path.
- ``repo`` parameter being forwarded to ``gh`` via ``--repo <repo>``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from routes.pipelines import get_pr_base_branch


def _make_completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    """Build a ``CompletedProcess`` for mocking ``subprocess.run``."""
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Happy path: gh returns a valid baseRefName
# ---------------------------------------------------------------------------


class TestGhPrViewHappyPath:
    """gh CLI returns a valid ``baseRefName`` payload."""

    def test_base_ref_name_main(self):
        """PR with baseRefName=main -> returns 'main' (no 'origin/' prefix)."""
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(returncode=0, stdout='{"baseRefName": "main"}')

            result = get_pr_base_branch(123)

            assert result == "main"
            assert not result.startswith("origin/")

    def test_base_ref_name_develop(self):
        """PR with baseRefName=develop -> returns 'develop'."""
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(
                returncode=0, stdout='{"baseRefName": "develop"}'
            )

            result = get_pr_base_branch(456)

            assert result == "develop"
            assert not result.startswith("origin/")

    def test_base_ref_name_custom_branch(self):
        """Arbitrary feature branch name is returned verbatim, still no prefix."""
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(
                returncode=0, stdout='{"baseRefName": "release/2026-04"}'
            )

            result = get_pr_base_branch(789)

            assert result == "release/2026-04"
            assert not result.startswith("origin/")


# ---------------------------------------------------------------------------
# Fallback: gh fails in various ways
# ---------------------------------------------------------------------------


class TestGhPrViewFallback:
    """When gh fails, we should fall back to ``_detect_default_branch`` or 'main'."""

    def test_gh_nonzero_exit_falls_back_to_detect_default(self, tmp_path: Path):
        """gh exits non-zero -> _detect_default_branch is consulted when worktree is given."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch", return_value="master") as mock_detect,
        ):
            mock_run.return_value = _make_completed(
                returncode=1, stdout="", stderr="gh: no such PR"
            )

            result = get_pr_base_branch(123, worktree_repo_path=tmp_path)

            assert result == "master"
            assert not result.startswith("origin/")
            mock_detect.assert_called_once_with(tmp_path)

    def test_gh_invalid_json_falls_back(self, tmp_path: Path):
        """gh returns invalid JSON -> _detect_default_branch is consulted."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch", return_value="main") as mock_detect,
        ):
            mock_run.return_value = _make_completed(returncode=0, stdout="not-valid-json{{{")

            result = get_pr_base_branch(123, worktree_repo_path=tmp_path)

            assert result == "main"
            assert not result.startswith("origin/")
            mock_detect.assert_called_once_with(tmp_path)

    def test_gh_empty_stdout_falls_back(self, tmp_path: Path):
        """gh returns 0 but empty stdout -> fall back to _detect_default_branch."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch", return_value="main") as mock_detect,
        ):
            mock_run.return_value = _make_completed(returncode=0, stdout="   ")

            result = get_pr_base_branch(123, worktree_repo_path=tmp_path)

            assert result == "main"
            assert not result.startswith("origin/")
            mock_detect.assert_called_once_with(tmp_path)

    def test_gh_subprocess_raises_falls_back(self, tmp_path: Path):
        """subprocess.run raises -> _detect_default_branch is consulted."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch", return_value="develop") as mock_detect,
        ):
            mock_run.side_effect = OSError("boom: gh binary not found")

            result = get_pr_base_branch(123, worktree_repo_path=tmp_path)

            assert result == "develop"
            assert not result.startswith("origin/")
            mock_detect.assert_called_once_with(tmp_path)

    def test_gh_json_missing_base_ref_name_falls_back(self, tmp_path: Path):
        """Valid JSON without baseRefName -> fall back to _detect_default_branch."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch", return_value="main") as mock_detect,
        ):
            mock_run.return_value = _make_completed(returncode=0, stdout='{"other": "value"}')

            result = get_pr_base_branch(123, worktree_repo_path=tmp_path)

            assert result == "main"
            assert not result.startswith("origin/")
            mock_detect.assert_called_once_with(tmp_path)


# ---------------------------------------------------------------------------
# pr_number=None paths
# ---------------------------------------------------------------------------


class TestNoPrNumber:
    """With ``pr_number=None`` we skip gh entirely."""

    def test_pr_none_with_worktree_calls_detect_default(self, tmp_path: Path):
        """pr_number=None with worktree_repo_path -> calls _detect_default_branch."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch", return_value="develop") as mock_detect,
        ):
            result = get_pr_base_branch(None, worktree_repo_path=tmp_path)

            assert result == "develop"
            assert not result.startswith("origin/")
            mock_detect.assert_called_once_with(tmp_path)
            # gh must not be invoked when pr_number is None.
            mock_run.assert_not_called()

    def test_pr_none_without_worktree_returns_main(self):
        """pr_number=None and no worktree -> returns literal 'main'."""
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._detect_default_branch") as mock_detect,
        ):
            result = get_pr_base_branch(None)

            assert result == "main"
            assert not result.startswith("origin/")
            mock_run.assert_not_called()
            mock_detect.assert_not_called()


# ---------------------------------------------------------------------------
# --repo argument forwarding
# ---------------------------------------------------------------------------


class TestRepoArgumentForwarding:
    """``repo`` parameter should be forwarded to gh via ``--repo <repo>``."""

    def test_repo_passed_as_repo_flag(self):
        """When ``repo`` is provided, gh is invoked with ``--repo <repo>``."""
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(returncode=0, stdout='{"baseRefName": "main"}')

            result = get_pr_base_branch(42, repo="anthropics/egg")

            assert result == "main"
            assert mock_run.call_count == 1
            args, _kwargs = mock_run.call_args
            cmd = args[0]
            assert cmd[:5] == ["gh", "pr", "view", "42", "--json"]
            assert cmd[5] == "baseRefName"
            # --repo must appear, followed by the repo slug.
            assert "--repo" in cmd
            assert cmd[cmd.index("--repo") + 1] == "anthropics/egg"

    def test_no_repo_flag_when_repo_is_none(self):
        """Without ``repo`` the ``--repo`` flag is not included."""
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(returncode=0, stdout='{"baseRefName": "main"}')

            result = get_pr_base_branch(42)

            assert result == "main"
            args, _kwargs = mock_run.call_args
            cmd = args[0]
            assert "--repo" not in cmd


# ---------------------------------------------------------------------------
# Global "never origin/ prefixed" guarantee
# ---------------------------------------------------------------------------


class TestNoOriginPrefix:
    """The returned branch name must never carry an ``origin/`` prefix."""

    @pytest.mark.parametrize(
        "scenario",
        [
            "gh_main",
            "gh_develop",
            "gh_failed_fallback",
            "gh_invalid_json_fallback",
            "gh_raises_fallback",
            "pr_none_with_worktree",
            "pr_none_no_worktree",
        ],
    )
    def test_returned_branch_never_origin_prefixed(self, scenario: str, tmp_path: Path):
        """Every code path must return a bare branch name."""
        if scenario == "gh_main":
            with patch("routes.pipelines.subprocess.run") as mock_run:
                mock_run.return_value = _make_completed(
                    returncode=0, stdout='{"baseRefName": "main"}'
                )
                result = get_pr_base_branch(1)
        elif scenario == "gh_develop":
            with patch("routes.pipelines.subprocess.run") as mock_run:
                mock_run.return_value = _make_completed(
                    returncode=0, stdout='{"baseRefName": "develop"}'
                )
                result = get_pr_base_branch(1)
        elif scenario == "gh_failed_fallback":
            with (
                patch("routes.pipelines.subprocess.run") as mock_run,
                patch("routes.pipelines._detect_default_branch", return_value="main"),
            ):
                mock_run.return_value = _make_completed(returncode=1)
                result = get_pr_base_branch(1, worktree_repo_path=tmp_path)
        elif scenario == "gh_invalid_json_fallback":
            with (
                patch("routes.pipelines.subprocess.run") as mock_run,
                patch("routes.pipelines._detect_default_branch", return_value="master"),
            ):
                mock_run.return_value = _make_completed(returncode=0, stdout="garbage")
                result = get_pr_base_branch(1, worktree_repo_path=tmp_path)
        elif scenario == "gh_raises_fallback":
            with (
                patch("routes.pipelines.subprocess.run") as mock_run,
                patch("routes.pipelines._detect_default_branch", return_value="main"),
            ):
                mock_run.side_effect = RuntimeError("boom")
                result = get_pr_base_branch(1, worktree_repo_path=tmp_path)
        elif scenario == "pr_none_with_worktree":
            with patch("routes.pipelines._detect_default_branch", return_value="develop"):
                result = get_pr_base_branch(None, worktree_repo_path=tmp_path)
        elif scenario == "pr_none_no_worktree":
            result = get_pr_base_branch(None)
        else:  # pragma: no cover - defensive
            pytest.fail(f"unknown scenario: {scenario}")

        assert isinstance(result, str)
        assert result
        assert not result.startswith("origin/"), (
            f"scenario {scenario!r} returned origin-prefixed ref: {result!r}"
        )


# ---------------------------------------------------------------------------
# Sanity check: subprocess.run call shape
# ---------------------------------------------------------------------------


class TestGhCommandShape:
    """Make sure we invoke gh with the right top-level arguments."""

    def test_gh_invocation_uses_expected_arguments(self):
        """gh is called with ``pr view <N> --json baseRefName`` in that order."""
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(returncode=0, stdout='{"baseRefName": "main"}')

            get_pr_base_branch(99)

            args, kwargs = mock_run.call_args
            cmd = args[0]
            assert cmd[0] == "gh"
            assert cmd[1] == "pr"
            assert cmd[2] == "view"
            assert cmd[3] == "99"
            assert "--json" in cmd
            assert "baseRefName" in cmd
            # subprocess.run should capture output and not raise on non-zero.
            assert kwargs.get("capture_output") is True
            assert kwargs.get("text") is True
            assert kwargs.get("check") is False

    def test_subprocess_run_is_mocked_not_real(self):
        """Ensure the test never actually shells out to gh."""
        sentinel = MagicMock(
            return_value=_make_completed(returncode=0, stdout='{"baseRefName": "main"}')
        )
        with patch("routes.pipelines.subprocess.run", sentinel):
            get_pr_base_branch(1)
        assert sentinel.called
