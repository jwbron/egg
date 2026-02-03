"""Tests for gateway git_client module.

Migrated from james-in-a-box/tests/gateway_sidecar/test_git_client.py
with additional enhancements for coverage.
"""

from gateway.git_client import (
    BLOCKED_GIT_FLAGS,
    GIT_ALLOWED_COMMANDS,
    get_authenticated_remote_target,
    is_repos_parent_directory,
    is_ssh_url,
    normalize_flag,
    ssh_url_to_https,
    validate_git_args,
)


class TestIsSshUrl:
    """Tests for is_ssh_url function."""

    def test_git_at_url_is_ssh(self):
        """git@github.com URLs are SSH."""
        assert is_ssh_url("git@github.com:owner/repo.git") is True

    def test_ssh_protocol_is_ssh(self):
        """ssh:// URLs are SSH."""
        assert is_ssh_url("ssh://git@github.com/owner/repo.git") is True

    def test_https_url_is_not_ssh(self):
        """HTTPS URLs are not SSH."""
        assert is_ssh_url("https://github.com/owner/repo.git") is False

    def test_http_url_is_not_ssh(self):
        """HTTP URLs are not SSH."""
        assert is_ssh_url("http://github.com/owner/repo.git") is False

    def test_empty_string(self):
        """Empty string is not SSH."""
        assert is_ssh_url("") is False

    def test_git_protocol_url(self):
        """git:// protocol URLs are not SSH."""
        assert is_ssh_url("git://github.com/owner/repo.git") is False


class TestSshUrlToHttps:
    """Tests for ssh_url_to_https function."""

    def test_convert_git_at_with_dot_git(self):
        """Convert git@github.com:owner/repo.git to HTTPS."""
        result = ssh_url_to_https("git@github.com:owner/repo.git")
        assert result == "https://github.com/owner/repo.git"

    def test_convert_git_at_without_dot_git(self):
        """Convert git@github.com:owner/repo (no .git) to HTTPS."""
        result = ssh_url_to_https("git@github.com:owner/repo")
        assert result == "https://github.com/owner/repo.git"

    def test_convert_ssh_protocol_with_dot_git(self):
        """Convert ssh://git@github.com/owner/repo.git to HTTPS."""
        result = ssh_url_to_https("ssh://git@github.com/owner/repo.git")
        assert result == "https://github.com/owner/repo.git"

    def test_convert_ssh_protocol_without_dot_git(self):
        """Convert ssh://git@github.com/owner/repo (no .git) to HTTPS."""
        result = ssh_url_to_https("ssh://git@github.com/owner/repo")
        assert result == "https://github.com/owner/repo.git"

    def test_https_url_unchanged(self):
        """HTTPS URLs are returned unchanged."""
        url = "https://github.com/owner/repo.git"
        assert ssh_url_to_https(url) == url

    def test_http_url_unchanged(self):
        """HTTP URLs are returned unchanged."""
        url = "http://github.com/owner/repo.git"
        assert ssh_url_to_https(url) == url

    def test_preserves_owner_and_repo(self):
        """Owner and repo names are preserved in conversion."""
        result = ssh_url_to_https("git@github.com:jwbron/james-in-a-box.git")
        assert result == "https://github.com/jwbron/james-in-a-box.git"

    def test_preserves_nested_owner(self):
        """Handles nested paths like org/repo correctly."""
        result = ssh_url_to_https("git@github.com:Khan/webapp.git")
        assert result == "https://github.com/Khan/webapp.git"


