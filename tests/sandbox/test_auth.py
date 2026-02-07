"""Tests for sandbox/egg_lib/auth.py - Authentication and API key management."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.auth import (
    _read_secrets_env,
    get_anthropic_api_key,
    get_anthropic_auth_method,
    get_claude_oauth_token,
    get_github_app_token,
    get_github_readonly_token,
    get_github_token,
)


class TestGetClaudeOauthToken:
    """Tests for get_claude_oauth_token."""

    def test_from_env(self, monkeypatch):
        """Returns token from environment variable."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test123")
        assert get_claude_oauth_token() == "sk-ant-oat-test123"

    def test_skips_proxy_injected(self, monkeypatch):
        """Skips PROXY-INJECTED placeholder tokens."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "PROXY-INJECTED-placeholder")
        assert get_claude_oauth_token() is None

    def test_from_secrets_env(self, monkeypatch, tmp_path):
        """Returns token from secrets.env file."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-fromfile\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_claude_oauth_token() == "sk-ant-oat-fromfile"

    def test_returns_none_when_not_found(self, monkeypatch, tmp_path):
        """Returns None when token not found anywhere."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_claude_oauth_token() is None

    def test_secrets_file_doesnt_exist(self, monkeypatch, tmp_path):
        """Returns None when secrets.env doesn't exist."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path / "nonexistent"
            assert get_claude_oauth_token() is None


class TestGetAnthropicApiKey:
    """Tests for get_anthropic_api_key."""

    def test_from_env(self, monkeypatch):
        """Returns API key from environment variable."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test123")
        assert get_anthropic_api_key() == "sk-ant-api-test123"

    def test_from_secrets_env(self, monkeypatch, tmp_path):
        """Returns API key from secrets.env file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("ANTHROPIC_API_KEY=sk-ant-api-fromfile\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_api_key() == "sk-ant-api-fromfile"

    def test_from_legacy_file(self, monkeypatch, tmp_path):
        """Returns API key from legacy dedicated file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        api_key_file = tmp_path / "anthropic-api-key"
        api_key_file.write_text("sk-ant-api-legacy\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_api_key() == "sk-ant-api-legacy"

    def test_returns_none_when_not_found(self, monkeypatch, tmp_path):
        """Returns None when API key not found anywhere."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_api_key() is None


class TestGetAnthropicAuthMethod:
    """Tests for get_anthropic_auth_method."""

    def test_from_env_api_key(self, monkeypatch):
        """Returns 'api_key' from environment."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        assert get_anthropic_auth_method() == "api_key"

    def test_from_env_oauth(self, monkeypatch):
        """Returns 'oauth' from environment."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        assert get_anthropic_auth_method() == "oauth"

    def test_from_config_yaml(self, monkeypatch, tmp_path):
        """Returns method from config.yaml."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "")
        config_file = tmp_path / "config.yaml"
        config_file.write_text("anthropic_auth_method: api_key\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.auth.get_claude_oauth_token", return_value=None):
                with patch("egg_lib.auth.get_anthropic_api_key", return_value=None):
                    assert get_anthropic_auth_method() == "api_key"

    def test_infer_from_oauth_token(self, monkeypatch, tmp_path):
        """Infers 'oauth' when OAuth token is available."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.auth.get_claude_oauth_token", return_value="token"):
                assert get_anthropic_auth_method() == "oauth"

    def test_infer_from_api_key(self, monkeypatch, tmp_path):
        """Infers 'api_key' when API key is available."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.auth.get_claude_oauth_token", return_value=None):
                with patch("egg_lib.auth.get_anthropic_api_key", return_value="sk-ant-api"):
                    assert get_anthropic_auth_method() == "api_key"

    def test_defaults_to_oauth(self, monkeypatch, tmp_path):
        """Defaults to 'oauth' when nothing available."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.auth.get_claude_oauth_token", return_value=None):
                with patch("egg_lib.auth.get_anthropic_api_key", return_value=None):
                    assert get_anthropic_auth_method() == "oauth"

    def test_config_yaml_bad_file(self, monkeypatch, tmp_path):
        """Handles corrupt config.yaml gracefully."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "")
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{{bad yaml")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            with patch("egg_lib.auth.get_claude_oauth_token", return_value=None):
                with patch("egg_lib.auth.get_anthropic_api_key", return_value=None):
                    # Should not raise, should default
                    assert get_anthropic_auth_method() == "oauth"


class TestGetGithubToken:
    """Tests for get_github_token."""

    def test_returns_valid_token(self):
        """Returns token when HostConfig provides valid ghp_ token."""
        mock_host_config_instance = MagicMock()
        mock_host_config_instance.github_token = "ghp_test123"
        mock_host_config_class = MagicMock(return_value=mock_host_config_instance)
        with patch.dict("sys.modules", {"config": MagicMock(), "config.host_config": MagicMock(HostConfig=mock_host_config_class)}):
            result = get_github_token()
            assert result == "ghp_test123"

    def test_returns_github_pat_token(self):
        """Returns token starting with github_pat_."""
        mock_host_config_instance = MagicMock()
        mock_host_config_instance.github_token = "github_pat_test123"
        mock_host_config_class = MagicMock(return_value=mock_host_config_instance)
        with patch.dict("sys.modules", {"config": MagicMock(), "config.host_config": MagicMock(HostConfig=mock_host_config_class)}):
            result = get_github_token()
            assert result == "github_pat_test123"

    def test_returns_none_on_import_error(self):
        """Returns None when HostConfig is not importable."""
        with patch("egg_lib.auth.Path"):
            with patch.dict("sys.modules", {"config": None, "config.host_config": None}):
                # Force ImportError
                result = get_github_token()
                assert result is None

    def test_returns_none_for_invalid_token(self):
        """Returns None when token doesn't match known prefixes."""
        mock_host_config_instance = MagicMock()
        mock_host_config_instance.github_token = "invalid_token"
        mock_host_config_class = MagicMock(return_value=mock_host_config_instance)
        with patch.dict("sys.modules", {"config": MagicMock(), "config.host_config": MagicMock(HostConfig=mock_host_config_class)}):
            result = get_github_token()
            assert result is None


