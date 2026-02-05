"""
Tests for the docker-setup.py module.

Tests the Docker development environment setup:
- Distribution detection
- Architecture detection
- Run command helpers
- Configuration loading

Note: Most installation functions require root and package managers,
so we focus on testing detection and helper functions.
"""

import os
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load docker-setup module (hyphenated filename)
docker_setup_path = Path(__file__).parent.parent.parent / "sandbox" / "docker-setup.py"
loader = SourceFileLoader("docker_setup", str(docker_setup_path))
docker_setup = loader.load_module()

run = docker_setup.run
run_shell = docker_setup.run_shell
detect_distro = docker_setup.detect_distro
get_arch = docker_setup.get_arch
load_config = docker_setup.load_config
get_extra_packages = docker_setup.get_extra_packages


class TestRun:
    """Tests for the run() helper function."""

    @patch("subprocess.run")
    def test_run_returns_result(self, mock_run, capsys):
        """Test that run returns subprocess result."""
        mock_run.return_value = MagicMock(returncode=0)

        result = run(["echo", "hello"])

        assert result.returncode == 0
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_run_prints_command(self, mock_run, capsys):
        """Test that run prints the command."""
        mock_run.return_value = MagicMock(returncode=0)

        run(["ls", "-la", "/tmp"])
        captured = capsys.readouterr()

        assert "Running:" in captured.out
        assert "ls" in captured.out

    @patch("subprocess.run")
    def test_run_with_check_true(self, mock_run):
        """Test that check=True is passed by default."""
        mock_run.return_value = MagicMock(returncode=0)

        run(["test", "command"])

        mock_run.assert_called_with(["test", "command"], check=True)

    @patch("subprocess.run")
    def test_run_with_check_false(self, mock_run):
        """Test that check=False can be passed."""
        mock_run.return_value = MagicMock(returncode=1)

        run(["test", "command"], check=False)

        mock_run.assert_called_with(["test", "command"], check=False)

    @patch("subprocess.run")
    def test_run_passes_kwargs(self, mock_run):
        """Test that additional kwargs are passed."""
        mock_run.return_value = MagicMock(returncode=0)

        run(["test"], capture_output=True, text=True)

        mock_run.assert_called_with(["test"], check=True, capture_output=True, text=True)


class TestRunShell:
    """Tests for the run_shell() helper function."""

    @patch("subprocess.run")
    def test_run_shell_uses_bash(self, mock_run, capsys):
        """Test that run_shell uses bash."""
        mock_run.return_value = MagicMock(returncode=0)

        run_shell("echo hello")

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["shell"] is True
        assert call_kwargs["executable"] == "/bin/bash"

    @patch("subprocess.run")
    def test_run_shell_prints_command(self, mock_run, capsys):
        """Test that run_shell prints the command."""
        mock_run.return_value = MagicMock(returncode=0)

        run_shell("ls -la")
        captured = capsys.readouterr()

        assert "Running:" in captured.out
        assert "ls" in captured.out


class TestDetectDistro:
    """Tests for Linux distribution detection."""

    def test_detect_distro_returns_string(self):
        """Test that detect_distro returns a string."""
        result = detect_distro()
        assert isinstance(result, str)
        assert result in ["fedora", "ubuntu", "unknown"]


class TestGetArch:
    """Tests for architecture detection."""

    @patch("os.uname")
    def test_get_arch_x86_64(self, mock_uname):
        """Test detecting x86_64 architecture."""
        mock_uname.return_value = MagicMock(machine="x86_64")

        result = get_arch()
        assert result == "x86_64"

    @patch("os.uname")
    def test_get_arch_aarch64(self, mock_uname):
        """Test detecting ARM64 architecture."""
        mock_uname.return_value = MagicMock(machine="aarch64")

        result = get_arch()
        assert result == "aarch64"

    @patch("os.uname")
    def test_get_arch_arm(self, mock_uname):
        """Test detecting ARM architecture."""
        mock_uname.return_value = MagicMock(machine="armv7l")

        result = get_arch()
        assert result == "armv7l"


