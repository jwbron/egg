"""Tests for sandbox/egg_lib/setup_flow.py - Interactive setup process."""

import sys
from pathlib import Path
from unittest.mock import patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.setup_flow import (
    _configure_repo_checks,
    _create_general_config,
    _create_launcher_secret,
    _create_repositories_config,
    _get_template_path,
    _read_secrets_env,
    _write_secrets_env,
    add_standard_mounts,
    check_host_setup,
    setup,
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


class TestGetTemplatePath:
    """Tests for _get_template_path."""

    def test_returns_path(self):
        """Returns a Path object."""
        result = _get_template_path()
        assert isinstance(result, Path)
        assert "repositories.yaml.example" in str(result)
        assert "config" in str(result)


class TestReadSecretsEnv:
    """Tests for _read_secrets_env."""

    def test_reads_key_value_pairs(self, tmp_path):
        """Reads KEY=VALUE pairs from secrets.env."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("KEY1=value1\nKEY2=value2\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_skips_comments_and_blanks(self, tmp_path):
        """Skips comment and blank lines."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("# comment\n\nKEY=value\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY": "value"}

    def test_handles_no_file(self, tmp_path):
        """Returns empty dict when file doesn't exist."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {}

    def test_handles_equals_in_value(self, tmp_path):
        """Handles values with = in them."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("KEY=val=ue\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY": "val=ue"}


class TestWriteSecretsEnv:
    """Tests for _write_secrets_env."""

    def test_writes_secrets_file(self, tmp_path):
        """Writes secrets to file."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _write_secrets_env({"ANTHROPIC_API_KEY": "sk-ant-api-test"})
            secrets_file = tmp_path / "secrets.env"
            assert secrets_file.exists()
            content = secrets_file.read_text()
            assert "ANTHROPIC_API_KEY=sk-ant-api-test" in content

    def test_groups_by_category(self, tmp_path):
        """Groups secrets by category in output."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _write_secrets_env(
                {
                    "CLAUDE_CODE_OAUTH_TOKEN": "token",
                    "GITHUB_APP_ID": "123",
                    "GITHUB_TOKEN": "ghp_test",
                }
            )
            content = (tmp_path / "secrets.env").read_text()
            assert "# Claude Authentication" in content
            assert "# GitHub App" in content
            assert "# GitHub Tokens" in content

    def test_writes_other_category(self, tmp_path):
        """Writes uncategorized secrets under Other."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _write_secrets_env({"CUSTOM_KEY": "value"})
            content = (tmp_path / "secrets.env").read_text()
            assert "# Other" in content
            assert "CUSTOM_KEY=value" in content

    def test_sets_permissions(self, tmp_path):
        """Sets 0o600 permissions on secrets file."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _write_secrets_env({"KEY": "value"})
            secrets_file = tmp_path / "secrets.env"
            assert oct(secrets_file.stat().st_mode & 0o777) == "0o600"

    def test_creates_parent_dir(self, tmp_path):
        """Creates parent directory if needed."""
        config_dir = tmp_path / "subdir"
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            _write_secrets_env({"KEY": "value"})
            assert (config_dir / "secrets.env").exists()


class TestCreateLauncherSecret:
    """Tests for _create_launcher_secret."""

    def test_creates_secret_file(self, tmp_path):
        """Creates launcher-secret file when it doesn't exist."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _create_launcher_secret()
            secret_file = tmp_path / "launcher-secret"
            assert secret_file.exists()
            content = secret_file.read_text()
            assert len(content) > 20  # URL-safe token

    def test_doesnt_overwrite_existing(self, tmp_path):
        """Does not overwrite existing launcher-secret."""
        secret_file = tmp_path / "launcher-secret"
        secret_file.write_text("existing-secret")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _create_launcher_secret()
            assert secret_file.read_text() == "existing-secret"

    def test_sets_permissions(self, tmp_path):
        """Sets 0o600 permissions on secret file."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            _create_launcher_secret()
            secret_file = tmp_path / "launcher-secret"
            assert oct(secret_file.stat().st_mode & 0o777) == "0o600"


