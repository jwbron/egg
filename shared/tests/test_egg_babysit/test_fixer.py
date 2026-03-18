"""Tests for egg_babysit.fixer — fixer and non-LLM fix runners."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.fixer import FixerResult, run_fixer, run_non_llm_fix


@pytest.fixture
def config():
    return BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=600)


class TestRunNonLlmFix:
    """Test run_non_llm_fix shell command execution."""

    @patch("egg_babysit.fixer.subprocess.run")
    def test_run_non_llm_fix_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = run_non_llm_fix("make lint-fix", "/path/to/repo")

        assert result is True
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["shell"] is True
        assert call_kwargs.kwargs["cwd"] == "/path/to/repo"

    @patch("egg_babysit.fixer.subprocess.run")
    def test_run_non_llm_fix_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")

        result = run_non_llm_fix("make lint-fix", "/path/to/repo")

        assert result is False

    @patch("egg_babysit.fixer.subprocess.run")
    def test_run_non_llm_fix_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("make", 300)

        result = run_non_llm_fix("make lint-fix", "/path/to/repo")

        assert result is False

    @patch("egg_babysit.fixer.subprocess.run")
    def test_run_non_llm_fix_exception(self, mock_run):
        mock_run.side_effect = OSError("Command not found")

        result = run_non_llm_fix("bad-command", "/path/to/repo")

        assert result is False

    @patch.dict("os.environ", {"EGG_REPO_PATH": "/env/repo"})
    @patch("egg_babysit.fixer.subprocess.run")
    def test_run_non_llm_fix_uses_env_fallback(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_non_llm_fix("make fix", "")

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["cwd"] == "/env/repo"


class TestRunFixer:
    """Test run_fixer agent spawner."""

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_run_fixer_success_with_commit(self, mock_build, mock_run, mock_sha, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="Done", stderr="")
        mock_sha.side_effect = ["old_sha", "new_sha"]

        result = run_fixer("Fix the lint", config, "check_fix")

        assert result.success is True
        assert result.commit_sha == "new_sha"
        assert result.error is None

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_run_fixer_success_no_commit(self, mock_build, mock_run, mock_sha, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="Done", stderr="")
        mock_sha.side_effect = ["same_sha", "same_sha"]

        result = run_fixer("Fix the lint", config, "check_fix")

        assert result.success is True
        assert result.commit_sha is None

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_run_fixer_agent_fails(self, mock_build, mock_run, mock_sha, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Agent error")
        mock_sha.return_value = "sha"

        result = run_fixer("Fix the lint", config, "check_fix")

        assert result.success is False
        assert result.error is not None

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_run_fixer_timeout(self, mock_build, mock_run, mock_sha, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 300)
        mock_sha.return_value = "sha"

        result = run_fixer("Fix the lint", config, "check_fix")

        assert result.success is False
        assert "timed out" in result.error.lower()

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_run_fixer_unexpected_exception(self, mock_build, mock_run, mock_sha, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.side_effect = OSError("Something broke")
        mock_sha.return_value = "sha"

        result = run_fixer("Fix the lint", config, "check_fix")

        assert result.success is False
        assert result.error is not None


class TestFixerResult:
    """Test FixerResult dataclass."""

    def test_success_result(self):
        result = FixerResult(success=True, commit_sha="abc123")
        assert result.success is True
        assert result.commit_sha == "abc123"
        assert result.error is None

    def test_failure_result(self):
        result = FixerResult(success=False, error="Something failed")
        assert result.success is False
        assert result.commit_sha is None
        assert result.error == "Something failed"
