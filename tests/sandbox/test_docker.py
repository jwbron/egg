"""Tests for sandbox/egg_lib/docker.py - build-context helpers."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))


class TestIsDangerousDir:
    """Tests for is_dangerous_dir."""

    def test_safe_directory(self, tmp_path):
        """Returns False for safe directory."""
        from egg_lib.docker import is_dangerous_dir

        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        assert is_dangerous_dir(safe_dir) is False

    def test_dangerous_directory(self):
        """Returns True for dangerous directory."""
        # Use actual Config.DANGEROUS_DIRS
        from egg_lib.config import Config
        from egg_lib.docker import is_dangerous_dir

        if Config.DANGEROUS_DIRS:
            # Test that an actual dangerous dir is detected
            dangerous = Config.DANGEROUS_DIRS[0]
            if dangerous.exists():
                assert is_dangerous_dir(dangerous) is True


class TestGetLocalRepoPath:
    """Tests for _get_local_repo_path."""

    def test_finds_repo_by_full_path(self, tmp_path):
        """Matches repo by owner/name at end of path."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "org" / "my-app"
        repo_dir.mkdir(parents=True)

        config = {"local_repos": {"paths": [str(repo_dir)]}}

        result = _get_local_repo_path(config, "org/my-app")
        assert result == repo_dir

    def test_finds_repo_by_name_only(self, tmp_path):
        """Falls back to matching just the repo name."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "my-app"
        repo_dir.mkdir(parents=True)

        config = {"local_repos": {"paths": [str(repo_dir)]}}

        result = _get_local_repo_path(config, "org/my-app")
        assert result == repo_dir

    def test_returns_none_for_missing_repo(self, tmp_path):
        """Returns None when repo not in local_repos."""
        from egg_lib.docker import _get_local_repo_path

        config = {"local_repos": {"paths": [str(tmp_path / "other-repo")]}}

        result = _get_local_repo_path(config, "org/my-app")
        assert result is None

    def test_returns_none_for_empty_config(self):
        """Returns None with no local_repos config."""
        from egg_lib.docker import _get_local_repo_path

        result = _get_local_repo_path({}, "org/my-app")
        assert result is None


class TestCopyRepoWatchFiles:
    """Tests for populate_build_context."""

    def test_refuses_target_dir_not_named_repo_deps(self, tmp_path):
        """Footgun guard: refuse to rmtree a target whose basename != repo-deps."""
        import pytest
        from egg_lib.docker import populate_build_context

        bad_target = tmp_path / "important-data"
        bad_target.mkdir()
        (bad_target / "do-not-delete.txt").write_text("precious")

        # Mock _load_repos_config so a CI-runner host yaml can't influence the
        # test. The guard fires before any config read, so the patched value
        # never matters — but bare-calling it would otherwise reach real
        # production yaml-load + path resolution from a unit test.
        with patch("egg_lib.docker._load_repos_config", return_value={}):
            with pytest.raises(ValueError, match="must be named 'repo-deps'"):
                populate_build_context(bad_target, quiet=True)

        # Pre-existing content must remain untouched.
        assert (bad_target / "do-not-delete.txt").read_text() == "precious"

    def test_copies_watch_files(self, tmp_path):
        """Copies watch files from local repos to build context."""
        from egg_lib.docker import populate_build_context

        # Set up local repo with a watch file
        repo_dir = tmp_path / "org" / "web-app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "package-lock.json").write_text('{"lockfileVersion": 3}')

        # Config with build_commands
        config = {
            "repo_settings": {
                "org/web-app": {
                    "build_commands": {
                        "watch_files": ["package-lock.json"],
                        "commands": ["npm ci"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        # Mock the config loading and Config.CONFIG_DIR
        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # Check the watch file was copied
        dest = build_dir / "repo-deps" / "org--web-app" / "package-lock.json"
        assert dest.exists()
        assert dest.read_text() == '{"lockfileVersion": 3}'

    def test_writes_manifest_json(self, tmp_path):
        """Writes manifest.json with build commands into repo-deps."""

        from egg_lib.docker import populate_build_context

        repo_dir = tmp_path / "org" / "web-app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "package-lock.json").write_text("{}")

        config = {
            "repo_settings": {
                "org/web-app": {
                    "build_commands": {
                        "watch_files": ["package-lock.json"],
                        "commands": ["npm ci"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 1
        assert build_commands[0]["repo"] == "org/web-app"
        assert build_commands[0]["commands"] == ["npm ci"]
        assert build_commands[0]["watch_files"] == ["package-lock.json"]
        assert manifest["extra_packages"] == {"apt": [], "dnf": []}

    def test_manifest_includes_multiple_repos(self, tmp_path):
        """Manifest includes all repos with build_commands."""

        from egg_lib.docker import populate_build_context

        repo_a = tmp_path / "org" / "app-a"
        repo_a.mkdir(parents=True)
        repo_b = tmp_path / "org" / "app-b"
        repo_b.mkdir(parents=True)

        config = {
            "repo_settings": {
                "org/app-a": {
                    "build_commands": {
                        "commands": ["make deps"],
                    }
                },
                "org/app-b": {
                    "build_commands": {
                        "watch_files": ["go.sum"],
                        "commands": ["go mod download"],
                    }
                },
                "org/no-build": {
                    "checks": [{"name": "test"}],
                },
            },
            "local_repos": {"paths": [str(repo_a), str(repo_b)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 2
        repos = [m["repo"] for m in build_commands]
        assert "org/app-a" in repos
        assert "org/app-b" in repos

    def test_manifest_includes_extra_packages(self, tmp_path):
        """Manifest includes extra_packages when configured in docker_setup."""

        from egg_lib.docker import populate_build_context

        config = {
            "repo_settings": {},
            "docker_setup": {
                "extra_packages": {
                    "apt": ["golang-go", "nodejs"],
                    "dnf": ["golang", "nodejs"],
                }
            },
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["extra_packages"] == {
            "apt": ["golang-go", "nodejs"],
            "dnf": ["golang", "nodejs"],
        }
        assert manifest["build_commands"] == []

    def test_manifest_includes_generic_packages(self, tmp_path):
        """Generic packages are appended to both apt and dnf lists in manifest."""

        from egg_lib.docker import populate_build_context

        config = {
            "repo_settings": {},
            "docker_setup": {
                "extra_packages": {
                    "apt": ["libssl-dev"],
                    "dnf": ["openssl-devel"],
                    "packages": ["curl", "wget"],
                }
            },
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["extra_packages"]["apt"] == ["libssl-dev", "curl", "wget"]
        assert manifest["extra_packages"]["dnf"] == ["openssl-devel", "curl", "wget"]

    def test_no_manifest_when_no_build_commands_or_extra_packages(self, tmp_path):
        """No manifest.json when no repos have build_commands or extra_packages."""
        from egg_lib.docker import populate_build_context

        config = {"repo_settings": {"org/app": {"checks": []}}}

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert not manifest_path.exists()

    def test_skips_when_no_build_commands(self, tmp_path):
        """Does nothing when no repos have build_commands."""
        from egg_lib.docker import populate_build_context

        config = {"repo_settings": {"org/app": {"checks": []}}}

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # repo-deps should have the empty marker
        repo_deps = build_dir / "repo-deps"
        if repo_deps.exists():
            assert (repo_deps / ".empty").exists()


class TestLoadReposConfig:
    """Tests for _load_repos_config."""

    def test_returns_empty_when_file_missing(self, tmp_path):
        """Returns empty dict when repositories.yaml doesn't exist."""
        from egg_lib.docker import _load_repos_config

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = tmp_path / "nonexistent.yaml"
            result = _load_repos_config()

        assert result == {}

    def test_returns_empty_on_malformed_yaml(self, tmp_path):
        """Returns empty dict when YAML is invalid."""
        from egg_lib.docker import _load_repos_config

        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(": : :\n  invalid: [unclosed")

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = bad_yaml
            result = _load_repos_config()

        assert result == {}

    def test_returns_empty_on_empty_file(self, tmp_path):
        """Returns empty dict when YAML file is empty."""
        from egg_lib.docker import _load_repos_config

        empty_file = tmp_path / "empty.yaml"
        empty_file.write_text("")

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = empty_file
            result = _load_repos_config()

        assert result == {}

    def test_loads_valid_config(self, tmp_path):
        """Successfully loads a valid repositories.yaml."""
        from egg_lib.docker import _load_repos_config

        config_file = tmp_path / "repos.yaml"
        config_file.write_text(
            "repo_settings:\n  org/app:\n    build_commands:\n      commands:\n        - make\n"
        )

        with patch("egg_lib.docker.Config") as mock_config:
            mock_config.REPOS_CONFIG_FILE = config_file
            result = _load_repos_config()

        assert "repo_settings" in result
        assert "org/app" in result["repo_settings"]


