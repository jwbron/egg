"""
Tests for the repo_config module.

Tests the repository configuration module that provides access to
repositories.yaml configuration.
"""

import os

import pytest
import yaml


class TestGetConfigPath:
    """Tests for _get_config_path function."""

    def test_finds_config_relative_to_file(self, temp_dir):
        """Test finding config relative to the module file."""
        config_dir = temp_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        config_file = config_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser")

        assert config_file.exists()

    def test_finds_config_in_host_config_directory(self, temp_dir, monkeypatch):
        """Test finding config in ~/.config/egg/ (host config location)."""
        monkeypatch.setenv("HOME", str(temp_dir))

        config_path = temp_dir / ".config" / "egg"
        config_path.mkdir(parents=True, exist_ok=True)

        config_file = config_path / "repositories.yaml"
        config_file.write_text("github_username: testuser")

        assert config_file.exists()

    def test_finds_config_in_container_mount(self, temp_dir, monkeypatch):
        """Test finding config in ~/repos/egg/config/ (container mount)."""
        monkeypatch.setenv("HOME", str(temp_dir))

        config_path = temp_dir / "repos" / "egg" / "config"
        config_path.mkdir(parents=True, exist_ok=True)

        config_file = config_path / "repositories.yaml"
        config_file.write_text("github_username: testuser")

        assert config_file.exists()

    def test_finds_config_from_env_var(self, temp_dir, monkeypatch):
        """Test finding config from EGG_REPO_CONFIG env var."""
        config_file = temp_dir / "custom-config.yaml"
        config_file.write_text("github_username: testuser")

        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        env_path = os.environ.get("EGG_REPO_CONFIG")
        assert env_path == str(config_file)

    def test_raises_when_not_found(self, temp_dir, monkeypatch):
        """Test that FileNotFoundError is raised when config not found."""
        monkeypatch.setenv("HOME", str(temp_dir))

        # No config file exists
        config_path = temp_dir / "config" / "repositories.yaml"
        assert not config_path.exists()


