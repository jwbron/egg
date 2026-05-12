"""Tests for sandbox/egg_lib/setup_flow.py - host-side helpers."""

import sys
from pathlib import Path
from unittest.mock import patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.setup_flow import (
    add_standard_mounts,
    check_host_setup,
)


class TestCheckHostSetup:
    """Tests for check_host_setup."""

    def test_creates_config_dir(self, tmp_path):
        """Creates config directory if it doesn't exist."""
        config_dir = tmp_path / "egg-config"
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            result = check_host_setup()
            assert result is True
            assert config_dir.exists()

    def test_warns_missing_repos_config(self, tmp_path, capsys):
        """Warns when repositories.yaml is missing."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = check_host_setup()
            assert result is True

    def test_no_warn_when_config_exists(self, tmp_path):
        """Does not warn when repositories.yaml exists."""
        (tmp_path / "repositories.yaml").write_text("github_username: test\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = check_host_setup()
            assert result is True


class TestAddStandardMounts:
    """Tests for add_standard_mounts."""

    def test_adds_certs_volume_mount(self, monkeypatch):
        """Always mounts the egg-certs Docker named volume."""
        monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
        mount_args = []
        add_standard_mounts(mount_args)
        assert "-v" in mount_args
        assert any("egg-certs:/shared/certs:ro" in a for a in mount_args)

    def test_respects_compose_project_name(self, monkeypatch):
        """Uses COMPOSE_PROJECT_NAME env var for volume name."""
        monkeypatch.setenv("COMPOSE_PROJECT_NAME", "myproject")
        mount_args = []
        add_standard_mounts(mount_args)
        assert any("myproject-certs:/shared/certs:ro" in a for a in mount_args)

    def test_quiet_mode(self, monkeypatch, capsys):
        """Quiet mode suppresses output."""
        monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
        mount_args = []
        add_standard_mounts(mount_args, quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""