class TestGetGithubReadonlyToken:
    """Tests for get_github_readonly_token."""

    def test_returns_token(self):
        """Returns readonly token from HostConfig."""
        mock_host_config_instance = MagicMock()
        mock_host_config_instance.get_secret.return_value = "ghp_readonly123"
        mock_host_config_class = MagicMock(return_value=mock_host_config_instance)
        with patch.dict("sys.modules", {"config": MagicMock(), "config.host_config": MagicMock(HostConfig=mock_host_config_class)}):
            result = get_github_readonly_token()
            assert result == "ghp_readonly123"

    def test_returns_none_on_import_error(self):
        """Returns None when HostConfig is not importable."""
        with patch("egg_lib.auth.Path"):
            with patch.dict("sys.modules", {"config": None, "config.host_config": None}):
                result = get_github_readonly_token()
                assert result is None

    def test_returns_none_when_no_token(self):
        """Returns None when no readonly token configured."""
        mock_host_config_instance = MagicMock()
        mock_host_config_instance.get_secret.return_value = None
        mock_host_config_class = MagicMock(return_value=mock_host_config_instance)
        with patch.dict("sys.modules", {"config": MagicMock(), "config.host_config": MagicMock(HostConfig=mock_host_config_class)}):
            result = get_github_readonly_token()
            assert result is None


class TestReadSecretsEnv:
    """Tests for _read_secrets_env."""

    def test_reads_key_value_pairs(self, tmp_path):
        """Reads KEY=VALUE pairs from secrets.env."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("KEY1=value1\nKEY2=value2\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_skips_comments(self, tmp_path):
        """Skips comment lines."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("# comment\nKEY=value\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY": "value"}

    def test_skips_blank_lines(self, tmp_path):
        """Skips blank lines."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("\n\nKEY=value\n\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY": "value"}

    def test_handles_equals_in_value(self, tmp_path):
        """Handles values containing = signs."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("KEY=value=with=equals\n")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {"KEY": "value=with=equals"}

    def test_returns_empty_when_no_file(self, tmp_path):
        """Returns empty dict when secrets.env doesn't exist."""
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            result = _read_secrets_env()
            assert result == {}


class TestGetGithubAppToken:
    """Tests for get_github_app_token."""

    def test_returns_none_when_no_app_id(self, tmp_path):
        """Returns None when GITHUB_APP_ID is missing."""
        with patch("egg_lib.auth._read_secrets_env", return_value={}):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                assert get_github_app_token() is None

    def test_returns_none_when_no_installation_id(self, tmp_path):
        """Returns None when GITHUB_APP_INSTALLATION_ID is missing."""
        with patch("egg_lib.auth._read_secrets_env", return_value={"GITHUB_APP_ID": "123"}):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                assert get_github_app_token() is None

    def test_returns_none_when_no_pem_file(self, tmp_path):
        """Returns None when github-app.pem doesn't exist."""
        with patch("egg_lib.auth._read_secrets_env", return_value={
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                assert get_github_app_token() is None

    def test_returns_none_when_script_not_found(self, tmp_path):
        """Returns None when token script doesn't exist."""
        (tmp_path / "github-app.pem").write_text("fake-key")
        with patch("egg_lib.auth._read_secrets_env", return_value={
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                # Script won't exist at the computed path
                assert get_github_app_token() is None

    def test_returns_token_on_success(self, tmp_path):
        """Returns token when script succeeds."""
        (tmp_path / "github-app.pem").write_text("fake-key")
        with patch("egg_lib.auth._read_secrets_env", return_value={
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                mock_result = MagicMock(returncode=0, stdout="ghs_testtoken123\n", stderr="")
                with patch("egg_lib.auth.subprocess.run", return_value=mock_result):
                    with patch("egg_lib.auth.Path.__file__", create=True):
                        # Need to make the script path exist
                        with patch.object(Path, "exists", return_value=True):
                            result = get_github_app_token()
                            assert result == "ghs_testtoken123"

    def test_returns_none_on_script_failure(self, tmp_path):
        """Returns None when script fails."""
        (tmp_path / "github-app.pem").write_text("fake-key")
        with patch("egg_lib.auth._read_secrets_env", return_value={
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                mock_result = MagicMock(returncode=1, stdout="", stderr="error")
                with patch("egg_lib.auth.subprocess.run", return_value=mock_result):
                    with patch.object(Path, "exists", return_value=True):
                        result = get_github_app_token()
                        assert result is None

    def test_handles_timeout(self, tmp_path):
        """Returns None on script timeout."""
        (tmp_path / "github-app.pem").write_text("fake-key")
        with patch("egg_lib.auth._read_secrets_env", return_value={
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                with patch("egg_lib.auth.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
                    with patch.object(Path, "exists", return_value=True):
                        result = get_github_app_token()
                        assert result is None

    def test_handles_generic_exception(self, tmp_path):
        """Returns None on generic exception."""
        (tmp_path / "github-app.pem").write_text("fake-key")
        with patch("egg_lib.auth._read_secrets_env", return_value={
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
        }):
            with patch("egg_lib.auth.Config") as mock_config:
                mock_config.USER_CONFIG_DIR = tmp_path
                with patch("egg_lib.auth.subprocess.run", side_effect=Exception("boom")):
                    with patch.object(Path, "exists", return_value=True):
                        result = get_github_app_token()
                        assert result is None