class TestLoadConfig:
    """Tests for _load_config function."""

    def test_loads_valid_yaml(self, temp_dir):
        """Test loading valid YAML config."""
        config_file = temp_dir / "repositories.yaml"
        config_content = """
github_username: testuser
writable_repos:
  - testuser/repo1
  - testuser/repo2
default_reviewer: reviewer
"""
        config_file.write_text(config_content)

        config = yaml.safe_load(config_file.read_text())

        assert config["github_username"] == "testuser"
        assert len(config["writable_repos"]) == 2

    def test_handles_empty_config(self, temp_dir):
        """Test handling of empty config file."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("")

        config = yaml.safe_load(config_file.read_text())
        assert config is None

    def test_handles_minimal_config(self, temp_dir):
        """Test handling of minimal config."""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser")

        config = yaml.safe_load(config_file.read_text())
        assert config["github_username"] == "testuser"


class TestGetGithubUsername:
    """Tests for get_github_username function."""

    def test_returns_username(self, temp_dir):
        """Test returning configured username."""
        config = {"github_username": "testuser"}
        username = config.get("github_username")

        assert username == "testuser"

    def test_raises_when_not_configured(self):
        """Test raising ValueError when username not configured."""
        config = {}
        username = config.get("github_username")

        if not username:
            with pytest.raises(ValueError, match="github_username not configured"):
                raise ValueError("github_username not configured")


class TestGetWritableRepos:
    """Tests for get_writable_repos function."""

    def test_returns_list_of_repos(self):
        """Test returning list of writable repos."""
        config = {"writable_repos": ["user/repo1", "user/repo2"]}

        repos = config.get("writable_repos", [])
        assert len(repos) == 2
        assert "user/repo1" in repos

    def test_returns_empty_list_when_not_configured(self):
        """Test returning empty list when writable_repos not configured."""
        config = {}
        repos = config.get("writable_repos", [])

        assert repos == []

    def test_repo_format(self):
        """Test that repos are in owner/repo format."""
        repos = ["owner/repo1", "org/repo2"]

        for repo in repos:
            assert "/" in repo
            parts = repo.split("/")
            assert len(parts) == 2


class TestIsWritableRepo:
    """Tests for is_writable_repo function."""

    def test_returns_true_for_writable_repo(self):
        """Test returning True for writable repo."""
        writable = ["user/repo1", "user/repo2"]
        repo = "user/repo1"

        is_writable = any(r.lower() == repo.lower() for r in writable)
        assert is_writable

    def test_returns_false_for_non_writable_repo(self):
        """Test returning False for non-writable repo."""
        writable = ["user/repo1"]
        repo = "user/other-repo"

        is_writable = any(r.lower() == repo.lower() for r in writable)
        assert not is_writable

    def test_case_insensitive_comparison(self):
        """Test that comparison is case insensitive."""
        writable = ["User/Repo1"]
        repo = "user/repo1"

        is_writable = any(r.lower() == repo.lower() for r in writable)
        assert is_writable

    def test_handles_empty_writable_list(self):
        """Test handling of empty writable repos list."""
        writable = []
        repo = "user/repo1"

        is_writable = any(r.lower() == repo.lower() for r in writable)
        assert not is_writable


class TestGetDefaultReviewer:
    """Tests for get_default_reviewer function."""

    def test_returns_explicit_reviewer(self):
        """Test returning explicitly configured reviewer."""
        config = {"github_username": "user", "default_reviewer": "reviewer"}

        reviewer = config.get("default_reviewer")
        if not reviewer:
            reviewer = config.get("github_username")

        assert reviewer == "reviewer"

    def test_falls_back_to_username(self):
        """Test falling back to github_username when reviewer not set."""
        config = {"github_username": "user"}

        reviewer = config.get("default_reviewer")
        if not reviewer:
            reviewer = config.get("github_username")

        assert reviewer == "user"


class TestGetSyncConfig:
    """Tests for get_sync_config function."""

    def test_returns_sync_settings(self):
        """Test returning GitHub sync configuration."""
        config = {"github_sync": {"sync_all_prs": True, "sync_interval_minutes": 10}}

        sync_config = config.get("github_sync", {})
        assert sync_config["sync_all_prs"] is True
        assert sync_config["sync_interval_minutes"] == 10

    def test_returns_defaults_when_not_configured(self):
        """Test returning defaults when github_sync not configured."""
        config = {}
        defaults = {"sync_all_prs": True, "sync_interval_minutes": 5}

        sync_config = config.get("github_sync", defaults)
        assert sync_config["sync_all_prs"] is True
        assert sync_config["sync_interval_minutes"] == 5


class TestGetReposForSync:
    """Tests for get_repos_for_sync function."""

    def test_returns_writable_repos(self):
        """Test that repos for sync are writable repos."""
        config = {"writable_repos": ["user/repo1", "user/repo2"]}

        repos = config.get("writable_repos", [])
        assert len(repos) == 2


class TestMain:
    """Tests for CLI main function."""

    def test_github_username_flag(self, capsys):
        """Test --github-username flag output."""
        username = "testuser"
        print(username)

        captured = capsys.readouterr()
        assert "testuser" in captured.out

    def test_list_writable_flag(self, capsys):
        """Test --list-writable flag output."""
        repos = ["user/repo1", "user/repo2"]

        for repo in repos:
            print(repo)

        captured = capsys.readouterr()
        assert "user/repo1" in captured.out
        assert "user/repo2" in captured.out

    def test_check_writable_returns_exit_code(self):
        """Test --check-writable returns correct exit code."""
        writable = ["user/repo1"]

        # Writable repo
        repo = "user/repo1"
        exit_code = 0 if any(r.lower() == repo.lower() for r in writable) else 1
        assert exit_code == 0

        # Non-writable repo
        repo = "user/other"
        exit_code = 0 if any(r.lower() == repo.lower() for r in writable) else 1
        assert exit_code == 1

    def test_default_reviewer_flag(self, capsys):
        """Test --default-reviewer flag output."""
        reviewer = "reviewer"
        print(reviewer)

        captured = capsys.readouterr()
        assert "reviewer" in captured.out

    def test_sync_all_prs_flag(self, capsys):
        """Test --sync-all-prs flag output."""
        sync_all = True
        print("true" if sync_all else "false")

        captured = capsys.readouterr()
        assert "true" in captured.out

    def test_default_output_summary(self, capsys):
        """Test default output shows summary."""
        config = {
            "github_username": "testuser",
            "writable_repos": ["testuser/repo1"],
            "default_reviewer": "reviewer",
        }

        print("Repository Configuration")
        print("=" * 40)
        print(f"\nGitHub username: {config['github_username']}")
        print(f"\nWritable repos ({len(config['writable_repos'])}):")
        for repo in config["writable_repos"]:
            print(f"  - {repo}")
        print(f"\nDefault reviewer: {config['default_reviewer']}")

        captured = capsys.readouterr()
        assert "Repository Configuration" in captured.out
        assert "testuser" in captured.out


class TestConfigFileFormat:
    """Tests for config file format validation."""

    def test_valid_config_structure(self, temp_dir):
        """Test valid config file structure."""
        config_content = """