class TestGetLocalRepoPathEdgeCases:
    """Edge case tests for _get_local_repo_path."""

    def test_non_dict_local_repos_returns_none(self):
        """Non-dict local_repos value returns None."""
        from egg_lib.docker import _get_local_repo_path

        config = {"local_repos": "not-a-dict"}
        result = _get_local_repo_path(config, "org/app")
        assert result is None

    def test_non_list_paths_returns_none(self):
        """Non-list paths value returns None."""
        from egg_lib.docker import _get_local_repo_path

        config = {"local_repos": {"paths": "not-a-list"}}
        result = _get_local_repo_path(config, "org/app")
        assert result is None

    def test_case_insensitive_matching(self, tmp_path):
        """Path matching is case-insensitive."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "MyOrg" / "MyApp"
        repo_dir.mkdir(parents=True)

        config = {"local_repos": {"paths": [str(repo_dir)]}}
        result = _get_local_repo_path(config, "myorg/myapp")
        assert result == repo_dir

    def test_skips_nonexistent_paths(self, tmp_path):
        """Non-existent paths in the list are skipped."""
        from egg_lib.docker import _get_local_repo_path

        real_dir = tmp_path / "org" / "app"
        real_dir.mkdir(parents=True)

        config = {
            "local_repos": {
                "paths": [
                    str(tmp_path / "nonexistent" / "org" / "app"),
                    str(real_dir),
                ]
            }
        }

        result = _get_local_repo_path(config, "org/app")
        assert result == real_dir

    def test_single_component_repo_name(self, tmp_path):
        """Repo name without owner (no slash) is handled."""
        from egg_lib.docker import _get_local_repo_path

        repo_dir = tmp_path / "myapp"
        repo_dir.mkdir()

        config = {"local_repos": {"paths": [str(repo_dir)]}}
        # Single-component name: no fallback matching (len(repo_parts) == 1)
        result = _get_local_repo_path(config, "myapp")
        assert result == repo_dir


class TestCopyRepoWatchFilesEdgeCases:
    """Edge case tests for populate_build_context."""

    def test_nested_watch_files_preserve_structure(self, tmp_path):
        """Watch files in subdirectories preserve their directory structure."""
        from egg_lib.docker import populate_build_context

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        nested_dir = repo_dir / "config"
        nested_dir.mkdir()
        (nested_dir / "settings.json").write_text('{"key": "value"}')

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["config/settings.json"],
                        "commands": ["make deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        dest = build_dir / "repo-deps" / "org--app" / "config" / "settings.json"
        assert dest.exists()
        assert dest.read_text() == '{"key": "value"}'

    def test_cleans_up_stale_repo_deps(self, tmp_path):
        """Old repo-deps directory is cleaned before copying new files."""
        from egg_lib.docker import populate_build_context

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        # Create stale repo-deps
        stale_dir = build_dir / "repo-deps" / "old--repo"
        stale_dir.mkdir(parents=True)
        (stale_dir / "stale.txt").write_text("old")

        config = {"repo_settings": {"org/app": {"checks": []}}}

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # Stale directory should be removed
        assert not stale_dir.exists()

    def test_skips_manifest_entry_when_local_path_unresolved(self, tmp_path):
        """No manifest entry for a repo whose local path can't be found.

        Without a local path the watch-files dir won't exist in the build
        context, so emitting a manifest entry would only surface as a
        downstream RuntimeError from docker-setup.py:run_build_commands
        (``watch files directory ... does not exist``). The host already
        warned at populate-time; keep that as the single source of truth.
        """

        from egg_lib.docker import populate_build_context

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        # Config with commands but no local repo path match
        config = {
            "repo_settings": {
                "org/unknown-repo": {
                    "build_commands": {
                        "watch_files": ["req.txt"],
                        "commands": ["pip install -r req.txt"],
                    }
                }
            },
            "local_repos": {"paths": []},
        }

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # No buildable repos and no extra_packages => populate writes the
        # global ``.empty`` marker and skips manifest.json entirely.
        assert (build_dir / "repo-deps" / ".empty").exists()
        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert not manifest_path.exists()

    def test_warns_when_watch_files_not_list(self, tmp_path):
        """Malformed build_commands.watch_files surfaces as a host-side warn,
        not a silent skip. Without this, a yaml typo (e.g. ``watch_files:
        package-lock.json`` instead of a list) silently drops the repo from
        both the copy step and the manifest, producing an image with no
        per-repo build steps and no log line. Regression guard: a future
        refactor that strips the warn back to a bare ``continue`` should
        fail this test.
        """
        from egg_lib.docker import populate_build_context

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": "not-a-list",
                        "commands": ["make"],
                    }
                }
            },
        }
        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with (
            patch("egg_lib.docker._load_repos_config", return_value=config),
            patch("egg_lib.docker.warn") as mock_warn,
        ):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        assert any(
            "watch_files and commands must be lists" in call.args[0]
            for call in mock_warn.call_args_list
        ), f"expected malformed-list warn, got: {mock_warn.call_args_list}"

    def test_multiple_repos_copy_separately(self, tmp_path):
        """Watch files from multiple repos are copied to separate directories."""
        from egg_lib.docker import populate_build_context

        repo_a = tmp_path / "org" / "app-a"
        repo_a.mkdir(parents=True)
        (repo_a / "package.json").write_text('{"name": "a"}')

        repo_b = tmp_path / "org" / "app-b"
        repo_b.mkdir(parents=True)
        (repo_b / "requirements.txt").write_text("flask\n")

        config = {
            "repo_settings": {
                "org/app-a": {
                    "build_commands": {
                        "watch_files": ["package.json"],
                        "commands": ["npm ci"],
                    }
                },
                "org/app-b": {
                    "build_commands": {
                        "watch_files": ["requirements.txt"],
                        "commands": ["pip install -r requirements.txt"],
                    }
                },
            },
            "local_repos": {"paths": [str(repo_a), str(repo_b)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        assert (build_dir / "repo-deps" / "org--app-a" / "package.json").exists()
        assert (build_dir / "repo-deps" / "org--app-b" / "requirements.txt").exists()

    def test_missing_watch_file_is_skipped(self, tmp_path):
        """Watch file that doesn't exist in local repo is skipped."""

        from egg_lib.docker import populate_build_context

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        # Don't create the watch file

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["nonexistent.json"],
                        "commands": ["npm ci"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # Dest directory should exist but be empty (no files copied)
        dest_dir = build_dir / "repo-deps" / "org--app"
        assert dest_dir.exists()
        # Manifest should still be written so build commands execute
        manifest_path = build_dir / "repo-deps" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        build_commands = manifest["build_commands"]
        assert len(build_commands) == 1
        assert build_commands[0]["commands"] == ["npm ci"]


class TestWatchFilePathTraversal:
    """Tests for path traversal validation in watch file handling."""

    def test_copy_rejects_path_traversal(self, tmp_path):
        """Watch files with .. components that escape the repo are rejected."""
        from egg_lib.docker import populate_build_context

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)
        (repo_dir / "legit.txt").write_text("ok")

        # Create a file outside the repo that a traversal would reach
        (tmp_path / "secret.txt").write_text("sensitive")

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["../../secret.txt"],
                        "commands": ["make deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # The traversal file should NOT be copied
        repo_deps = build_dir / "repo-deps"
        assert repo_deps.exists(), "repo-deps directory should always be created"
        # Should only have the .empty marker, not the secret file
        all_files = list(repo_deps.rglob("*"))
        file_names = [f.name for f in all_files if f.is_file()]
        assert "secret.txt" not in file_names

    def test_copy_rejects_symlink_escaping_repo(self, tmp_path):
        """Symlinks pointing outside the repo boundary are rejected."""
        from egg_lib.docker import populate_build_context

        repo_dir = tmp_path / "org" / "app"
        repo_dir.mkdir(parents=True)

        # Create a file outside the repo
        outside_file = tmp_path / "outside-secret.txt"
        outside_file.write_text("sensitive data")

        # Create a symlink inside the repo pointing outside
        symlink = repo_dir / "sneaky-link.txt"
        symlink.symlink_to(outside_file)

        config = {
            "repo_settings": {
                "org/app": {
                    "build_commands": {
                        "watch_files": ["sneaky-link.txt"],
                        "commands": ["make deps"],
                    }
                }
            },
            "local_repos": {"paths": [str(repo_dir)]},
        }

        build_dir = tmp_path / "build-context"
        build_dir.mkdir()

        with patch("egg_lib.docker._load_repos_config", return_value=config):
            populate_build_context(build_dir / "repo-deps", quiet=True)

        # The symlink target should NOT be copied
        repo_deps = build_dir / "repo-deps"
        assert repo_deps.exists(), "repo-deps directory should always be created"
        all_files = list(repo_deps.rglob("*"))
        file_names = [f.name for f in all_files if f.is_file()]
        assert "sneaky-link.txt" not in file_names
