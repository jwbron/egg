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
load_build_commands_manifest = docker_setup.load_build_commands_manifest
load_extra_packages_manifest = docker_setup.load_extra_packages_manifest
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

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": ["package.json"],
                "commands": ["npm ci", "npm run build"],
            }
        ]

        run_build_commands(build_commands, repo_deps_base=repo_deps)

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
    def test_strips_pip_ignore_installed_from_env(self, mock_run, tmp_path, monkeypatch):
        """PIP_IGNORE_INSTALLED must not leak into build commands.

        The sandbox image sets it for system-Python installs; inherited by a
        repo's `pip install` it reinstalls pip itself, breaking pinned tooling.
        """
        mock_run.return_value = MagicMock(returncode=0)
        monkeypatch.setenv("PIP_IGNORE_INSTALLED", "1")

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["pip install -r requirements.txt"],
            }
        ]

        run_build_commands(build_commands, repo_deps_base=repo_deps)

        env = mock_run.call_args.kwargs["env"]
        assert "PIP_IGNORE_INSTALLED" not in env

    @patch("subprocess.run")
    def test_command_failure_aborts_build(self, mock_run, tmp_path, capsys):
        """A failed build command aborts the image build (was warn-only). See #2087."""
        mock_run.return_value = MagicMock(returncode=1)

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["false"],
            }
        ]

        with pytest.raises(RuntimeError, match="exited with code 1"):
            run_build_commands(build_commands, repo_deps_base=repo_deps)


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
    def test_missing_work_dir_raises(self, mock_run, tmp_path):
        """When repo work_dir doesn't exist, build aborts (vs. silent /tmp fallback).

        The earlier behavior silently fell back to running commands in /tmp,
        which masked real misconfiguration. See #2087.
        """
        mock_run.return_value = MagicMock(returncode=0)

        repo_deps = tmp_path / "repo-deps"
        repo_deps.mkdir()

        build_commands = [
            {
                "repo": "org/nonexistent-repo-xyz-12345",
                "watch_files": [],
                "commands": ["echo hello"],
            }
        ]

        with pytest.raises(RuntimeError, match="watch files directory.*does not exist"):
            run_build_commands(build_commands, repo_deps_base=repo_deps)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_subprocess_exception_raises(self, mock_run, tmp_path):
        """Subprocess raising an exception aborts the build.

        The earlier behavior caught and warned, masking misconfigurations
        like "tool not on PATH". See #2087.
        """
        mock_run.side_effect = OSError("command not found")

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["nonexistent-command"],
            }
        ]

        with pytest.raises(RuntimeError, match="raised an exception"):
            run_build_commands(build_commands, repo_deps_base=repo_deps)

    @patch("subprocess.run")
    def test_nonzero_exit_raises_with_exit_code(self, mock_run, tmp_path):
        """Failed commands abort the build and report the exit code.

        The earlier behavior printed a warning and continued, allowing the
        image to be built without any of the dependencies the commands were
        supposed to install. See #2087.
        """
        mock_run.return_value = MagicMock(returncode=127)

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        build_commands = [
            {
                "repo": "org/app",
                "watch_files": [],
                "commands": ["bad-command"],
            }
        ]

        with pytest.raises(RuntimeError, match="exited with code 127"):
            run_build_commands(build_commands, repo_deps_base=repo_deps)

    @patch("subprocess.run")
    def test_multiple_repos_run_sequentially(self, mock_run, tmp_path, capsys):
        """Multiple repos' commands all execute."""
        mock_run.return_value = MagicMock(returncode=0)

        repo_deps = tmp_path / "repo-deps"
        for repo_dir in ("org--app-a", "org--app-b"):
            (repo_deps / repo_dir).mkdir(parents=True)

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

        run_build_commands(build_commands, repo_deps_base=repo_deps)

        assert mock_run.call_count == 3
        captured = capsys.readouterr()
        assert "org/app-a" in captured.out
        assert "org/app-b" in captured.out


