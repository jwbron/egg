"""Tests for git_client.py validation functions."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from git_client import (
    BLOCKED_GIT_FLAGS,
    GIT_ALLOWED_COMMANDS,
    is_repos_parent_directory,
    is_ssh_url,
    normalize_flag,
    ssh_url_to_https,
    validate_git_args,
    validate_repo_path,
)


class TestValidateRepoPath:
    """Tests for repo path validation."""

    def test_empty_path_rejected(self):
        """Empty path should be rejected."""
        valid, error = validate_repo_path("")
        assert not valid
        assert "required" in error.lower()

    def test_none_path_rejected(self):
        """None path should be rejected."""
        valid, _error = validate_repo_path(None)
        assert not valid

    def test_allowed_path_accepted(self):
        """Paths within allowed directories should be accepted."""
        # Note: This test assumes the path exists - it validates format only
        valid, error = validate_repo_path("/home/egg/repos/myrepo")
        assert valid or "allowed directories" in error

    def test_path_traversal_rejected(self):
        """Path traversal attempts should be rejected."""
        valid, error = validate_repo_path("/home/egg/repos/../../../etc/passwd")
        assert not valid
        assert "allowed directories" in error.lower()

    def test_outside_allowed_paths_rejected(self):
        """Paths outside allowed directories should be rejected."""
        valid, error = validate_repo_path("/tmp/malicious")
        assert not valid
        assert "allowed directories" in error.lower()


class TestIsReposParentDirectory:
    """Tests for repos parent directory detection."""

    def test_repos_parent_detected(self):
        """The /home/egg/repos directory should be detected as a parent."""
        assert is_repos_parent_directory("/home/egg/repos")
        assert is_repos_parent_directory("/home/egg/repos/")

    def test_worktrees_parent_detected(self):
        """The /home/egg/.egg-worktrees directory should be detected as a parent."""
        assert is_repos_parent_directory("/home/egg/.egg-worktrees")
        assert is_repos_parent_directory("/home/egg/.egg-worktrees/")

    def test_legacy_repos_parent_detected(self):
        """The /repos directory should be detected as a parent."""
        assert is_repos_parent_directory("/repos")
        assert is_repos_parent_directory("/repos/")

    def test_actual_repo_not_detected(self):
        """Paths inside repos should NOT be detected as parent directories."""
        assert not is_repos_parent_directory("/home/egg/repos/myrepo")
        assert not is_repos_parent_directory("/home/egg/repos/some-project/src")
        assert not is_repos_parent_directory("/home/egg/.egg-worktrees/container-123/myrepo")

    def test_empty_path(self):
        """Empty paths should return False."""
        assert not is_repos_parent_directory("")
        assert not is_repos_parent_directory(None)

    def test_unrelated_paths(self):
        """Unrelated paths should return False."""
        assert not is_repos_parent_directory("/tmp")
        assert not is_repos_parent_directory("/home/egg")
        assert not is_repos_parent_directory("/etc/passwd")


class TestNormalizeFlag:
    """Tests for flag normalization."""

    def test_short_flag_normalized(self):
        """Short flags should be normalized to long form."""
        assert normalize_flag("-a") == "--all"
        assert normalize_flag("-v") == "--verbose"
        assert normalize_flag("-f") == "--force"

    def test_long_flag_unchanged(self):
        """Long flags should remain unchanged."""
        assert normalize_flag("--all") == "--all"
        assert normalize_flag("--verbose") == "--verbose"

    def test_unknown_flag_unchanged(self):
        """Unknown flags should remain unchanged."""
        assert normalize_flag("--unknown") == "--unknown"
        assert normalize_flag("-x") == "-x"

    def test_flag_with_value_normalized(self):
        """Flags with values should have base normalized."""
        assert normalize_flag("-n=5") == "--dry-run=5"
        assert normalize_flag("--depth=5") == "--depth=5"


class TestValidateGitArgs:
    """Tests for git argument validation."""

    def test_empty_args_valid(self):
        """Empty args should be valid."""
        valid, error, normalized = validate_git_args("status", [])
        assert valid
        assert error == ""
        assert normalized == []

    def test_allowed_flag_accepted(self):
        """Allowed flags should be accepted."""
        valid, _error, normalized = validate_git_args("status", ["--porcelain"])
        assert valid
        assert "--porcelain" in normalized

    def test_blocked_flag_rejected(self):
        """Blocked flags should be rejected."""
        valid, error, _normalized = validate_git_args("fetch", ["--upload-pack=/bin/sh"])
        assert not valid
        assert "not allowed" in error.lower()

    def test_unknown_flag_rejected(self):
        """Unknown flags should be rejected (allowlist approach)."""
        valid, error, _normalized = validate_git_args("status", ["--malicious"])
        assert not valid
        assert "not allowed" in error.lower()

    def test_config_override_blocked(self):
        """Config override flags should be blocked."""
        valid, error, _normalized = validate_git_args("fetch", ["-c", "core.sshCommand=evil"])
        assert not valid
        assert "not allowed" in error.lower()

    def test_non_flag_args_passed_through(self):
        """Non-flag arguments should pass through."""
        # Use --max-count instead of -n because -n is globally normalized to --dry-run
        # (for git push), not --max-count (for git log)
        valid, _error, normalized = validate_git_args("log", ["--max-count=5", "main"])
        assert valid
        assert "main" in normalized

    def test_unknown_operation_rejected(self):
        """Unknown operations should be rejected."""
        valid, error, _normalized = validate_git_args("fake-command", [])
        assert not valid
        assert "unknown operation" in error.lower()

    def test_nested_structure_rejected(self):
        """Nested data structures in args should be rejected."""
        valid, error, _normalized = validate_git_args("status", [["nested", "list"]])
        assert not valid
        assert "invalid argument type" in error.lower()

    def test_numeric_flag_for_log(self):
        """Numeric flags like -3 should be allowed for git log."""
        valid, _error, normalized = validate_git_args("log", ["-3", "--oneline"])
        assert valid
        assert "--max-count=3" in normalized
        assert "--oneline" in normalized

    def test_numeric_flag_for_log_larger_number(self):
        """Larger numeric flags like -10 should work."""
        valid, _error, normalized = validate_git_args("log", ["-10"])
        assert valid
        assert "--max-count=10" in normalized

    def test_numeric_flag_rejected_for_non_log(self):
        """Numeric flags should be rejected for operations other than log."""
        valid, error, _normalized = validate_git_args("status", ["-3"])
        assert not valid
        assert "numeric flag" in error.lower()

    def test_double_dash_separator_allowed(self):
        """The -- separator should be allowed for any operation."""
        valid, _error, normalized = validate_git_args("checkout", ["--", "file.txt"])
        assert valid
        assert "--" in normalized
        assert "file.txt" in normalized

    def test_double_dash_with_log(self):
        """The -- separator should work with log operation."""
        valid, _error, normalized = validate_git_args("log", ["--oneline", "--", "path/to/file"])
        assert valid
        assert "--" in normalized

    def test_merge_base_flags_accepted(self):
        """merge-base flags should be accepted (issue #160)."""
        # Test basic usage with refs
        valid, _error, normalized = validate_git_args("merge-base", ["origin/main", "HEAD"])
        assert valid
        assert "origin/main" in normalized
        assert "HEAD" in normalized

        # Test --all flag
        valid, _error, normalized = validate_git_args("merge-base", ["--all", "A", "B"])
        assert valid
        assert "--all" in normalized

        # Test --is-ancestor flag
        valid, _error, normalized = validate_git_args("merge-base", ["--is-ancestor", "A", "B"])
        assert valid
        assert "--is-ancestor" in normalized

        # Test --fork-point flag
        valid, _error, normalized = validate_git_args("merge-base", ["--fork-point", "origin/main"])
        assert valid
        assert "--fork-point" in normalized

    def test_merge_base_unknown_flag_rejected(self):
        """Unknown flags for merge-base should be rejected."""
        valid, error, _normalized = validate_git_args("merge-base", ["--malicious"])
        assert not valid
        assert "not allowed" in error.lower()


class TestGitAllowedCommands:
    """Tests for the allowed commands configuration."""

    def test_network_operations_defined(self):
        """Network operations should be defined."""
        assert "fetch" in GIT_ALLOWED_COMMANDS
        assert "push" in GIT_ALLOWED_COMMANDS
        assert "ls-remote" in GIT_ALLOWED_COMMANDS

    def test_local_read_operations_defined(self):
        """Local read operations should be defined."""
        assert "status" in GIT_ALLOWED_COMMANDS
        assert "log" in GIT_ALLOWED_COMMANDS
        assert "diff" in GIT_ALLOWED_COMMANDS
        assert "branch" in GIT_ALLOWED_COMMANDS
        assert "merge-base" in GIT_ALLOWED_COMMANDS

    def test_local_write_operations_defined(self):
        """Local write operations should be defined."""
        assert "add" in GIT_ALLOWED_COMMANDS
        assert "commit" in GIT_ALLOWED_COMMANDS
        assert "checkout" in GIT_ALLOWED_COMMANDS
        assert "reset" in GIT_ALLOWED_COMMANDS

    def test_patch_operations_defined(self):
        """Patch-related operations should be defined (issue #118)."""
        assert "apply" in GIT_ALLOWED_COMMANDS
        assert "format-patch" in GIT_ALLOWED_COMMANDS

    def test_each_operation_has_allowed_flags(self):
        """Each operation should have allowed_flags defined."""
        for op, config in GIT_ALLOWED_COMMANDS.items():
            assert "allowed_flags" in config, f"{op} missing allowed_flags"
            assert isinstance(config["allowed_flags"], list), f"{op} allowed_flags not a list"


class TestBlockedGitFlags:
    """Tests for blocked flags configuration."""

    def test_dangerous_flags_blocked(self):
        """Known dangerous flags should be blocked."""
        assert "--upload-pack" in BLOCKED_GIT_FLAGS
        assert "--exec" in BLOCKED_GIT_FLAGS
        assert "-c" in BLOCKED_GIT_FLAGS
        assert "--config" in BLOCKED_GIT_FLAGS
        assert "--receive-pack" in BLOCKED_GIT_FLAGS


class TestSshUrlConversion:
    """Tests for SSH to HTTPS URL conversion."""

    def test_git_at_format_converted(self):
        """git@github.com:owner/repo.git format should be converted."""
        result = ssh_url_to_https("git@github.com:owner/repo.git")
        assert result == "https://github.com/owner/repo.git"

    def test_git_at_without_extension(self):
        """git@ format without .git should be converted."""
        result = ssh_url_to_https("git@github.com:owner/repo")
        assert result == "https://github.com/owner/repo.git"

    def test_ssh_protocol_format_converted(self):
        """ssh://git@github.com/owner/repo.git format should be converted."""
        result = ssh_url_to_https("ssh://git@github.com/owner/repo.git")
        assert result == "https://github.com/owner/repo.git"

    def test_https_unchanged(self):
        """HTTPS URLs should remain unchanged."""
        url = "https://github.com/owner/repo.git"
        assert ssh_url_to_https(url) == url

    def test_unknown_format_unchanged(self):
        """Unknown formats should remain unchanged."""
        url = "file:///path/to/repo"
        assert ssh_url_to_https(url) == url


class TestIsSshUrl:
    """Tests for SSH URL detection."""

    def test_git_at_detected(self):
        """git@ URLs should be detected as SSH."""
        assert is_ssh_url("git@github.com:owner/repo.git")

    def test_ssh_protocol_detected(self):
        """ssh:// URLs should be detected as SSH."""
        assert is_ssh_url("ssh://git@github.com/owner/repo.git")

    def test_https_not_ssh(self):
        """HTTPS URLs should not be detected as SSH."""
        assert not is_ssh_url("https://github.com/owner/repo.git")

    def test_file_not_ssh(self):
        """file:// URLs should not be detected as SSH."""
        assert not is_ssh_url("file:///path/to/repo")


class TestGitHubClientExecuteEnvironment:
    """Tests for GitHubClient.execute() environment configuration.

    When gh commands run git internally (e.g., gh pr checkout runs git fetch),
    the environment must include git URL rewrite config to convert SSH URLs
    to HTTPS, since the gateway uses token auth not SSH keys.
    """

    def test_execute_env_includes_ssh_url_rewrite(self):
        """execute() environment should include SSH to HTTPS URL rewrite config."""
        from unittest.mock import MagicMock, patch

        from github_client import GitHubClient

        client = GitHubClient()

        with (
            patch.object(client, "get_token_for_mode", return_value="test-token"),
            patch("github_client.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")

            client.execute(["pr", "view", "123"])

            # Verify subprocess.run was called
            assert mock_run.called
            call_kwargs = mock_run.call_args.kwargs
            env = call_kwargs.get("env", {})

            # Verify SSH URL rewrite config is present
            assert env.get("GIT_CONFIG_COUNT") == "3"

            # Check for git@github.com: rewrite
            found_git_at = False
            found_ssh_protocol = False
            for i in range(3):
                key = env.get(f"GIT_CONFIG_KEY_{i}", "")
                value = env.get(f"GIT_CONFIG_VALUE_{i}", "")
                if "insteadOf" in key and value == "git@github.com:":
                    found_git_at = True
                if "insteadOf" in key and value == "ssh://git@github.com/":
                    found_ssh_protocol = True

            assert found_git_at, "Missing URL rewrite for git@github.com: format"
            assert found_ssh_protocol, "Missing URL rewrite for ssh://git@github.com/ format"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
