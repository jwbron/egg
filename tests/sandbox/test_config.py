"""Tests for sandbox egg_lib config module."""

from pathlib import Path

from egg_lib.config import Colors, Config


class TestColors:
    """Tests for Colors class."""

    def test_has_color_codes(self):
        """Colors class has ANSI color codes."""
        assert Colors.GREEN != ""
        assert Colors.RED != ""
        assert Colors.BLUE != ""
        assert Colors.YELLOW != ""
        assert Colors.NC != ""  # No Color / reset

    def test_nc_resets(self):
        """NC (No Color) is the ANSI reset sequence."""
        assert Colors.NC == "\033[0m"


class TestConfig:
    """Tests for Config class."""

    def test_user_config_dir(self):
        """USER_CONFIG_DIR is a Path in home directory."""
        assert isinstance(Config.USER_CONFIG_DIR, Path)
        assert ".config" in str(Config.USER_CONFIG_DIR)
        assert "egg" in str(Config.USER_CONFIG_DIR)

    def test_config_dir(self):
        """CONFIG_DIR is a Path."""
        assert isinstance(Config.CONFIG_DIR, Path)
