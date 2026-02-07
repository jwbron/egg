"""Tests for sandbox/egg_lib/config.py - Configuration and platform detection."""

import sys
from pathlib import Path
from unittest.mock import patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.config import Colors, Config, get_local_repos, get_platform


class TestColors:
    """Tests for Colors class."""

    def test_has_color_codes(self):
        """Colors class has expected ANSI color codes."""
        assert "\033[" in Colors.BLUE
        assert "\033[" in Colors.GREEN
        assert "\033[" in Colors.YELLOW
        assert "\033[" in Colors.RED
        assert "\033[" in Colors.BOLD
        assert Colors.NC == "\033[0m"


class TestConfig:
    """Tests for Config class."""

    def test_config_dir_exists(self):
        """Config has expected path attributes."""
        assert Config.IMAGE_NAME == "egg"
        assert Config.CONTAINER_NAME == "egg"
        assert isinstance(Config.CONFIG_DIR, Path)
        assert isinstance(Config.DOCKERFILE, Path)
        assert isinstance(Config.USER_CONFIG_DIR, Path)
        assert isinstance(Config.REPOS_CONFIG_FILE, Path)

    def test_dangerous_dirs(self):
        """DANGEROUS_DIRS contains expected credential directories."""
        dir_names = [d.name for d in Config.DANGEROUS_DIRS]
        assert ".ssh" in dir_names
        assert ".aws" in dir_names
        assert ".gnupg" in dir_names

    def test_cache_dir_alias(self):
        """CONFIG_DIR is an alias for CACHE_DIR."""
        assert Config.CONFIG_DIR == Config.CACHE_DIR


class TestGetPlatform:
    """Tests for get_platform()."""

    @patch("platform.system", return_value="Linux")
    def test_linux(self, mock_system):
        """Returns 'linux' on Linux."""
        assert get_platform() == "linux"

    @patch("platform.system", return_value="Darwin")
    def test_macos(self, mock_system):
        """Returns 'macos' on macOS."""
        assert get_platform() == "macos"

    @patch("platform.system", return_value="Windows")
    def test_unknown(self, mock_system):
        """Returns 'unknown' for unrecognized platforms."""
        assert get_platform() == "unknown"


class TestGetLocalRepos:
    """Tests for get_local_repos()."""

    def test_missing_config_file(self, tmp_path):
        """Returns empty list when config file doesn't exist."""
        result = get_local_repos(config_file=tmp_path / "nonexistent.yaml")
        assert result == []

    def test_valid_config_with_paths(self, tmp_path):
        """Parses YAML config and returns existing paths."""
        # Create a directory that exists
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()

        config_file = tmp_path / "repositories.yaml"
        config_file.write_text(
            f"local_repos:\n  paths:\n    - {repo_dir}\n    - /nonexistent/path\n"
        )

        result = get_local_repos(config_file=config_file)
        assert repo_dir.resolve() in result
        # /nonexistent/path should be excluded
        assert len(result) == 1

    def test_empty_config(self, tmp_path):
        """Returns empty list for empty YAML file."""
        config_file = tmp_path / "repositories.yaml"
        config_file.write_text("")

        result = get_local_repos(config_file=config_file)
        assert result == []

    def test_config_without_local_repos_key(self, tmp_path):
        """Returns empty list when local_repos key is missing."""
        config_file = tmp_path / "repositories.yaml"
        config_file.write_text("other_key: value\n")

        result = get_local_repos(config_file=config_file)
        assert result == []
