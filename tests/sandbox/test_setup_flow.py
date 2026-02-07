"""Tests for sandbox/egg_lib/setup_flow.py - Interactive setup process."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.setup_flow import (
    _create_general_config,
    _create_launcher_secret,
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
            _write_secrets_env({
                "CLAUDE_CODE_OAUTH_TOKEN": "token",
                "GITHUB_APP_ID": "123",
                "GITHUB_TOKEN": "ghp_test",
            })
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
            with patch("egg_lib.setup_flow._read_secrets_env", return_value={"CLAUDE_CODE_OAUTH_TOKEN": "token"}):
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
            with patch("egg_lib.setup_flow._read_secrets_env", return_value={"ANTHROPIC_API_KEY": "key"}):
                result = _create_general_config()
                assert result is True

    def test_doesnt_overwrite_unchanged(self, tmp_path):
        """Doesn't overwrite config when auth method hasn't changed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("anthropic_auth_method: oauth\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.setup_flow._read_secrets_env", return_value={"CLAUDE_CODE_OAUTH_TOKEN": "token"}):
                result = _create_general_config()
                assert result is True

    def test_updates_changed_auth_method(self, tmp_path):
        """Updates config when auth method changed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("anthropic_auth_method: api_key\n")
        with patch("egg_lib.setup_flow.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.setup_flow._read_secrets_env", return_value={"CLAUDE_CODE_OAUTH_TOKEN": "token"}):
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
                        with patch("egg_lib.setup_flow._create_repositories_config", return_value=True):
                            with patch("egg_lib.setup_flow._create_general_config", return_value=True):
                                with patch("egg_lib.setup_flow.build_image", return_value=True):
                                    with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
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
                        with patch("egg_lib.setup_flow._create_repositories_config", return_value=False):
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
                        with patch("egg_lib.setup_flow._create_repositories_config", return_value=True):
                            with patch("egg_lib.setup_flow._create_general_config", return_value=True):
                                with patch("egg_lib.setup_flow.build_image", return_value=False):
                                    with patch("egg_lib.setup_flow.Path.home", return_value=tmp_path):
                                        result = setup()
                                        assert result is False
