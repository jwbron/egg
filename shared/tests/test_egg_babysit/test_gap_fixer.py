"""Gap tests for egg_babysit.fixer — timeout calc, repo path, edge cases."""

import os
from unittest.mock import MagicMock, patch

from egg_babysit.config import BabysitConfig
from egg_babysit.fixer import FixerResult, _agent_timeout, _repo_path, run_fixer


class TestAgentTimeout:
    """Test _agent_timeout calculation."""

    def test_timeout_uses_half_remaining(self):
        """Timeout is half of remaining time."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=1000)
        # 200s elapsed, remaining=800, half=400
        result = _agent_timeout(config, elapsed=200)
        assert result == 400

    def test_timeout_minimum_300(self):
        """Timeout has a minimum floor of 300 seconds."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=100)
        # 50s elapsed, remaining=50, half=25 -> clamped to 300
        result = _agent_timeout(config, elapsed=50)
        assert result == 300

    def test_timeout_with_zero_elapsed(self):
        """With no elapsed time, full timeout is halved."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=600)
        result = _agent_timeout(config, elapsed=0)
        assert result == 300

    def test_timeout_when_elapsed_exceeds_total(self):
        """When elapsed exceeds timeout, result is minimum 300."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=100)
        result = _agent_timeout(config, elapsed=200)
        assert result == 300

    def test_timeout_large_remaining(self):
        """Large remaining time yields large timeout."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=14400)
        result = _agent_timeout(config, elapsed=0)
        assert result == 7200


class TestRepoPath:
    """Test _repo_path resolution."""

    @patch.dict(os.environ, {"EGG_REPO_PATH": "/home/egg/repos/test"})
    def test_repo_path_from_env(self):
        """Uses EGG_REPO_PATH env var when set."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        assert _repo_path(config) == "/home/egg/repos/test"

    @patch.dict(os.environ, {}, clear=True)
    def test_repo_path_fallback_to_dot(self):
        """Falls back to '.' when env var not set."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        # Remove EGG_REPO_PATH if it exists
        env = os.environ.copy()
        env.pop("EGG_REPO_PATH", None)
        with patch.dict(os.environ, env, clear=True):
            assert _repo_path(config) == "."


class TestRunFixerEdgeCases:
    """Edge case tests for run_fixer."""

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_pre_sha_none_still_works(self, mock_build, mock_run, mock_sha):
        """When pre-SHA is None (git error), fixer still works."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=600)
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="Done", stderr="")
        # Pre-sha returns None (git error), post-sha returns a value
        mock_sha.side_effect = [None, "new_sha"]

        result = run_fixer("Fix it", config, "test_step")

        # Should succeed, commit_sha should be new_sha (since None != new_sha)
        assert result.success is True
        assert result.commit_sha == "new_sha"

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_post_sha_none_no_commit(self, mock_build, mock_run, mock_sha):
        """When post-SHA is None (git error), no commit detected."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=600)
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="Done", stderr="")
        mock_sha.side_effect = ["old_sha", None]

        result = run_fixer("Fix it", config, "test_step")

        assert result.success is True
        assert result.commit_sha is None

    @patch("egg_babysit.fixer._get_head_sha")
    @patch("egg_babysit.fixer.subprocess.run")
    @patch("egg_babysit.fixer.build_agent_command")
    def test_agent_nonzero_exit_with_empty_stderr(self, mock_build, mock_run, mock_sha):
        """Non-zero exit with empty stderr gives a default error message."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=600)
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="")
        mock_sha.return_value = "sha"

        result = run_fixer("Fix it", config, "test_step")

        assert result.success is False
        assert "2" in result.error  # Should include exit code


class TestFixerResultDataclass:
    """Test FixerResult edge cases."""

    def test_default_values(self):
        """FixerResult defaults for optional fields."""
        result = FixerResult(success=True)
        assert result.commit_sha is None
        assert result.error is None

    def test_all_fields_populated(self):
        """FixerResult with all fields set."""
        result = FixerResult(success=False, commit_sha="abc", error="failed")
        assert result.success is False
        assert result.commit_sha == "abc"
        assert result.error == "failed"
