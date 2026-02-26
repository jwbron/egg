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
get_build_commands = docker_setup.get_build_commands
run_build_commands = docker_setup.run_build_commands


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


class TestGetBuildCommands:
    """Tests for build_commands extraction from config."""

    def test_returns_build_commands(self):
        """Test extracting build_commands from repo_settings."""
        config = {
            "repo_settings": {
                "org/web-app": {
                    "build_commands": {
                        "watch_files": ["package-lock.json"],
                        "commands": ["npm ci"],
                    }
                },
                "org/python-svc": {
                    "build_commands": {
                        "watch_files": ["requirements.txt"],
                        "commands": ["pip install -r requirements.txt"],
                    }
                },
            }
        }
        result = get_build_commands(config)
        assert len(result) == 2
        repos = [r["repo"] for r in result]
        assert "org/web-app" in repos
        assert "org/python-svc" in repos

    def test_returns_empty_for_no_build_commands(self):
        """Test empty list when no build_commands configured."""
        config = {
            "repo_settings": {"org/app": {"checks": [{"name": "test", "command": "make test"}]}}
        }
        result = get_build_commands(config)
        assert result == []

    def test_returns_empty_for_empty_config(self):
        """Test empty list with empty config."""
        result = get_build_commands({})
        assert result == []

    def test_skips_repos_with_empty_commands(self):
        """Test that repos with empty commands are skipped."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["package.json"],
                        "commands": [],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert result == []

    def test_handles_missing_watch_files(self):
        """Test build_commands without watch_files key."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "commands": ["make deps"],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert len(result) == 1
        assert result[0]["watch_files"] == []
        assert result[0]["commands"] == ["make deps"]

    def test_handles_invalid_types(self):
        """Test graceful handling of invalid config types."""
        config = {"repo_settings": {"org/app": {"build_commands": "not-a-dict"}}}
        result = get_build_commands(config)
        assert result == []


class TestRunBuildCommands:
    """Tests for run_build_commands function."""

    @patch("subprocess.run")
    def test_runs_commands(self, mock_run, tmp_path, capsys):
        """Test that build commands are executed."""
        mock_run.return_value = MagicMock(returncode=0)

        # Create the work directory that run_build_commands expects
        work_dir = Path("/tmp/repo-deps/org--app")
        work_dir.mkdir(parents=True, exist_ok=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": ["package.json"],
                "commands": ["npm ci", "npm run build"],
            }
        ]

        try:
            run_build_commands(build_commands)
        finally:
            # Clean up
            import shutil

            shutil.rmtree("/tmp/repo-deps", ignore_errors=True)

        # Should have called subprocess.run twice (one per command)
        assert mock_run.call_count == 2
        captured = capsys.readouterr()
        assert "Build commands" in captured.out
        assert "org/app" in captured.out

    @patch("subprocess.run")
    def test_empty_commands_is_noop(self, mock_run, capsys):
        """Test that empty build_commands list does nothing."""
        run_build_commands([])
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out == ""

    @patch("subprocess.run")
    def test_handles_command_failure(self, mock_run, tmp_path, capsys):
        """Test that failed commands produce warnings but don't crash."""
        mock_run.return_value = MagicMock(returncode=1)

        work_dir = tmp_path / "repo-deps" / "org--app"
        work_dir.mkdir(parents=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["false"],
            }
        ]

        # Just run it - should not raise
        run_build_commands(build_commands)

        captured = capsys.readouterr()
        assert "Build commands" in captured.out


class TestGetBuildCommandsEdgeCases:
    """Edge case tests for get_build_commands."""

    def test_non_dict_repo_settings_returns_empty(self):
        """Non-dict repo_settings returns empty list."""
        config = {"repo_settings": "not-a-dict"}
        result = get_build_commands(config)
        assert result == []

    def test_skips_non_dict_settings_entries(self):
        """Non-dict individual repo settings are skipped."""
        config = {
            "repo_settings": {
                "org/broken": "not-a-dict",
                "org/valid": {
                    "build_commands": {
                        "commands": ["make deps"],
                    }
                },
            }
        }
        result = get_build_commands(config)
        assert len(result) == 1
        assert result[0]["repo"] == "org/valid"

    def test_non_list_watch_files_returns_empty(self):
        """Non-list watch_files defaults to empty list."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": "just-a-string",
                        "commands": ["make deps"],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert len(result) == 1
        assert result[0]["watch_files"] == []
        assert result[0]["commands"] == ["make deps"]

    def test_non_list_commands_returns_empty(self):
        """Non-list commands causes the repo to be skipped."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["req.txt"],
                        "commands": "single-command",
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert result == []


class TestRunBuildCommandsEdgeCases:
    """Edge case tests for run_build_commands."""

    @patch("subprocess.run")
    def test_fallback_to_tmp_when_work_dir_missing(self, mock_run, capsys):
        """When repo work_dir doesn't exist, falls back to /tmp."""
        mock_run.return_value = MagicMock(returncode=0)

        build_commands = [
            {
                "repo": "org/nonexistent-repo-xyz-12345",
                "watch_files": [],
                "commands": ["echo hello"],
            }
        ]

        run_build_commands(build_commands)

        # Verify it ran with /tmp as cwd
        call_args = mock_run.call_args
        assert call_args.kwargs["cwd"] == "/tmp"
        captured = capsys.readouterr()
        assert "does not exist" in captured.out

    @patch("subprocess.run")
    def test_subprocess_exception_does_not_crash(self, mock_run, capsys):
        """Subprocess raising an exception is caught and warned about."""
        mock_run.side_effect = OSError("command not found")

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["nonexistent-command"],
            }
        ]

        # Should not raise
        run_build_commands(build_commands)

        captured = capsys.readouterr()
        assert "Command failed" in captured.out
        assert "command not found" in captured.out

    @patch("subprocess.run")
    def test_warning_message_includes_exit_code(self, mock_run, tmp_path, capsys):
        """Failed commands report the exit code in the warning."""
        mock_run.return_value = MagicMock(returncode=127)

        work_dir = Path("/tmp/repo-deps/org--app")
        work_dir.mkdir(parents=True, exist_ok=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["bad-command"],
            }
        ]

        try:
            run_build_commands(build_commands)
        finally:
            import shutil

            shutil.rmtree("/tmp/repo-deps", ignore_errors=True)

        captured = capsys.readouterr()
        assert "127" in captured.out

    @patch("subprocess.run")
    def test_multiple_repos_run_sequentially(self, mock_run, capsys):
        """Multiple repos' commands all execute."""
        mock_run.return_value = MagicMock(returncode=0)

        build_commands = [
            {
                "repo": "org/app-a",
                "watch_files": [],
                "commands": ["cmd-a1", "cmd-a2"],
            },
            {
                "repo": "org/app-b",
                "watch_files": [],
                "commands": ["cmd-b1"],
            },
        ]

        run_build_commands(build_commands)

        assert mock_run.call_count == 3
        captured = capsys.readouterr()
        assert "org/app-a" in captured.out
        assert "org/app-b" in captured.out


class TestMain:
    """Tests for main entry point."""

    def test_main_requires_root(self, capsys, monkeypatch):
        """Test that main requires root privileges."""
        monkeypatch.setattr(os, "geteuid", lambda: 1000)  # Non-root

        with pytest.raises(SystemExit) as excinfo:
            docker_setup.main()

        captured = capsys.readouterr()
        assert "root" in captured.out
        assert excinfo.value.code == 1
