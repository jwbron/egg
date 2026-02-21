"""
Tests for checkpoint repo helper functions in repo_config module.

Tests get_all_checkpoint_repos() and is_checkpoint_repo() which are used
by the gateway to exempt checkpoint repos from private mode policy.
"""

import pytest

import config.repo_config as repo_config_module
from config.repo_config import get_all_checkpoint_repos, is_checkpoint_repo


@pytest.fixture(autouse=True)
def clear_checkpoint_cache():
    """Clear the checkpoint repos cache before each test."""
    repo_config_module._checkpoint_repos_cache = None
    yield
    repo_config_module._checkpoint_repos_cache = None


class TestGetAllCheckpointRepos:
    """Tests for get_all_checkpoint_repos function."""

    def test_empty_config(self, temp_dir, monkeypatch):
        """Returns empty set when no repo_settings configured."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser\n")
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == set()

    def test_no_checkpoint_repos(self, temp_dir, monkeypatch):
        """Returns empty set when repo_settings exist but no checkpoint_repo."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    restrict_to_configured_users: true\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == set()

    def test_single_checkpoint_repo(self, temp_dir, monkeypatch):
        """Returns set with one checkpoint repo."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checkpoint_repo: testuser/my-checkpoints\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == {"testuser/my-checkpoints"}

    def test_multiple_repos(self, temp_dir, monkeypatch):
        """Returns set with multiple distinct checkpoint repos."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/app-one:\n"
            "    checkpoint_repo: testuser/ckpt-one\n"
            "  testuser/app-two:\n"
            "    checkpoint_repo: testuser/ckpt-two\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == {"testuser/ckpt-one", "testuser/ckpt-two"}

    def test_deduplication(self, temp_dir, monkeypatch):
        """Multiple repos pointing to the same checkpoint repo are deduplicated."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/app-one:\n"
            "    checkpoint_repo: testuser/shared-ckpt\n"
            "  testuser/app-two:\n"
            "    checkpoint_repo: testuser/shared-ckpt\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == {"testuser/shared-ckpt"}

    def test_case_insensitivity(self, temp_dir, monkeypatch):
        """Checkpoint repo names are lowercased for comparison."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checkpoint_repo: TestUser/My-Checkpoints\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert "testuser/my-checkpoints" in result

    def test_config_unavailable_returns_empty(self, temp_dir, monkeypatch):
        """Returns empty set when config file cannot be loaded."""
        monkeypatch.setenv("EGG_REPO_CONFIG", str(temp_dir / "nonexistent.yaml"))
        monkeypatch.setenv("HOME", str(temp_dir))

        result = get_all_checkpoint_repos()
        assert result == set()

    def test_ignores_non_string_checkpoint_repo(self, temp_dir, monkeypatch):
        """Ignores checkpoint_repo values that are not strings."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checkpoint_repo: 12345\n"
            "  testuser/other-app:\n"
            "    checkpoint_repo: valid/repo\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == {"valid/repo"}

    def test_ignores_empty_checkpoint_repo(self, temp_dir, monkeypatch):
        """Ignores empty string checkpoint_repo values."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            '    checkpoint_repo: ""\n'
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_checkpoint_repos()
        assert result == set()


class TestIsCheckpointRepo:
    """Tests for is_checkpoint_repo function."""

    def test_match(self, temp_dir, monkeypatch):
        """Returns True for a configured checkpoint repo."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checkpoint_repo: testuser/my-checkpoints\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        assert is_checkpoint_repo("testuser", "my-checkpoints") is True

    def test_no_match(self, temp_dir, monkeypatch):
        """Returns False for a repo that is not a checkpoint destination."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checkpoint_repo: testuser/my-checkpoints\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        assert is_checkpoint_repo("testuser", "my-app") is False

    def test_case_insensitive(self, temp_dir, monkeypatch):
        """Matching is case insensitive."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checkpoint_repo: TestUser/My-Checkpoints\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        assert is_checkpoint_repo("testuser", "my-checkpoints") is True
        assert is_checkpoint_repo("TESTUSER", "MY-CHECKPOINTS") is True

    def test_config_unavailable_returns_false(self, temp_dir, monkeypatch):
        """Returns False when config cannot be loaded (fail-closed)."""
        monkeypatch.setenv("EGG_REPO_CONFIG", str(temp_dir / "nonexistent.yaml"))
        monkeypatch.setenv("HOME", str(temp_dir))

        assert is_checkpoint_repo("testuser", "my-checkpoints") is False

    def test_empty_config_returns_false(self, temp_dir, monkeypatch):
        """Returns False when config has no checkpoint repos."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser\n")
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        assert is_checkpoint_repo("testuser", "my-checkpoints") is False
