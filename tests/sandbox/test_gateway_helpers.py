"""Tests for egg_lib.gateway helper functions.

Tests the pure/testable helper functions that don't require Docker.
"""

from pathlib import Path
from unittest.mock import patch

from egg_lib.gateway import (
    CONTAINER_HOME,
    GATEWAY_BUILD_HASH_LABEL,
    LAUNCHER_SECRET_FILE,
    _get_user_git_config,
    _hash_directory,
    _hash_file,
    _load_secrets,
    _parse_git_mounts,
)


class TestHashFile:
    """Tests for _hash_file function."""

    def test_hash_file_contents(self, tmp_path):
        """File contents are added to hasher."""
        import hashlib

        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")

        h1 = hashlib.sha256()
        _hash_file(f, h1)

        h2 = hashlib.sha256()
        h2.update(b"hello world")

        assert h1.hexdigest() == h2.hexdigest()

    def test_hash_nonexistent_file(self, tmp_path):
        """Non-existent file is silently skipped."""
        import hashlib

        h = hashlib.sha256()
        initial = h.hexdigest()
        _hash_file(tmp_path / "nonexistent.txt", h)
        # Hash should be unchanged (no data added) but hexdigest
        # is called on the same empty hasher, so same value
        assert h.hexdigest() == initial


class TestHashDirectory:
    """Tests for _hash_directory function."""

    def test_hash_directory(self, tmp_path):
        """Hashes all files in directory."""
        import hashlib

        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")

        h = hashlib.sha256()
        _hash_directory(tmp_path, h)
        result = h.hexdigest()

        # Should produce a deterministic hash
        h2 = hashlib.sha256()
        _hash_directory(tmp_path, h2)
        assert h2.hexdigest() == result

    def test_hash_directory_nonexistent(self, tmp_path):
        """Non-existent directory is handled gracefully."""
        import hashlib

        h = hashlib.sha256()
        _hash_directory(tmp_path / "nonexistent", h)
        # Should not raise

    def test_hash_directory_exclude_tests(self, tmp_path):
        """Test files are excluded when exclude_tests=True."""
        import hashlib

        (tmp_path / "module.py").write_text("code")
        (tmp_path / "test_module.py").write_text("test code")

        h1 = hashlib.sha256()
        _hash_directory(tmp_path, h1, exclude_tests=True)

        h2 = hashlib.sha256()
        _hash_directory(tmp_path, h2, exclude_tests=False)

        # Hashes should differ since test file is excluded in h1
        assert h1.hexdigest() != h2.hexdigest()

    def test_skips_dotfiles(self, tmp_path):
        """Files starting with . are skipped."""
        import hashlib

        (tmp_path / "visible.txt").write_text("data")
        (tmp_path / ".hidden").write_text("secret")

        h1 = hashlib.sha256()
        _hash_directory(tmp_path, h1)

        # Remove hidden file and hash again
        (tmp_path / ".hidden").unlink()
        h2 = hashlib.sha256()
        _hash_directory(tmp_path, h2)

        # Should be the same since dotfiles are skipped
        assert h1.hexdigest() == h2.hexdigest()