class TestGetAuthenticatedRemoteTarget:
    """Tests for get_authenticated_remote_target function."""

    def test_ssh_url_returns_https(self):
        """SSH URLs are converted to HTTPS."""
        result = get_authenticated_remote_target("origin", "git@github.com:owner/repo.git")
        assert result == "https://github.com/owner/repo.git"

    def test_ssh_protocol_returns_https(self):
        """ssh:// URLs are converted to HTTPS."""
        result = get_authenticated_remote_target("origin", "ssh://git@github.com/owner/repo.git")
        assert result == "https://github.com/owner/repo.git"

    def test_https_url_returns_remote_name(self):
        """HTTPS URLs return the remote name (no conversion needed)."""
        result = get_authenticated_remote_target("origin", "https://github.com/owner/repo.git")
        assert result == "origin"

    def test_http_url_returns_remote_name(self):
        """HTTP URLs return the remote name."""
        result = get_authenticated_remote_target("upstream", "http://github.com/owner/repo.git")
        assert result == "upstream"

    def test_custom_remote_name_returned_for_https(self):
        """Custom remote names are preserved for HTTPS URLs."""
        result = get_authenticated_remote_target("my-remote", "https://github.com/owner/repo.git")
        assert result == "my-remote"

    def test_ssh_url_ignores_remote_name(self):
        """For SSH URLs, the remote name is ignored in favor of HTTPS URL."""
        result = get_authenticated_remote_target(
            "my-custom-remote", "git@github.com:owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"


class TestGitAllowedCommands:
    """Tests for GIT_ALLOWED_COMMANDS and validate_git_args."""

    def test_common_operations_in_allowlist(self):
        """Verify common operations are in allowlist."""
        common_ops = [
            "status",
            "log",
            "diff",
            "branch",
            "checkout",
            "add",
            "commit",
            "stash",
            "show",
        ]
        for op in common_ops:
            assert op in GIT_ALLOWED_COMMANDS, f"{op} should be in allowed commands"

    def test_new_operations_in_allowlist(self):
        """Verify new operations (rm, mv, blame, reflog, describe) are in allowlist."""
        new_ops = ["rm", "mv", "blame", "reflog", "describe"]
        for op in new_ops:
            assert op in GIT_ALLOWED_COMMANDS, f"{op} should be in allowed commands"

    def test_rm_validates_common_flags(self):
        """git rm accepts common flags."""
        valid, err, _ = validate_git_args("rm", ["--cached", "file.txt"])
        assert valid, f"git rm --cached should be valid: {err}"

        valid, err, _ = validate_git_args("rm", ["-r", "--dry-run", "dir/"])
        assert valid, f"git rm -r --dry-run should be valid: {err}"

    def test_mv_validates_common_flags(self):
        """git mv accepts common flags."""
        valid, err, _ = validate_git_args("mv", ["-f", "old.py", "new.py"])
        assert valid, f"git mv -f should be valid: {err}"

    def test_blame_validates_common_flags(self):
        """git blame accepts common flags."""
        valid, err, _ = validate_git_args("blame", ["-L", "1,10", "file.py"])
        assert valid, f"git blame -L should be valid: {err}"

    def test_reflog_validates_common_flags(self):
        """git reflog accepts common flags (use --max-count, not -n)."""
        valid, err, _ = validate_git_args("reflog", ["--oneline", "--max-count", "10"])
        assert valid, f"git reflog --max-count should be valid: {err}"

    def test_reflog_rejects_n_flag(self):
        """git reflog rejects -n flag (normalized to --dry-run globally)."""
        valid, _err, _ = validate_git_args("reflog", ["-n", "10"])
        assert not valid, "-n should be rejected for reflog (normalized to --dry-run)"

    def test_describe_validates_common_flags(self):
        """git describe accepts common flags."""
        valid, err, _ = validate_git_args("describe", ["--tags", "--always"])
        assert valid, f"git describe --tags --always should be valid: {err}"

    def test_blocked_flags_rejected(self):
        """Dangerous flags are rejected for all operations."""
        for op in ["rm", "mv", "blame"]:
            valid, _err, _ = validate_git_args(op, ["--exec=evil"])
            assert not valid, f"--exec should be rejected for {op}"


class TestValidateGitArgs:
    """Additional tests for validate_git_args function."""

    def test_empty_args_allowed(self):
        """Empty args should be allowed for most commands."""
        valid, err, validated = validate_git_args("status", [])
        assert valid, f"Empty args should be valid for status: {err}"
        assert validated == []

    def test_file_paths_passed_through(self):
        """File paths should be passed through."""
        valid, err, validated = validate_git_args("add", ["src/main.py", "tests/"])
        assert valid, f"File paths should be valid: {err}"
        assert "src/main.py" in validated
        assert "tests/" in validated

    def test_dangerous_exec_flag_blocked(self):
        """--exec flag should be blocked."""
        valid, err, _ = validate_git_args("log", ["--exec=/bin/evil"])
        assert not valid
        assert "exec" in err.lower() or "blocked" in err.lower()

    def test_commit_message_allowed(self):
        """Commit with -m flag should be allowed."""
        valid, err, validated = validate_git_args("commit", ["-m", "Test message"])
        assert valid, f"Commit with message should be valid: {err}"
        assert "-m" in validated
        assert "Test message" in validated

    def test_checkout_branch_allowed(self):
        """Checkout with branch name should be allowed."""
        valid, err, validated = validate_git_args("checkout", ["-b", "new-branch"])
        assert valid, f"Checkout -b should be valid: {err}"

    def test_log_with_format_allowed(self):
        """Log with format options should be allowed."""
        valid, err, validated = validate_git_args("log", ["--oneline", "--graph", "-10"])
        assert valid, f"Log with format should be valid: {err}"

    def test_diff_with_options_allowed(self):
        """Diff with various options should be allowed."""
        valid, err, validated = validate_git_args("diff", ["--stat", "--cached", "HEAD~1"])
        assert valid, f"Diff with options should be valid: {err}"

    def test_stash_subcommands_allowed(self):
        """Stash subcommands should be allowed."""
        for subcommand in ["push", "pop", "list", "apply", "drop"]:
            valid, err, validated = validate_git_args("stash", [subcommand])
            assert valid, f"stash {subcommand} should be valid: {err}"


class TestIsReposParentDirectory:
    """Tests for repos parent directory detection."""

    def test_repos_parent_detected(self):
        """The /home/user/repos directory should be detected as a parent."""
        assert is_repos_parent_directory("/home/user/repos")
        assert is_repos_parent_directory("/home/user/repos/")

    def test_worktrees_parent_detected(self):
        """The /home/user/.egg-worktrees directory should be detected as a parent."""
        assert is_repos_parent_directory("/home/user/.egg-worktrees")
        assert is_repos_parent_directory("/home/user/.egg-worktrees/")

    def test_legacy_repos_parent_detected(self):
        """The /repos directory should be detected as a parent."""
        assert is_repos_parent_directory("/repos")
        assert is_repos_parent_directory("/repos/")

    def test_actual_repo_not_detected(self):
        """Paths inside repos should NOT be detected as parent directories."""
        assert not is_repos_parent_directory("/home/user/repos/myrepo")
        assert not is_repos_parent_directory("/home/user/repos/some-project/src")
        assert not is_repos_parent_directory("/home/user/.egg-worktrees/container-123/myrepo")

    def test_empty_path(self):
        """Empty paths should return False."""
        assert not is_repos_parent_directory("")
        assert not is_repos_parent_directory(None)

    def test_unrelated_paths(self):
        """Unrelated paths should return False."""
        assert not is_repos_parent_directory("/tmp")
        assert not is_repos_parent_directory("/home/user")
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


class TestBlockedGitFlags:
    """Tests for blocked flags configuration."""

    def test_dangerous_flags_blocked(self):
        """Known dangerous flags should be blocked."""
        assert "--upload-pack" in BLOCKED_GIT_FLAGS
        assert "--exec" in BLOCKED_GIT_FLAGS
        assert "-c" in BLOCKED_GIT_FLAGS
        assert "--config" in BLOCKED_GIT_FLAGS
        assert "--receive-pack" in BLOCKED_GIT_FLAGS


class TestValidateRepoPath:
    """Tests for validate_repo_path function."""

    def test_valid_repo_path(self):
        """Valid repo path should be accepted."""
        from gateway.git_client import validate_repo_path

        valid, err = validate_repo_path("/home/user/repos/my-repo")
        assert valid, f"Valid path should be accepted: {err}"

    def test_rejects_path_traversal(self):
        """Path traversal attempts should be rejected."""
        from gateway.git_client import validate_repo_path

        valid, err = validate_repo_path("/home/user/repos/../../../etc/passwd")
        assert not valid
        # Error message is about path not being within allowed directories
        assert "allowed directories" in err.lower() or "invalid" in err.lower()

    def test_rejects_empty_path(self):
        """Empty path should be rejected."""
        from gateway.git_client import validate_repo_path

        valid, err = validate_repo_path("")
        assert not valid

    def test_none_path_rejected(self):
        """None path should be rejected."""
        from gateway.git_client import validate_repo_path

        valid, _err = validate_repo_path(None)
        assert not valid

    def test_outside_allowed_paths_rejected(self):
        """Paths outside allowed directories should be rejected."""
        from gateway.git_client import validate_repo_path

        valid, err = validate_repo_path("/tmp/malicious")
        assert not valid
        assert "allowed directories" in err.lower()