class TestCreateGeneralConfig:
    """Tests for _create_general_config."""

    def test_creates_config_with_oauth(self, tmp_path):
        """Creates config.yaml with oauth auth method."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch(
                "egg_lib.setup_flow._read_secrets_env",
                return_value={"CLAUDE_CODE_OAUTH_TOKEN": "token"},
            ):
                result = _create_general_config()
                assert result is True
                config_file = tmp_path / "config.yaml"
                assert config_file.exists()
                content = config_file.read_text()
                assert "oauth" in content

    def test_creates_config_with_api_key(self, tmp_path):
        """Creates config.yaml with api_key auth method."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch(
                "egg_lib.setup_flow._read_secrets_env", return_value={"ANTHROPIC_API_KEY": "key"}
            ):
                result = _create_general_config()
                assert result is True

    def test_doesnt_overwrite_unchanged(self, tmp_path):
        """Doesn't overwrite config when auth method hasn't changed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("anthropic_auth_method: oauth\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch(
                "egg_lib.setup_flow._read_secrets_env",
                return_value={"CLAUDE_CODE_OAUTH_TOKEN": "token"},
            ):
                result = _create_general_config()
                assert result is True

    def test_updates_changed_auth_method(self, tmp_path):
        """Updates config when auth method changed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("anthropic_auth_method: api_key\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch(
                "egg_lib.setup_flow._read_secrets_env",
                return_value={"CLAUDE_CODE_OAUTH_TOKEN": "token"},
            ):
                result = _create_general_config()
                assert result is True

    def test_defaults_to_oauth(self, tmp_path):
        """Defaults to oauth when no credentials configured."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.setup_flow._read_secrets_env", return_value={}):
                result = _create_general_config()
                assert result is True


class TestAddStandardMounts:
    """Tests for add_standard_mounts."""

    def test_adds_certs_mount(self, tmp_path):
        """Adds shared certs mount when directory exists."""
        certs_dir = tmp_path / ".egg-shared-certs"
        certs_dir.mkdir()
        mount_args = []
        with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
            add_standard_mounts(mount_args)
            assert "-v" in mount_args
            assert any("shared/certs" in a for a in mount_args)

    def test_skips_missing_certs(self, tmp_path):
        """Skips mount when shared certs directory doesn't exist."""
        mount_args = []
        with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
            add_standard_mounts(mount_args)
            assert mount_args == []

    def test_quiet_mode(self, tmp_path, capsys):
        """Quiet mode suppresses output."""
        certs_dir = tmp_path / ".egg-shared-certs"
        certs_dir.mkdir()
        mount_args = []
        with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
            add_standard_mounts(mount_args, quiet=True)
            captured = capsys.readouterr()
            assert captured.out == ""