class TestLoadSecrets:
    """Tests for _load_secrets function."""

    def test_load_secrets(self, tmp_path, monkeypatch):
        """Parse secrets.env file."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "KEY1=value1\n"
            'KEY2="quoted value"\n'
            "KEY3='single quoted'\n"
            "# comment line\n"
            "\n"
            "KEY4=no_quotes\n"
        )
        # Patch Config.USER_CONFIG_DIR
        with patch("egg_lib.gateway.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _load_secrets()

        assert result["KEY1"] == "value1"
        assert result["KEY2"] == "quoted value"
        assert result["KEY3"] == "single quoted"
        assert result["KEY4"] == "no_quotes"
        assert "# comment line" not in result

    def test_load_secrets_missing_file(self, tmp_path):
        """Return empty dict when file doesn't exist."""
        with patch("egg_lib.gateway.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _load_secrets()
        assert result == {}


class TestParseGitMounts:
    """Tests for _parse_git_mounts function."""

    def test_parse_valid_config(self, tmp_path):
        """Parse valid repositories.yaml with real directories."""
        repo1 = tmp_path / "repo1"
        repo1.mkdir()
        git_dir = repo1 / ".git"
        git_dir.mkdir()

        config = tmp_path / "repos.yaml"
        config.write_text(f"local_repos:\n  paths:\n    - {repo1}\n")

        mounts = _parse_git_mounts(config, "/home/user")
        assert len(mounts) == 1
        assert str(git_dir) in mounts[0]
        assert "/home/user/.git-main/repo1" in mounts[0]

    def test_parse_worktree_gitfile(self, tmp_path):
        """Handle git worktree .git file (not directory)."""
        repo = tmp_path / "worktree"
        repo.mkdir()

        # Simulate a worktree .git file pointing elsewhere
        actual_git = tmp_path / "actual-git-dir"
        actual_git.mkdir()

        gitfile = repo / ".git"
        gitfile.write_text(f"gitdir: {actual_git}")

        config = tmp_path / "repos.yaml"
        config.write_text(f"local_repos:\n  paths:\n    - {repo}\n")

        mounts = _parse_git_mounts(config, "/home/user")
        assert len(mounts) == 1
        assert str(actual_git) in mounts[0]

    def test_parse_nonexistent_config(self, tmp_path):
        """Return empty for missing config file."""
        mounts = _parse_git_mounts(tmp_path / "missing.yaml", "/home/user")
        assert mounts == []

    def test_parse_nonexistent_repo(self, tmp_path):
        """Skip repos that don't exist on disk."""
        config = tmp_path / "repos.yaml"
        config.write_text("local_repos:\n  paths:\n    - /nonexistent/repo\n")

        mounts = _parse_git_mounts(config, "/home/user")
        assert mounts == []

    def test_parse_empty_config(self, tmp_path):
        """Handle empty YAML file."""
        config = tmp_path / "repos.yaml"
        config.write_text("")

        mounts = _parse_git_mounts(config, "/home/user")
        assert mounts == []


class TestGetUserGitConfig:
    """Tests for _get_user_git_config function."""

    def test_valid_config(self, tmp_path):
        """Extract git name and email from config."""
        config = tmp_path / "repos.yaml"
        config.write_text("user_mode:\n  git_name: Test User\n  git_email: test@example.com\n")
        name, email = _get_user_git_config(config)
        assert name == "Test User"
        assert email == "test@example.com"

    def test_missing_file(self, tmp_path):
        """Return None,None for missing file."""
        name, email = _get_user_git_config(tmp_path / "missing.yaml")
        assert name is None
        assert email is None

    def test_no_user_mode(self, tmp_path):
        """Return None,None when user_mode not in config."""
        config = tmp_path / "repos.yaml"
        config.write_text("local_repos:\n  paths: []\n")
        name, email = _get_user_git_config(config)
        assert name is None
        assert email is None

    def test_partial_config(self, tmp_path):
        """Handle config with only name, no email."""
        config = tmp_path / "repos.yaml"
        config.write_text("user_mode:\n  git_name: Only Name\n")
        name, email = _get_user_git_config(config)
        assert name == "Only Name"
        assert email is None


class TestConstants:
    """Tests for module-level constants."""

    def test_container_home(self):
        """Container home is /home/egg."""
        assert CONTAINER_HOME == "/home/egg"

    def test_build_hash_label(self):
        """Build hash label follows Docker convention."""
        assert "org.egg" in GATEWAY_BUILD_HASH_LABEL

    def test_launcher_secret_file_is_path(self):
        """Launcher secret file is a Path."""
        assert isinstance(LAUNCHER_SECRET_FILE, Path)