class TestGetExtraPackages:
    """Tests for extra package configuration."""

    def test_get_extra_packages_empty_config(self):
        """Test with empty config returns empty lists."""
        apt, dnf = get_extra_packages({}, "ubuntu")
        assert apt == []
        assert dnf == []

    def test_get_extra_packages_apt_only(self):
        """Test apt-specific packages."""
        config = {
            "docker_setup": {
                "extra_packages": {
                    "apt": ["nodejs", "python3.11"],
                }
            }
        }
        apt, dnf = get_extra_packages(config, "ubuntu")
        assert apt == ["nodejs", "python3.11"]
        assert dnf == []

    def test_get_extra_packages_dnf_only(self):
        """Test dnf-specific packages."""
        config = {
            "docker_setup": {
                "extra_packages": {
                    "dnf": ["golang", "java-11-openjdk"],
                }
            }
        }
        apt, dnf = get_extra_packages(config, "fedora")
        assert apt == []
        assert dnf == ["golang", "java-11-openjdk"]

    def test_get_extra_packages_generic(self):
        """Test generic packages added to both lists."""
        config = {
            "docker_setup": {
                "extra_packages": {
                    "packages": ["vim", "htop"],
                }
            }
        }
        apt, dnf = get_extra_packages(config, "ubuntu")
        assert apt == ["vim", "htop"]
        assert dnf == ["vim", "htop"]

    def test_get_extra_packages_combined(self):
        """Test combining distro-specific and generic packages."""
        config = {
            "docker_setup": {
                "extra_packages": {
                    "apt": ["nodejs"],
                    "dnf": ["nodejs"],
                    "packages": ["vim"],
                }
            }
        }
        apt, dnf = get_extra_packages(config, "ubuntu")
        assert apt == ["nodejs", "vim"]
        assert dnf == ["nodejs", "vim"]


class TestInstallCorePackages:
    """Tests for core package installation."""

    @patch.object(docker_setup, "run")
    def test_install_core_packages_ubuntu(self, mock_run, capsys):
        """Test core package installation on Ubuntu."""
        mock_run.return_value = MagicMock(returncode=0)

        docker_setup.install_core_packages("ubuntu")

        # Should call apt-get update and install
        calls = mock_run.call_args_list
        assert len(calls) >= 2
        # First call should be apt-get update
        assert "apt-get" in str(calls[0])

    @patch.object(docker_setup, "run")
    def test_install_core_packages_fedora(self, mock_run, capsys):
        """Test core package installation on Fedora."""
        mock_run.return_value = MagicMock(returncode=0)

        docker_setup.install_core_packages("fedora")

        # Should call dnf install
        calls = mock_run.call_args_list
        assert len(calls) >= 1
        assert "dnf" in str(calls[0])