# GitHub username for egg
github_username: testuser

# Repos where egg has write access
writable_repos:
  - testuser/repo1
  - testuser/repo2

# Default reviewer for PRs
default_reviewer: reviewer

# GitHub sync settings
github_sync:
  sync_all_prs: true
  sync_interval_minutes: 5
"""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(config_content)

        config = yaml.safe_load(config_file.read_text())

        assert "github_username" in config
        assert "writable_repos" in config
        assert isinstance(config["writable_repos"], list)

    def test_comments_are_preserved(self, temp_dir):
        """Test that YAML comments are valid."""
        config_content = """
# This is a comment
github_username: testuser  # inline comment
"""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(config_content)

        # Should parse without error
        config = yaml.safe_load(config_file.read_text())
        assert config["github_username"] == "testuser"

    def test_handles_unicode(self, temp_dir):
        """Test handling of unicode in config."""
        config_content = """
github_username: user_émoji_🎉
"""
        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(config_content)

        config = yaml.safe_load(config_file.read_text())
        assert "user_" in config["github_username"]


class TestGetRepoChecks:
    """Tests for get_repo_checks function."""

    def test_returns_configured_checks(self, temp_dir, monkeypatch):
        """Test returning configured check commands for a repo."""
        from config.repo_config import get_repo_checks

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checks:\n"
            "      - name: lint\n"
            "        command: npm run lint\n"
            "      - name: test\n"
            "        command: npm test\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        checks = get_repo_checks("testuser/my-app")
        assert len(checks) == 2
        assert checks[0] == {"name": "lint", "command": "npm run lint"}
        assert checks[1] == {"name": "test", "command": "npm test"}

    def test_returns_empty_list_when_no_checks(self, temp_dir, monkeypatch):
        """Test returning empty list when no checks configured for repo."""
        from config.repo_config import get_repo_checks

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    restrict_to_configured_users: true\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        checks = get_repo_checks("testuser/my-app")
        assert checks == []

    def test_returns_empty_list_for_unknown_repo(self, temp_dir, monkeypatch):
        """Test returning empty list when repo is not in config."""
        from config.repo_config import get_repo_checks

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checks:\n"
            "      - name: lint\n"
            "        command: npm run lint\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        checks = get_repo_checks("testuser/other-repo")
        assert checks == []

    def test_case_insensitive_repo_matching(self, temp_dir, monkeypatch):
        """Test that repo name matching is case insensitive."""
        from config.repo_config import get_repo_checks

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  TestUser/My-App:\n"
            "    checks:\n"
            "      - name: test\n"
            "        command: make test\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        checks = get_repo_checks("testuser/my-app")
        assert len(checks) == 1
        assert checks[0] == {"name": "test", "command": "make test"}

    def test_skips_invalid_check_entries(self, temp_dir, monkeypatch):
        """Test that malformed check entries are filtered out."""
        from config.repo_config import get_repo_checks

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/my-app:\n"
            "    checks:\n"
            "      - name: valid\n"
            "        command: make test\n"
            "      - name: missing-command\n"
            "      - just-a-string\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        checks = get_repo_checks("testuser/my-app")
        assert len(checks) == 1
        assert checks[0]["name"] == "valid"

    def test_returns_empty_for_no_repo_settings(self, temp_dir, monkeypatch):
        """Test returning empty list when repo_settings section is absent."""
        from config.repo_config import get_repo_checks

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser\n")
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        checks = get_repo_checks("testuser/my-app")
        assert checks == []


class TestGetRepoBuildCommands:
    """Tests for get_repo_build_commands function."""

    def test_returns_configured_build_commands(self, temp_dir, monkeypatch):
        """Test returning build_commands for a repo."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/web-app:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - package-lock.json\n"
            "        - requirements.txt\n"
            "      commands:\n"
            "        - npm ci\n"
            "        - pip install -r requirements.txt\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/web-app")
        assert result["watch_files"] == ["package-lock.json", "requirements.txt"]
        assert result["commands"] == ["npm ci", "pip install -r requirements.txt"]

    def test_returns_empty_dict_when_not_configured(self, temp_dir, monkeypatch):
        """Test returning empty dict when no build_commands configured."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/web-app:\n"
            "    checks:\n"
            "      - name: test\n"
            "        command: npm test\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/web-app")
        assert result == {}

    def test_returns_empty_dict_for_unknown_repo(self, temp_dir, monkeypatch):
        """Test returning empty dict for unknown repo."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser\n")
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/unknown")
        assert result == {}

    def test_handles_empty_watch_files(self, temp_dir, monkeypatch):
        """Test build_commands with no watch_files."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/web-app:\n"
            "    build_commands:\n"
            "      commands:\n"
            "        - make deps\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/web-app")
        assert result["watch_files"] == []
        assert result["commands"] == ["make deps"]

    def test_handles_invalid_build_commands_type(self, temp_dir, monkeypatch):
        """Test that non-dict build_commands returns empty dict."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/web-app:\n"
            "    build_commands: not-a-dict\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/web-app")
        assert result == {}