class TestLoadBuildCommandsManifest:
    """Tests for loading build commands from manifest.json."""

    def test_loads_valid_manifest(self, tmp_path):
        """Test loading a valid manifest.json."""
        import json

        manifest = [
            {
                "repo": "org/web-app",
                "watch_files": ["package-lock.json"],
                "commands": ["npm ci"],
            },
            {
                "repo": "org/python-svc",
                "watch_files": ["requirements.txt"],
                "commands": ["pip install -r requirements.txt"],
            },
        ]
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert len(result) == 2
        repos = [r["repo"] for r in result]
        assert "org/web-app" in repos
        assert "org/python-svc" in repos

    def test_returns_empty_for_missing_file(self, tmp_path):
        """Test empty list when manifest file doesn't exist."""
        result = load_build_commands_manifest(str(tmp_path / "nonexistent.json"))
        assert result == []

    def test_returns_empty_for_invalid_json(self, tmp_path):
        """Test empty list when manifest contains invalid JSON."""
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text("not valid json {{{")
        result = load_build_commands_manifest(str(manifest_file))
        assert result == []

    def test_returns_empty_for_non_list_non_dict(self, tmp_path):
        """Test empty list when manifest root is not an array or dict."""
        import json

        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps("just a string"))
        result = load_build_commands_manifest(str(manifest_file))
        assert result == []

    def test_loads_new_dict_format(self, tmp_path):
        """Test loading build_commands from new dict-format manifest."""
        import json

        manifest = {
            "extra_packages": {"apt": ["golang-go"], "dnf": ["golang"]},
            "build_commands": [
                {
                    "repo": "org/app",
                    "watch_files": ["go.sum"],
                    "commands": ["go mod download"],
                }
            ],
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert len(result) == 1
        assert result[0]["repo"] == "org/app"
        assert result[0]["commands"] == ["go mod download"]

    def test_new_dict_format_empty_build_commands(self, tmp_path):
        """Test new format with only extra_packages (no build_commands) returns empty list."""
        import json

        manifest = {
            "extra_packages": {"apt": ["golang-go"], "dnf": []},
            "build_commands": [],
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert result == []

    def test_skips_entries_without_commands(self, tmp_path):
        """Test that entries without commands are skipped."""
        import json

        manifest = [
            {"repo": "org/app", "watch_files": ["f.txt"]},  # no commands
            {"repo": "org/other", "commands": ["make"], "watch_files": []},
        ]
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert len(result) == 1
        assert result[0]["repo"] == "org/other"

    def test_skips_entries_with_empty_commands(self, tmp_path):
        """Test that entries with empty commands list are skipped."""
        import json

        manifest = [{"repo": "org/app", "commands": [], "watch_files": []}]
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert result == []

    def test_skips_non_dict_entries(self, tmp_path):
        """Test that non-dict entries in the list are skipped."""
        import json

        manifest = ["not-a-dict", {"repo": "org/app", "commands": ["make"], "watch_files": []}]
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert len(result) == 1
        assert result[0]["repo"] == "org/app"


class TestLoadExtraPackagesManifest:
    """Tests for loading extra_packages from manifest.json."""

    def test_loads_apt_and_dnf(self, tmp_path):
        """Test loading apt and dnf packages from new dict-format manifest."""
        import json

        manifest = {
            "extra_packages": {"apt": ["golang-go", "nodejs"], "dnf": ["golang", "nodejs"]},
            "build_commands": [],
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        apt, dnf = load_extra_packages_manifest(str(manifest_file))
        assert apt == ["golang-go", "nodejs"]
        assert dnf == ["golang", "nodejs"]

    def test_returns_empty_for_missing_file(self, tmp_path):
        """Test empty lists when manifest file doesn't exist."""
        apt, dnf = load_extra_packages_manifest(str(tmp_path / "nonexistent.json"))
        assert apt == []
        assert dnf == []

    def test_returns_empty_for_old_list_format(self, tmp_path):
        """Test empty lists when manifest uses old list format (no extra_packages key)."""
        import json

        manifest = [{"repo": "org/app", "commands": ["make"], "watch_files": []}]
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        apt, dnf = load_extra_packages_manifest(str(manifest_file))
        assert apt == []
        assert dnf == []

    def test_returns_empty_for_missing_extra_packages_key(self, tmp_path):
        """Test empty lists when dict manifest has no extra_packages."""
        import json

        manifest = {"build_commands": []}
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        apt, dnf = load_extra_packages_manifest(str(manifest_file))
        assert apt == []
        assert dnf == []

    def test_returns_empty_for_invalid_json(self, tmp_path):
        """Test empty lists on invalid JSON."""
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text("not valid json {{{")
        apt, dnf = load_extra_packages_manifest(str(manifest_file))
        assert apt == []
        assert dnf == []


class TestPersistDirs:
    """Tests for persist_dirs in build_commands."""

    def test_get_build_commands_includes_persist_dirs(self):
        """persist_dirs is extracted from config."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["package.json"],
                        "commands": ["npm ci"],
                        "persist_dirs": ["node_modules", "dist"],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert len(result) == 1
        assert result[0]["persist_dirs"] == ["node_modules", "dist"]

    def test_get_build_commands_defaults_persist_dirs_to_empty(self):
        """Missing persist_dirs defaults to empty list."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "commands": ["npm ci"],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert result[0]["persist_dirs"] == []

    def test_get_build_commands_handles_non_list_persist_dirs(self):
        """Non-list persist_dirs defaults to empty list."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "commands": ["npm ci"],
                        "persist_dirs": "not-a-list",
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert result[0]["persist_dirs"] == []

    def test_persist_dirs_copies_to_prebuilt(self, tmp_path, capsys):
        """persist_dirs copies directories to prebuilt destination."""
        from docker_setup import persist_build_dirs

        # Create the work directory with node_modules
        repo_deps = tmp_path / "repo-deps"
        work_dir = repo_deps / "org--app"
        nm_dir = work_dir / "node_modules"
        nm_dir.mkdir(parents=True)
        (nm_dir / "pkg.json").write_text('{"name": "test"}')

        prebuilt = tmp_path / "prebuilt-deps"

        persist_build_dirs(
            [
                {
                    "repo": "org/app",
                    "commands": ["npm ci"],
                    "persist_dirs": ["node_modules"],
                }
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )

        assert (prebuilt / "org--app" / "node_modules" / "pkg.json").exists()
        assert (
            prebuilt / "org--app" / "node_modules" / "pkg.json"
        ).read_text() == '{"name": "test"}'
        captured = capsys.readouterr()
        assert "Persisting" in captured.out
        assert "Persisted 1 directories" in captured.out

    def test_persist_dirs_raises_on_missing_dir(self, tmp_path):
        """persist_dirs raises when a declared dir doesn't exist post-build.

        The earlier behavior silently skipped, producing an image where the
        config promised dependencies but none were actually persisted. See
        #2087.
        """
        from docker_setup import persist_build_dirs

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        with pytest.raises(RuntimeError, match="does not exist .* after build"):
            persist_build_dirs(
                [
                    {
                        "repo": "org/app",
                        "commands": ["npm ci"],
                        "persist_dirs": ["nonexistent_dir"],
                    }
                ],
                repo_deps_base=repo_deps,
                prebuilt_base=tmp_path / "prebuilt",
            )

    def test_persist_dirs_blocks_path_traversal(self, tmp_path, capsys):
        """persist_dirs rejects paths that escape the build context."""
        from docker_setup import persist_build_dirs

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        persist_build_dirs(
            [
                {
                    "repo": "org/app",
                    "commands": ["npm ci"],
                    "persist_dirs": ["../../../etc"],
                }
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=tmp_path / "prebuilt",
        )

        captured = capsys.readouterr()
        assert "escapes build context" in captured.out

    def test_manifest_preserves_persist_dirs(self, tmp_path):
        """Test that persist_dirs is preserved through manifest loading."""
        import json

        manifest = {
            "build_commands": [
                {
                    "repo": "org/app",
                    "commands": ["npm ci"],
                    "watch_files": ["package.json"],
                    "persist_dirs": ["node_modules"],
                }
            ]
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert len(result) == 1
        assert result[0]["persist_dirs"] == ["node_modules"]


class TestPersistSystemDirs:
    """Tests for persist_system_dirs in build_commands."""

    def test_get_build_commands_includes_persist_system_dirs(self):
        """persist_system_dirs is extracted from config."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "commands": ["make install-go"],
                        "persist_system_dirs": ["/usr/local/go", "/usr/local/node"],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert len(result) == 1
        assert result[0]["persist_system_dirs"] == ["/usr/local/go", "/usr/local/node"]

    def test_get_build_commands_defaults_persist_system_dirs_to_empty(self):
        """Missing persist_system_dirs defaults to empty list."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "commands": ["npm ci"],
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert result[0]["persist_system_dirs"] == []

    def test_get_build_commands_handles_non_list_persist_system_dirs(self):
        """Non-list persist_system_dirs defaults to empty list."""
        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "commands": ["npm ci"],
                        "persist_system_dirs": "not-a-list",
                    }
                }
            }
        }
        result = get_build_commands(config)
        assert result[0]["persist_system_dirs"] == []

    def test_persist_system_dirs_copies_to_prebuilt(self, tmp_path, capsys):
        """persist_system_dirs copies absolute-path directories to _system_ subdir."""
        from docker_setup import persist_build_dirs

        # Simulate a system-level Go installation
        go_dir = tmp_path / "fake_root" / "usr" / "local" / "go" / "bin"
        go_dir.mkdir(parents=True)
        (go_dir / "go").write_text("#!/bin/sh\necho go")
        (go_dir.parent / "src").mkdir()

        prebuilt = tmp_path / "prebuilt-deps"
        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        # Use the fake root path as the system dir
        sys_dir = str(tmp_path / "fake_root" / "usr" / "local" / "go")

        persist_build_dirs(
            [
                {
                    "repo": "org/app",
                    "commands": ["install go"],
                    "persist_dirs": [],
                    "persist_system_dirs": [sys_dir],
                }
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )

        # Should be stored under __egg_system_dirs__/<stripped_path>
        dest = prebuilt / "__egg_system_dirs__" / sys_dir.lstrip("/")
        assert dest.is_dir()
        assert (dest / "bin" / "go").exists()

        captured = capsys.readouterr()
        assert "Persisting system dir" in captured.out

    def test_persist_system_dirs_raises_on_nonexistent(self, tmp_path):
        """persist_system_dirs raises when a declared path doesn't exist.

        Same fail-fast contract as `persist_dirs`: if the build commands
        promised to install something to /usr/local/bin but didn't, the
        image is broken. See #2087.
        """
        from docker_setup import persist_build_dirs

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        with pytest.raises(RuntimeError, match="does not exist .* after build"):
            persist_build_dirs(
                [
                    {
                        "repo": "org/app",
                        "commands": ["install go"],
                        "persist_dirs": [],
                        "persist_system_dirs": ["/nonexistent/path/go"],
                    }
                ],
                repo_deps_base=repo_deps,
                prebuilt_base=tmp_path / "prebuilt",
            )

    def test_persist_system_dirs_skips_relative_paths(self, tmp_path, capsys):
        """persist_system_dirs rejects non-absolute paths."""
        from docker_setup import persist_build_dirs

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        persist_build_dirs(
            [
                {
                    "repo": "org/app",
                    "commands": ["install go"],
                    "persist_dirs": [],
                    "persist_system_dirs": ["relative/path"],
                }
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=tmp_path / "prebuilt",
        )

        captured = capsys.readouterr()
        assert "is not absolute" in captured.out

    def test_persist_system_dirs_blocks_path_traversal(self, tmp_path, capsys):
        """persist_system_dirs rejects paths that resolve to denied locations via .. traversal."""
        from docker_setup import persist_build_dirs

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        # /usr/../etc resolves to /etc which is in DENIED_EXACT
        persist_build_dirs(
            [
                {
                    "repo": "org/app",
                    "commands": ["install go"],
                    "persist_dirs": [],
                    "persist_system_dirs": ["/usr/../etc"],
                }
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=tmp_path / "prebuilt",
        )

        captured = capsys.readouterr()
        assert "denied path" in captured.out
        assert "/etc" in captured.out

    def test_persist_system_dirs_blocks_denied_exact_paths(self, tmp_path, capsys):
        """persist_system_dirs rejects exact denied paths like /etc, /usr, /."""
        from docker_setup import persist_build_dirs

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app").mkdir(parents=True)

        persist_build_dirs(
            [
                {
                    "repo": "org/app",
                    "commands": ["install go"],
                    "persist_dirs": [],
                    "persist_system_dirs": ["/etc", "/usr", "/"],
                }
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=tmp_path / "prebuilt",
        )

        captured = capsys.readouterr()
        assert captured.out.count("denied path") == 3

    def test_persist_system_dirs_duplicate_across_repos(self, tmp_path, capsys):
        """persist_system_dirs handles duplicate dirs from different repos without crashing."""
        from docker_setup import persist_build_dirs

        # Create a fake system dir
        go_dir = tmp_path / "fake_go" / "bin"
        go_dir.mkdir(parents=True)
        (go_dir / "go").write_text("#!/bin/sh\necho go")

        prebuilt = tmp_path / "prebuilt-deps"
        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app1").mkdir(parents=True)
        (repo_deps / "org--app2").mkdir(parents=True)

        sys_dir = str(tmp_path / "fake_go")

        # Two repos both persist the same system dir — should not crash
        persist_build_dirs(
            [
                {
                    "repo": "org/app1",
                    "commands": ["install go"],
                    "persist_dirs": [],
                    "persist_system_dirs": [sys_dir],
                },
                {
                    "repo": "org/app2",
                    "commands": ["install go"],
                    "persist_dirs": [],
                    "persist_system_dirs": [sys_dir],
                },
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )

        # Both should succeed (dirs_exist_ok=True merges)
        dest = prebuilt / "__egg_system_dirs__" / sys_dir.lstrip("/")
        assert dest.is_dir()
        assert (dest / "bin" / "go").exists()

        captured = capsys.readouterr()
        assert captured.out.count("Persisting system dir") == 2

    def test_persist_system_dirs_overlap_first_writer_wins(self, tmp_path):
        """Overlapping persist_system_dirs across repos: first writer wins.

        Reproduces the realistic scenario where two repos install different
        binaries into the same system dir (e.g. jwbron/egg's ``uv`` and
        Khan/webapp's ``node`` both landing in ``/usr/local/bin``). The
        ``_copy_skip_existing`` callback in ``persist_build_dirs`` must not
        clobber repo-A's file with repo-B's.
        """
        from docker_setup import persist_build_dirs

        # Both repos point at the SAME source dir, but the file present at
        # the first repo's persist time is what should survive — simulate by
        # writing repo-B's overlapping content into the destination *between*
        # the two persist calls would require splitting the call. Instead
        # this test verifies the flag in the simpler, equivalent case:
        # repo-A's content is already at the destination when repo-B runs.
        bin_dir = tmp_path / "fake_root" / "usr" / "local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "shared").write_text("repo-A version")
        (bin_dir / "uv").write_text("repo-A only")

        repo_deps = tmp_path / "repo-deps"
        (repo_deps / "org--app-a").mkdir(parents=True)
        (repo_deps / "org--app-b").mkdir(parents=True)
        prebuilt = tmp_path / "prebuilt"

        # First persist call writes repo-A's content.
        persist_build_dirs(
            [
                {
                    "repo": "org/app-a",
                    "commands": ["x"],
                    "persist_dirs": [],
                    "persist_system_dirs": [str(bin_dir)],
                },
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )

        # Now mutate the source so repo-B "would" install a different version
        # of the shared file, then run repo-B's persist over the existing
        # destination. _copy_skip_existing must skip the existing 'shared'.
        (bin_dir / "shared").write_text("repo-B version (should be skipped)")
        (bin_dir / "node").write_text("repo-B only")

        persist_build_dirs(
            [
                {
                    "repo": "org/app-b",
                    "commands": ["y"],
                    "persist_dirs": [],
                    "persist_system_dirs": [str(bin_dir)],
                },
            ],
            repo_deps_base=repo_deps,
            prebuilt_base=prebuilt,
        )

        dest = prebuilt / "__egg_system_dirs__" / str(bin_dir).lstrip("/")
        # Shared file: first writer wins (repo-A's content survived).
        assert (dest / "shared").read_text() == "repo-A version"
        # Both repo-only files coexist (idempotent merge).
        assert (dest / "uv").read_text() == "repo-A only"
        assert (dest / "node").read_text() == "repo-B only"

    def test_manifest_preserves_persist_system_dirs(self, tmp_path):
        """persist_system_dirs is preserved through manifest loading."""
        import json

        manifest = {
            "build_commands": [
                {
                    "repo": "org/app",
                    "commands": ["install go"],
                    "watch_files": ["go.mod"],
                    "persist_dirs": [],
                    "persist_system_dirs": ["/usr/local/go"],
                }
            ]
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))

        result = load_build_commands_manifest(str(manifest_file))
        assert len(result) == 1
        assert result[0]["persist_system_dirs"] == ["/usr/local/go"]


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