class TestInstallExtraPackages:
    """Tests for extra package installation."""

    @patch.object(docker_setup, "run")
    def test_install_extra_packages_ubuntu(self, mock_run, capsys):
        """Test extra package installation on Ubuntu."""
        mock_run.return_value = MagicMock(returncode=0)

        docker_setup.install_extra_packages("ubuntu", ["nodejs", "golang"], [])

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "apt-get" in call_args
        assert "nodejs" in call_args
        assert "golang" in call_args

    @patch.object(docker_setup, "run")
    def test_install_extra_packages_fedora(self, mock_run, capsys):
        """Test extra package installation on Fedora."""
        mock_run.return_value = MagicMock(returncode=0)

        docker_setup.install_extra_packages("fedora", [], ["nodejs", "golang"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "dnf" in call_args
        assert "nodejs" in call_args
        assert "golang" in call_args

    @patch.object(docker_setup, "run")
    def test_install_extra_packages_empty(self, mock_run):
        """Test that empty package list doesn't call install."""
        docker_setup.install_extra_packages("ubuntu", [], [])
        mock_run.assert_not_called()


class TestConfigureSystem:
    """Tests for system configuration."""

    @patch.object(docker_setup, "run")
    @patch("builtins.open", create=True)
    def test_configure_system_sets_inotify(self, mock_open, mock_run, capsys):
        """Test that system configuration sets inotify watchers."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_file = MagicMock()
        mock_open.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        docker_setup.configure_system("ubuntu")

        captured = capsys.readouterr()
        assert "inotify" in captured.out.lower()


class TestMain:
    """Tests for main entry point."""

    def test_main_requires_root(self, capsys, monkeypatch):
        """Test that main requires root privileges."""
        monkeypatch.setattr(os, "geteuid", lambda: 1000)  # Non-root
        monkeypatch.setattr("sys.argv", ["docker-setup.py"])  # Clean argv for argparse

        with pytest.raises(SystemExit) as excinfo:
            docker_setup.main()

        captured = capsys.readouterr()
        assert "root" in captured.out
        assert excinfo.value.code == 1


class TestGetPipPackages:
    """Tests for pip package configuration."""

    def test_get_pip_packages_empty_config(self):
        """Test with empty config returns empty list."""
        result = docker_setup.get_pip_packages({})
        assert result == []

    def test_get_pip_packages_with_packages(self):
        """Test extracting pip packages from config."""
        config = {
            "docker_setup": {
                "pip": ["django>=4.0", "celery[redis]", "boto3"],
            }
        }
        result = docker_setup.get_pip_packages(config)
        assert result == ["django>=4.0", "celery[redis]", "boto3"]

    def test_get_pip_packages_no_docker_setup(self):
        """Test with config that has no docker_setup key."""
        config = {"writable_repos": ["owner/repo"]}
        result = docker_setup.get_pip_packages(config)
        assert result == []

    def test_get_pip_packages_no_pip_key(self):
        """Test with docker_setup but no pip key."""
        config = {"docker_setup": {"extra_packages": {"apt": ["nodejs"]}}}
        result = docker_setup.get_pip_packages(config)
        assert result == []


class TestGetNpmPackages:
    """Tests for npm package configuration."""

    def test_get_npm_packages_empty_config(self):
        """Test with empty config returns empty list."""
        result = docker_setup.get_npm_packages({})
        assert result == []

    def test_get_npm_packages_with_packages(self):
        """Test extracting npm packages from config."""
        config = {
            "docker_setup": {
                "npm": ["typescript", "jest", "eslint"],
            }
        }
        result = docker_setup.get_npm_packages(config)
        assert result == ["typescript", "jest", "eslint"]

    def test_get_npm_packages_no_docker_setup(self):
        """Test with config that has no docker_setup key."""
        config = {"writable_repos": ["owner/repo"]}
        result = docker_setup.get_npm_packages(config)
        assert result == []


class TestInstallPipPackages:
    """Tests for pip package installation."""

    @patch.object(docker_setup, "run")
    def test_install_pip_packages_empty(self, mock_run):
        """Test that empty package list doesn't call pip."""
        docker_setup.install_pip_packages([])
        mock_run.assert_not_called()

    @patch.object(docker_setup, "run")
    def test_install_pip_packages_calls_pip(self, mock_run, capsys):
        """Test that pip install is called with packages."""
        mock_run.return_value = MagicMock(returncode=0)

        docker_setup.install_pip_packages(["django", "celery"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "pip3" in call_args
        assert "install" in call_args
        assert "django" in call_args
        assert "celery" in call_args

    @patch.object(docker_setup, "run")
    def test_install_pip_packages_handles_failure(self, mock_run, capsys):
        """Test that pip install failure is handled gracefully."""
        mock_run.return_value = MagicMock(returncode=1)

        docker_setup.install_pip_packages(["nonexistent-package"])

        captured = capsys.readouterr()
        assert "WARNING" in captured.out or "failed" in captured.out.lower()


class TestInstallNpmPackages:
    """Tests for npm package installation."""

    @patch.object(docker_setup, "run")
    def test_install_npm_packages_empty(self, mock_run):
        """Test that empty package list doesn't call npm."""
        docker_setup.install_npm_packages([])
        mock_run.assert_not_called()

    @patch.object(docker_setup, "run_shell")
    @patch.object(docker_setup, "run")
    def test_install_npm_packages_no_npm(self, mock_run, mock_run_shell, capsys):
        """Test that missing npm is handled gracefully."""
        mock_run_shell.return_value = MagicMock(returncode=1)  # npm not found

        docker_setup.install_npm_packages(["typescript"])

        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "npm not installed" in captured.out.lower()

    @patch.object(docker_setup, "run_shell")
    @patch.object(docker_setup, "run")
    def test_install_npm_packages_calls_npm(self, mock_run, mock_run_shell, capsys):
        """Test that npm install is called with packages."""
        mock_run_shell.return_value = MagicMock(returncode=0)  # npm found
        mock_run.return_value = MagicMock(returncode=0)

        docker_setup.install_npm_packages(["typescript", "jest"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "npm" in call_args
        assert "install" in call_args
        assert "-g" in call_args
        assert "typescript" in call_args
        assert "jest" in call_args


class TestInstallDependencies:
    """Tests for the install_dependencies function."""

    @patch.object(docker_setup, "install_npm_packages")
    @patch.object(docker_setup, "install_pip_packages")
    def test_install_dependencies_empty_config(self, mock_pip, mock_npm, capsys, tmp_path):
        """Test install_dependencies with empty config file."""
        config_file = tmp_path / "repositories.yaml"
        config_file.write_text("{}\n")

        docker_setup.install_dependencies(str(config_file))

        mock_pip.assert_called_once_with([])
        mock_npm.assert_called_once_with([])

    @patch.object(docker_setup, "install_npm_packages")
    @patch.object(docker_setup, "install_pip_packages")
    def test_install_dependencies_with_packages(self, mock_pip, mock_npm, capsys, tmp_path):
        """Test install_dependencies with pip and npm packages configured."""
        import yaml

        config = {
            "docker_setup": {
                "pip": ["django", "celery"],
                "npm": ["typescript"],
            }
        }
        config_file = tmp_path / "repositories.yaml"
        config_file.write_text(yaml.dump(config))

        docker_setup.install_dependencies(str(config_file))

        mock_pip.assert_called_once_with(["django", "celery"])
        mock_npm.assert_called_once_with(["typescript"])

    @patch.object(docker_setup, "install_npm_packages")
    @patch.object(docker_setup, "install_pip_packages")
    def test_install_dependencies_missing_config(self, mock_pip, mock_npm, capsys):
        """Test install_dependencies with non-existent config file."""
        docker_setup.install_dependencies("/nonexistent/config.yaml")

        # Should still call with empty lists (graceful fallback)
        mock_pip.assert_called_once_with([])
        mock_npm.assert_called_once_with([])
