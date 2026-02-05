"""Tests for shared egg_config config module."""

from pathlib import Path

from egg_config.config import Config, get_local_repos, get_repos_config_file


class TestConfig:
    """Tests for Config class."""

    def test_user_config_dir_exists(self):
        """USER_CONFIG_DIR is a Path."""
        assert isinstance(Config.USER_CONFIG_DIR, Path)

    def test_repos_config_file(self):
        """REPOS_CONFIG_FILE is under USER_CONFIG_DIR."""
        assert str(Config.REPOS_CONFIG_FILE).endswith("repositories.yaml")


class TestGetReposConfigFile:
    """Tests for get_repos_config_file function."""

    def test_returns_path(self):
        """Returns a Path object."""
        result = get_repos_config_file()
        assert isinstance(result, Path)
        assert result == Config.REPOS_CONFIG_FILE


class TestGetLocalRepos:
    """Tests for get_local_repos function."""

    def test_nonexistent_config(self, tmp_path):
        """Return empty for nonexistent config file."""
        result = get_local_repos(tmp_path / "nonexistent.yaml")
        assert result == []

    def test_empty_config(self, tmp_path):
        """Return empty for empty YAML file."""
        config_file = tmp_path / "repos.yaml"
        config_file.write_text("")
        result = get_local_repos(config_file)
        assert result == []

    def test_no_local_repos_key(self, tmp_path):
        """Return empty when no local_repos key."""
        config_file = tmp_path / "repos.yaml"
        config_file.write_text("github_username: test\n")
        result = get_local_repos(config_file)
        assert result == []

    def test_valid_config(self, tmp_path):
        """Parse valid repos config with existing paths."""
        # Create actual directories
        repo1 = tmp_path / "repo1"
        repo1.mkdir()
        repo2 = tmp_path / "repo2"
        repo2.mkdir()

        config_file = tmp_path / "repos.yaml"
        config_file.write_text(f"local_repos:\n  paths:\n    - {repo1}\n    - {repo2}\n")
        result = get_local_repos(config_file)
        assert len(result) == 2

    def test_filters_nonexistent_paths(self, tmp_path):
        """Filter out paths that don't exist."""
        repo1 = tmp_path / "existing"
        repo1.mkdir()

        config_file = tmp_path / "repos.yaml"
        config_file.write_text(f"local_repos:\n  paths:\n    - {repo1}\n    - /nonexistent/path\n")
        result = get_local_repos(config_file)
        assert len(result) == 1

    def test_invalid_yaml(self, tmp_path):
        """Return empty for invalid YAML."""
        config_file = tmp_path / "repos.yaml"
        config_file.write_text("{{invalid yaml")
        result = get_local_repos(config_file)
        assert result == []

    def test_local_repos_not_dict(self, tmp_path):
        """Handle local_repos not being a dict."""
        config_file = tmp_path / "repos.yaml"
        config_file.write_text("local_repos: not_a_dict\n")
        result = get_local_repos(config_file)
        assert result == []
