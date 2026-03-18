"""Tests for egg_babysit.cli — CLI entry point for babysit-pr."""

from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.cli import _detect_repo, main
from egg_babysit.types import BabysitExitReason, BabysitResult, BabysitStep


class TestDetectRepo:
    """Test _detect_repo auto-detection from git remote."""

    @patch("egg_babysit.cli.subprocess.run")
    def test_https_remote(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="origin\thttps://github.com/owner/repo.git (fetch)\norigin\thttps://github.com/owner/repo.git (push)\n",
        )

        result = _detect_repo()

        assert result == "owner/repo"

    @patch("egg_babysit.cli.subprocess.run")
    def test_https_remote_no_git_suffix(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="origin\thttps://github.com/owner/repo (fetch)\n",
        )

        result = _detect_repo()

        assert result == "owner/repo"

    @patch("egg_babysit.cli.subprocess.run")
    def test_ssh_remote_colon_format(self, mock_run):
        """SSH git@github.com:owner/repo format is correctly parsed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="origin\tgit@github.com:owner/repo.git (fetch)\n",
        )

        result = _detect_repo()

        assert result == "owner/repo"

    @patch("egg_babysit.cli.subprocess.run")
    def test_ssh_with_slash_format(self, mock_run):
        """SSH URL with slash (github.com/) is supported."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="origin\tssh://git@github.com/owner/repo.git (fetch)\n",
        )

        result = _detect_repo()

        assert result == "owner/repo"

    @patch("egg_babysit.cli.subprocess.run")
    def test_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        result = _detect_repo()

        assert result == ""

    @patch("egg_babysit.cli.subprocess.run")
    def test_exception_returns_empty(self, mock_run):
        mock_run.side_effect = Exception("git not found")

        result = _detect_repo()

        assert result == ""

    @patch("egg_babysit.cli.subprocess.run")
    def test_no_fetch_line_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="origin\thttps://github.com/owner/repo.git (push)\n",
        )

        result = _detect_repo()

        assert result == ""


class TestMain:
    """Test main() CLI entry point."""

    @patch("egg_babysit.cli._register_pipeline")
    @patch("egg_babysit.cli.babysit")
    @patch("egg_babysit.cli._detect_repo")
    @patch("sys.argv", ["egg-babysit", "42", "--repo", "owner/repo"])
    def test_merged_exit_code_0(self, mock_detect, mock_babysit, mock_register):
        mock_babysit.return_value = BabysitResult(
            exit_reason=BabysitExitReason.MERGED,
            iterations=3,
            duration_seconds=60.0,
            last_step=BabysitStep.DONE,
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0

    @patch("egg_babysit.cli._register_pipeline")
    @patch("egg_babysit.cli.babysit")
    @patch("egg_babysit.cli._detect_repo")
    @patch("sys.argv", ["egg-babysit", "42", "--repo", "owner/repo"])
    def test_escalated_exit_code_0(self, mock_detect, mock_babysit, mock_register):
        mock_babysit.return_value = BabysitResult(
            exit_reason=BabysitExitReason.ESCALATED,
            iterations=2,
            duration_seconds=30.0,
            last_step=BabysitStep.CHECK_CONFLICTS,
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0

    @patch("egg_babysit.cli._register_pipeline")
    @patch("egg_babysit.cli.babysit")
    @patch("egg_babysit.cli._detect_repo")
    @patch("sys.argv", ["egg-babysit", "42", "--repo", "owner/repo"])
    def test_timeout_exit_code_1(self, mock_detect, mock_babysit, mock_register):
        mock_babysit.return_value = BabysitResult(
            exit_reason=BabysitExitReason.TIMEOUT,
            iterations=10,
            duration_seconds=14400.0,
            last_step=BabysitStep.WAIT_CI,
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    @patch("egg_babysit.cli._register_pipeline")
    @patch("egg_babysit.cli.babysit")
    @patch("egg_babysit.cli._detect_repo")
    @patch("sys.argv", ["egg-babysit", "42", "--repo", "owner/repo"])
    def test_error_exit_code_1(self, mock_detect, mock_babysit, mock_register):
        mock_babysit.return_value = BabysitResult(
            exit_reason=BabysitExitReason.ERROR,
            iterations=1,
            duration_seconds=5.0,
            last_step=BabysitStep.CHECK_CONFLICTS,
            message="Something broke",
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    @patch("egg_babysit.cli._register_pipeline")
    @patch("egg_babysit.cli.babysit")
    @patch("egg_babysit.cli._detect_repo", return_value="auto/repo")
    @patch("sys.argv", ["egg-babysit", "42"])
    def test_auto_detect_repo(self, mock_detect, mock_babysit, mock_register):
        mock_babysit.return_value = BabysitResult(
            exit_reason=BabysitExitReason.MERGED,
            iterations=1,
            duration_seconds=10.0,
            last_step=BabysitStep.DONE,
        )

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0
        # Verify babysit was called with the auto-detected repo.
        config = mock_babysit.call_args[0][0]
        assert config.repo == "auto/repo"

    @patch("egg_babysit.cli._detect_repo", return_value="")
    @patch("sys.argv", ["egg-babysit", "42"])
    def test_missing_repo_exits_1(self, mock_detect):
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    @patch("egg_babysit.cli._register_pipeline")
    @patch("egg_babysit.cli.babysit")
    @patch(
        "sys.argv",
        [
            "egg-babysit",
            "42",
            "--repo",
            "owner/repo",
            "--timeout",
            "3600",
            "--max-iterations",
            "5",
            "--poll-interval",
            "60",
            "--max-retries",
            "2",
            "--max-feedback-rounds",
            "3",
        ],
    )
    def test_custom_args_passed_to_config(self, mock_babysit, mock_register):
        mock_babysit.return_value = BabysitResult(
            exit_reason=BabysitExitReason.MERGED,
            iterations=1,
            duration_seconds=10.0,
            last_step=BabysitStep.DONE,
        )

        with pytest.raises(SystemExit):
            main()

        config = mock_babysit.call_args[0][0]
        assert config.pr_number == 42
        assert config.repo == "owner/repo"
        assert config.timeout_seconds == 3600
        assert config.max_iterations == 5
        assert config.poll_interval_seconds == 60
        assert config.max_retries_per_job == 2
        assert config.max_feedback_rounds == 3


class TestRegisterPipeline:
    """Test _register_pipeline."""

    @patch("egg_babysit.cli.subprocess.run")
    def test_skips_when_no_orchestrator_url(self, mock_run):
        from egg_babysit.cli import _register_pipeline

        config = MagicMock()
        config.orchestrator_url = ""

        _register_pipeline(config)

        mock_run.assert_not_called()

    @patch("egg_babysit.cli.subprocess.run")
    def test_calls_egg_orch(self, mock_run):
        from egg_babysit.cli import _register_pipeline

        config = MagicMock()
        config.orchestrator_url = "http://localhost:9999"
        config.pr_number = 42
        config.repo = "owner/repo"

        mock_run.return_value = MagicMock(returncode=0)

        _register_pipeline(config)

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "egg-orch" in call_args

    @patch("egg_babysit.cli.subprocess.run")
    def test_handles_file_not_found(self, mock_run):
        from egg_babysit.cli import _register_pipeline

        config = MagicMock()
        config.orchestrator_url = "http://localhost:9999"

        mock_run.side_effect = FileNotFoundError("egg-orch not found")

        # Should not raise.
        _register_pipeline(config)