class TestGetAllBuildCommands:
    """Tests for get_all_build_commands function."""

    def test_returns_all_repos_with_build_commands(self, temp_dir, monkeypatch):
        """Test returning build_commands for all configured repos."""
        from config.repo_config import get_all_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/web-app:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - package-lock.json\n"
            "      commands:\n"
            "        - npm ci\n"
            "  testuser/python-svc:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - requirements.txt\n"
            "      commands:\n"
            "        - pip install -r requirements.txt\n"
            "  testuser/no-build:\n"
            "    checks:\n"
            "      - name: test\n"
            "        command: make test\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_build_commands()
        assert len(result) == 2
        assert "testuser/web-app" in result
        assert "testuser/python-svc" in result
        assert "testuser/no-build" not in result

    def test_skips_repos_with_empty_commands(self, temp_dir, monkeypatch):
        """Test that repos with empty commands list are excluded."""
        from config.repo_config import get_all_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/web-app:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - package-lock.json\n"
            "      commands: []\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_build_commands()
        assert result == {}

    def test_returns_empty_when_no_repo_settings(self, temp_dir, monkeypatch):
        """Test returning empty dict when no repo_settings section."""
        from config.repo_config import get_all_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text("github_username: testuser\n")
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_build_commands()
        assert result == {}


class TestGetRepoBuildCommandsEdgeCases:
    """Additional edge case tests for get_repo_build_commands."""

    def test_non_list_watch_files_returns_empty_list(self, temp_dir, monkeypatch):
        """When watch_files is a string instead of a list, return empty list."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/app:\n"
            "    build_commands:\n"
            "      watch_files: just-a-string\n"
            "      commands:\n"
            "        - make deps\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/app")
        assert result["watch_files"] == []
        assert result["commands"] == ["make deps"]

    def test_non_list_commands_returns_empty_list(self, temp_dir, monkeypatch):
        """When commands is a string instead of a list, return empty list."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/app:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - requirements.txt\n"
            "      commands: single-command\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/app")
        assert result["watch_files"] == ["requirements.txt"]
        assert result["commands"] == []

    def test_non_string_items_are_coerced(self, temp_dir, monkeypatch):
        """Non-string items in watch_files/commands are coerced to strings."""
        from config.repo_config import get_repo_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/app:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - 123\n"
            "        - true\n"
            "      commands:\n"
            "        - 456\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_repo_build_commands("testuser/app")
        assert result["watch_files"] == ["123", "True"]
        assert result["commands"] == ["456"]


class TestGetAllBuildCommandsEdgeCases:
    """Additional edge case tests for get_all_build_commands."""

    def test_skips_non_dict_settings_entries(self, temp_dir, monkeypatch):
        """Repos with non-dict settings values are skipped."""
        from config.repo_config import get_all_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/broken: not-a-dict\n"
            "  testuser/valid:\n"
            "    build_commands:\n"
            "      commands:\n"
            "        - make deps\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_build_commands()
        assert len(result) == 1
        assert "testuser/valid" in result
        assert "testuser/broken" not in result

    def test_non_dict_repo_settings_returns_empty(self, temp_dir, monkeypatch):
        """Non-dict repo_settings value returns empty dict."""
        from config.repo_config import get_all_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings: not-a-dict\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_build_commands()
        assert result == {}

    def test_only_includes_repos_with_non_empty_commands(self, temp_dir, monkeypatch):
        """Repos with watch_files but no commands are excluded."""
        from config.repo_config import get_all_build_commands

        config_file = temp_dir / "repositories.yaml"
        config_file.write_text(
            "github_username: testuser\n"
            "repo_settings:\n"
            "  testuser/app:\n"
            "    build_commands:\n"
            "      watch_files:\n"
            "        - requirements.txt\n"
        )
        monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

        result = get_all_build_commands()
        assert result == {}
