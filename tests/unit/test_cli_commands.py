"""Unit tests for CLI command handlers and utilities."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from cli.commands.config import get_config_info, validate_config_files
from cli.commands.docker import (
    ContainerStatus,
    check_docker_installed,
    check_docker_running,
    get_container_status,
    list_egg_containers,
)
from cli.commands.handlers import (
    handle_config,
    handle_config_validate,
    handle_exec,
    handle_start,
    handle_status,
)
from cli.main import main


class TestDockerUtilities:
    """Tests for Docker utility functions."""

    def test_container_status_dataclass(self):
        """Test ContainerStatus dataclass."""
        status = ContainerStatus(
            name="egg-gateway",
            running=True,
            status="Up 5 minutes",
            health="healthy",
            id="abc123",
        )
        assert status.name == "egg-gateway"
        assert status.running is True
        assert status.status == "Up 5 minutes"
        assert status.health == "healthy"
        assert status.id == "abc123"

    def test_container_status_optional_fields(self):
        """Test ContainerStatus with optional fields."""
        status = ContainerStatus(
            name="test",
            running=False,
            status="Exited (0)",
        )
        assert status.health is None
        assert status.id is None

    @patch("shutil.which")
    def test_check_docker_installed_true(self, mock_which):
        """Test docker installed check returns True when present."""
        mock_which.return_value = "/usr/bin/docker"
        assert check_docker_installed() is True
        mock_which.assert_called_once_with("docker")

    @patch("shutil.which")
    def test_check_docker_installed_false(self, mock_which):
        """Test docker installed check returns False when missing."""
        mock_which.return_value = None
        assert check_docker_installed() is False

    @patch("subprocess.run")
    def test_check_docker_running_true(self, mock_run):
        """Test docker daemon running check returns True."""
        mock_run.return_value = MagicMock(returncode=0)
        assert check_docker_running() is True

    @patch("subprocess.run")
    def test_check_docker_running_false(self, mock_run):
        """Test docker daemon check returns False when not running."""
        mock_run.return_value = MagicMock(returncode=1)
        assert check_docker_running() is False

    @patch("subprocess.run")
    def test_check_docker_running_timeout(self, mock_run):
        """Test docker daemon check handles timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("docker", 10)
        assert check_docker_running() is False

    @patch("subprocess.run")
    def test_get_container_status_running(self, mock_run):
        """Test getting status of running container."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123def456\trunning\thealthy",
        )
        status = get_container_status("egg-gateway")
        assert status is not None
        assert status.name == "egg-gateway"
        assert status.running is True
        assert status.status == "running"
        assert status.health == "healthy"

    @patch("subprocess.run")
    def test_get_container_status_not_found(self, mock_run):
        """Test getting status of non-existent container."""
        mock_run.return_value = MagicMock(returncode=1)
        status = get_container_status("nonexistent")
        assert status is None

    @patch("subprocess.run")
    def test_list_egg_containers_empty(self, mock_run):
        """Test listing containers when none exist."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        containers = list_egg_containers()
        assert containers == []

    @patch("subprocess.run")
    @patch("cli.commands.docker.get_container_status")
    def test_list_egg_containers_with_results(self, mock_get_status, mock_run):
        """Test listing containers with results."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="egg-gateway\tUp 5 minutes\tabc123\negg-sandbox\tExited (0)\tdef456",
        )
        mock_get_status.return_value = ContainerStatus(
            name="test", running=True, status="Up", health=None
        )
        containers = list_egg_containers()
        assert len(containers) == 2
        assert containers[0].name == "egg-gateway"
        assert containers[0].running is True


class TestConfigCommands:
    """Tests for configuration commands."""

    def test_validate_config_files_no_config(self, tmp_path, monkeypatch):
        """Test validation when no config file exists."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_CONFIG", raising=False)

        result = validate_config_files()
        assert not result.valid
        assert any("egg.yaml" in e.lower() for e in result.errors)

    def test_validate_config_files_with_valid_config(self, tmp_path, monkeypatch):
        """Test validation with valid config."""
        config_file = tmp_path / "egg.yaml"
        config_file.write_text(
            """
egg:
  name: test
  git:
    branch_prefix: "egg/"
    protected_branches:
      - main
  repositories:
    allowed:
      - owner/repo
"""
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_CONFIG", raising=False)

        result = validate_config_files(config_path=str(config_file))
        assert result.valid

    def test_validate_config_files_invalid_path(self):
        """Test validation with non-existent config path."""
        result = validate_config_files(config_path="/nonexistent/path/egg.yaml")
        assert not result.valid
        assert any("not found" in e.lower() for e in result.errors)

    def test_get_config_info_no_config(self, tmp_path, monkeypatch):
        """Test get_config_info when no config exists."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_CONFIG", raising=False)
        monkeypatch.delenv("EGG_SECRETS", raising=False)

        info = get_config_info()
        assert info["config_path"] == "not found"
        assert info["config_exists"] == "no"

    def test_get_config_info_with_config(self, tmp_path, monkeypatch):
        """Test get_config_info with existing config."""
        config_file = tmp_path / "egg.yaml"
        config_file.write_text("egg:\n  name: test\n")

        info = get_config_info(config_path=str(config_file))
        assert info["config_path"] == str(config_file)
        assert info["config_exists"] == "yes"


class TestCommandHandlers:
    """Tests for CLI command handlers."""

    @patch("cli.commands.handlers._check_docker_prerequisites")
    @patch("cli.commands.handlers.validate_config_files")
    def test_handle_start_no_docker(self, mock_validate, mock_docker):
        """Test start command when Docker not available."""
        mock_docker.return_value = 1

        args = MagicMock()
        args.config = None
        args.private = False
        args.prompt = None

        result = handle_start(args)
        assert result == 1
        mock_validate.assert_not_called()

    @patch("cli.commands.handlers._check_docker_prerequisites")
    @patch("cli.commands.handlers.list_egg_containers")
    def test_handle_status_no_containers(self, mock_list, mock_docker, capsys):
        """Test status command with no containers."""
        mock_docker.return_value = 0
        mock_list.return_value = []

        args = MagicMock()
        result = handle_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "No egg containers found" in captured.out

    @patch("cli.commands.handlers._check_docker_prerequisites")
    @patch("cli.commands.handlers.list_egg_containers")
    def test_handle_status_with_containers(self, mock_list, mock_docker, capsys):
        """Test status command with running containers."""
        mock_docker.return_value = 0
        mock_list.return_value = [
            ContainerStatus(
                name="egg-gateway",
                running=True,
                status="Up 5 minutes",
                health="healthy",
            ),
            ContainerStatus(
                name="egg-sandbox",
                running=False,
                status="Exited (0)",
            ),
        ]

        args = MagicMock()
        result = handle_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "egg-gateway" in captured.out
        assert "egg-sandbox" in captured.out
        assert "1/2 containers running" in captured.out

    @patch("cli.commands.handlers._check_docker_prerequisites")
    def test_handle_exec_no_command(self, mock_docker, capsys):
        """Test exec command with no command specified."""
        mock_docker.return_value = 0

        args = MagicMock()
        args.cmd = []

        result = handle_exec(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "No command specified" in captured.err

    @patch("cli.commands.handlers._check_docker_prerequisites")
    @patch("cli.commands.handlers.get_container_status")
    def test_handle_exec_sandbox_not_running(self, mock_status, mock_docker, capsys):
        """Test exec command when sandbox not running."""
        mock_docker.return_value = 0
        mock_status.return_value = ContainerStatus(
            name="egg-sandbox",
            running=False,
            status="Exited",
        )

        args = MagicMock()
        args.cmd = ["ls"]

        result = handle_exec(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "not running" in captured.err

    def test_handle_config_validate(self, tmp_path, monkeypatch, capsys):
        """Test config validate command."""
        config_file = tmp_path / "egg.yaml"
        config_file.write_text(
            """
egg:
  name: test
  git:
    branch_prefix: "egg/"
    protected_branches:
      - main
  repositories:
    allowed:
      - owner/repo
"""
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_CONFIG", raising=False)
        monkeypatch.setenv("EGG_CONFIG", str(config_file))

        args = MagicMock()
        result = handle_config_validate(args)

        captured = capsys.readouterr()
        assert result == 0
        assert "valid" in captured.out.lower()

    def test_handle_config_no_subcommand(self, capsys):
        """Test config command with no subcommand."""
        args = MagicMock()
        args.config_command = None

        result = handle_config(args)
        assert result == 0
        captured = capsys.readouterr()
        assert "validate" in captured.out


class TestCLIMain:
    """Tests for the main CLI entry point."""

    def test_main_no_args_shows_help(self, capsys):
        """Test that running without args shows help."""
        with patch.object(sys, "argv", ["egg"]):
            result = main()
        assert result == 0
        captured = capsys.readouterr()
        assert "usage:" in captured.out.lower()

    def test_main_help_flag(self, capsys):
        """Test --help flag."""
        with patch.object(sys, "argv", ["egg", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0

    @patch("cli.commands.handlers._check_docker_prerequisites")
    @patch("cli.commands.handlers.list_egg_containers")
    def test_main_status_command(self, mock_list, mock_docker, capsys):
        """Test status command through main."""
        mock_docker.return_value = 0
        mock_list.return_value = []

        with patch.object(sys, "argv", ["egg", "status"]):
            result = main()

        assert result == 0

    def test_main_unknown_command(self, capsys):
        """Test handling of unknown command."""
        with patch.object(sys, "argv", ["egg", "unknown"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        # argparse exits with code 2 for invalid arguments
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err


class TestDockerPrerequisites:
    """Tests for Docker prerequisite checking."""

    @patch("cli.commands.handlers.check_docker_installed")
    def test_prereqs_docker_not_installed(self, mock_installed, capsys):
        """Test error when Docker not installed."""
        mock_installed.return_value = False

        from cli.commands.handlers import _check_docker_prerequisites

        result = _check_docker_prerequisites()
        assert result == 1
        captured = capsys.readouterr()
        assert "not installed" in captured.err.lower()

    @patch("cli.commands.handlers.check_docker_installed")
    @patch("cli.commands.handlers.check_docker_running")
    def test_prereqs_docker_not_running(self, mock_running, mock_installed, capsys):
        """Test error when Docker daemon not running."""
        mock_installed.return_value = True
        mock_running.return_value = False

        from cli.commands.handlers import _check_docker_prerequisites

        result = _check_docker_prerequisites()
        assert result == 1
        captured = capsys.readouterr()
        assert "not running" in captured.err.lower()

    @patch("cli.commands.handlers.check_docker_installed")
    @patch("cli.commands.handlers.check_docker_running")
    def test_prereqs_docker_ok(self, mock_running, mock_installed):
        """Test success when Docker is available."""
        mock_installed.return_value = True
        mock_running.return_value = True

        from cli.commands.handlers import _check_docker_prerequisites

        result = _check_docker_prerequisites()
        assert result == 0
