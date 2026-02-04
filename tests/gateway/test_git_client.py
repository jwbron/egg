"""Tests for gateway git_client module."""

import sys
from pathlib import Path

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from git_client import (
    GIT_ALLOWED_COMMANDS,
    get_authenticated_remote_target,
    is_ssh_url,
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
        result = ssh_url_to_https("git@github.com:jwbron/egg.git")
        assert result == "https://github.com/jwbron/egg.git"

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