class TestSetup:
    """Tests for setup."""

    def test_cancelled_by_user(self):
        """Setup returns False when user cancels."""
        with patch("builtins.input", return_value="no"):
            result = setup()
            assert result is False

    def test_full_setup_flow(self, tmp_path):
        """Setup orchestrates all steps."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            mock_config.CONFIG_DIR = tmp_path / "cache"
            with patch("builtins.input", return_value="yes"):
                with patch("egg_lib.setup_flow._create_secrets_config", return_value=True):
                    with patch("egg_lib.setup_flow._create_launcher_secret"):
                        with patch(
                            "egg_lib.setup_flow._create_repositories_config", return_value=True
                        ):
                            with patch(
                                "egg_lib.setup_flow._create_general_config", return_value=True
                            ):
                                with patch("egg_lib.setup_flow.build_image", return_value=True):
                                    with patch(
                                        "egg_lib.setup_flow.Path.home", return_value=tmp_path
                                    ):
                                        result = setup()
                                        assert result is True

    def test_fails_on_secrets_config_failure(self, tmp_path):
        """Setup returns False when secrets config fails."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            mock_config.CONFIG_DIR = tmp_path / "cache"
            with patch("builtins.input", return_value="yes"):
                with patch("egg_lib.setup_flow._create_secrets_config", return_value=False):
                    with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
                        result = setup()
                        assert result is False

    def test_fails_on_repos_config_failure(self, tmp_path):
        """Setup returns False when repos config fails."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            mock_config.CONFIG_DIR = tmp_path / "cache"
            with patch("builtins.input", return_value="yes"):
                with patch("egg_lib.setup_flow._create_secrets_config", return_value=True):
                    with patch("egg_lib.setup_flow._create_launcher_secret"):
                        with patch(
                            "egg_lib.setup_flow._create_repositories_config", return_value=False
                        ):
                            with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
                                result = setup()
                                assert result is False

    def test_fails_on_build_failure(self, tmp_path):
        """Setup returns False when Docker build fails."""
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            mock_config.CONFIG_DIR = tmp_path / "cache"
            with patch("builtins.input", return_value="yes"):
                with patch("egg_lib.setup_flow._create_secrets_config", return_value=True):
                    with patch("egg_lib.setup_flow._create_launcher_secret"):
                        with patch(
                            "egg_lib.setup_flow._create_repositories_config", return_value=True
                        ):
                            with patch(
                                "egg_lib.setup_flow._create_general_config", return_value=True
                            ):
                                with patch("egg_lib.setup_flow.build_image", return_value=False):
                                    with patch(
                                        "egg_lib.setup_flow.Path.home", return_value=tmp_path
                                    ):
                                        result = setup()
                                        assert result is False


class TestConfigureRepoChecks:
    """Tests for _configure_repo_checks."""

    def test_skips_when_gate_declined(self):
        """Returns empty dict when user declines the top-level gate prompt."""
        with patch("builtins.input", return_value="no"):
            result = _configure_repo_checks(["user/repo1", "user/repo2"])
            assert result == {}

    def test_skips_when_user_declines_per_repo(self):
        """Returns empty dict when user passes gate but declines per repo."""
        # "yes" for the gate, "no" for each repo
        inputs = iter(["yes", "no", "no"])
        with patch("builtins.input", side_effect=inputs):
            result = _configure_repo_checks(["user/repo1", "user/repo2"])
            assert result == {}

    def test_configures_checks_for_single_repo(self):
        """Stores checks when user configures a repo."""
        # "yes" for gate, "yes" for repo1, checks, "" to finish, "no" for repo2
        inputs = iter(["yes", "yes", "lint", "make lint", "test", "make test", "", "no"])
        with patch("builtins.input", side_effect=inputs):
            result = _configure_repo_checks(["user/repo1", "user/repo2"])
            assert "user/repo1" in result
            assert result["user/repo1"]["checks"] == [
                {"name": "lint", "command": "make lint"},
                {"name": "test", "command": "make test"},
            ]
            assert "user/repo2" not in result

    def test_configures_checks_for_multiple_repos(self):
        """Stores checks for multiple repos."""
        inputs = iter(
            [
                "yes",  # gate
                "yes",  # repo1
                "lint",
                "npm run lint",
                "",  # repo1 done
                "yes",  # repo2
                "test",
                "pytest",
                "",  # repo2 done
            ]
        )
        with patch("builtins.input", side_effect=inputs):
            result = _configure_repo_checks(["user/repo1", "user/repo2"])
            assert len(result) == 2
            assert result["user/repo1"]["checks"] == [
                {"name": "lint", "command": "npm run lint"},
            ]
            assert result["user/repo2"]["checks"] == [
                {"name": "test", "command": "pytest"},
            ]

    def test_skips_check_with_empty_command(self):
        """Skips a check entry when no command is provided."""
        inputs = iter(["yes", "yes", "lint", "", "test", "make test", ""])
        with patch("builtins.input", side_effect=inputs):
            result = _configure_repo_checks(["user/repo1"])
            checks = result["user/repo1"]["checks"]
            assert len(checks) == 1
            assert checks[0]["name"] == "test"

    def test_no_entry_when_all_checks_skipped(self):
        """No repo_settings entry when user starts but adds no valid checks."""
        inputs = iter(["yes", "yes", "lint", "", ""])
        with patch("builtins.input", side_effect=inputs):
            result = _configure_repo_checks(["user/repo1"])
            assert result == {}

    def test_empty_repo_list(self):
        """Returns empty dict for empty writable repos list."""
        result = _configure_repo_checks([])
        assert result == {}


class TestCreateRepositoriesConfigWithChecks:
    """Integration test for _create_repositories_config with check commands."""

    def test_generated_yaml_contains_repo_settings_with_checks(self, tmp_path):
        """Verifies that the generated repositories.yaml includes repo_settings
        with configured check commands."""
        import yaml

        inputs = iter(
            [
                "testuser",  # GitHub username
                "/dev/null",  # local repo path (will fail validation, that's fine)
                "",  # end local repos
                "testuser/my-app",  # writable repo
                "",  # end writable repos
                "mybot",  # bot name
                "egg",  # branch prefix
                "yes",  # gate: configure SDLC check commands?
                "yes",  # configure checks for testuser/my-app?
                "lint",  # check name
                "make lint",  # check command
                "test",  # check name
                "make test",  # check command
                "",  # done adding checks
            ]
        )
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("builtins.input", side_effect=inputs):
                result = _create_repositories_config()

        assert result is True
        config_file = tmp_path / "repositories.yaml"
        assert config_file.exists()

        config = yaml.safe_load(config_file.read_text())
        assert "repo_settings" in config
        assert "testuser/my-app" in config["repo_settings"]
        checks = config["repo_settings"]["testuser/my-app"]["checks"]
        assert len(checks) == 2
        assert checks[0] == {"name": "lint", "command": "make lint"}
        assert checks[1] == {"name": "test", "command": "make test"}
