"""Tests for sandbox egg_lib auth module."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from egg_lib.auth import (
    _read_secrets_env,
    get_anthropic_api_key,
    get_anthropic_auth_method,
    get_claude_oauth_token,
)


class TestGetClaudeOauthToken:
    """Tests for get_claude_oauth_token function."""

    def test_from_env(self, monkeypatch):
        """Get OAuth token from environment variable."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-123")
        assert get_claude_oauth_token() == "oauth-token-123"

    def test_proxy_injected_skipped(self, monkeypatch):
        """Proxy-injected placeholder tokens are skipped."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "PROXY-INJECTED-placeholder")
        assert get_claude_oauth_token() is None

    def test_from_secrets_env(self, monkeypatch, tmp_path):
        """Get OAuth token from secrets.env file."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        # Mock Config.USER_CONFIG_DIR
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_file = config_dir / "secrets.env"
        secrets_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=file-oauth-token\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            assert get_claude_oauth_token() == "file-oauth-token"

    def test_not_found(self, monkeypatch, tmp_path):
        """Return None when token not available."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            assert get_claude_oauth_token() is None

    def test_empty_env_var(self, monkeypatch, tmp_path):
        """Empty env var falls through to file."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            # Empty string is falsy, so falls through to file check
            result = get_claude_oauth_token()
            assert result is None


class TestGetAnthropicApiKey:
    """Tests for get_anthropic_api_key function."""

    def test_from_env(self, monkeypatch):
        """Get API key from environment variable."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")
        assert get_anthropic_api_key() == "sk-ant-test123"

    def test_from_secrets_env(self, monkeypatch, tmp_path):
        """Get API key from secrets.env file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_file = config_dir / "secrets.env"
        secrets_file.write_text("ANTHROPIC_API_KEY=sk-ant-fromfile\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            assert get_anthropic_api_key() == "sk-ant-fromfile"

    def test_from_legacy_file(self, monkeypatch, tmp_path):
        """Get API key from legacy dedicated file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # No secrets.env file
        api_key_file = config_dir / "anthropic-api-key"
        api_key_file.write_text("sk-ant-legacy\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            assert get_anthropic_api_key() == "sk-ant-legacy"

    def test_not_found(self, monkeypatch, tmp_path):
        """Return None when no API key found."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            assert get_anthropic_api_key() is None


class TestGetAnthropicAuthMethod:
    """Tests for get_anthropic_auth_method function."""

    def test_from_env_api_key(self, monkeypatch, tmp_path):
        """Get auth method from env - api_key."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_auth_method() == "api_key"

    def test_from_env_oauth(self, monkeypatch, tmp_path):
        """Get auth method from env - oauth."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_auth_method() == "oauth"

    def test_from_config_yaml(self, monkeypatch, tmp_path):
        """Get auth method from config.yaml."""
        monkeypatch.delenv("ANTHROPIC_AUTH_METHOD", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("anthropic_auth_method: api_key\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_auth_method() == "api_key"

    def test_inferred_from_oauth_token(self, monkeypatch, tmp_path):
        """Infer oauth method from available token."""
        monkeypatch.delenv("ANTHROPIC_AUTH_METHOD", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "valid-oauth")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_auth_method() == "oauth"

    def test_inferred_from_api_key(self, monkeypatch, tmp_path):
        """Infer api_key method from available key."""
        monkeypatch.delenv("ANTHROPIC_AUTH_METHOD", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_auth_method() == "api_key"

    def test_default_oauth(self, monkeypatch, tmp_path):
        """Default is oauth when nothing configured."""
        monkeypatch.delenv("ANTHROPIC_AUTH_METHOD", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            assert get_anthropic_auth_method() == "oauth"

    def test_invalid_env_value_falls_through(self, monkeypatch, tmp_path):
        """Invalid env value falls through to next source."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "invalid")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = tmp_path
            # Falls through to default
            result = get_anthropic_auth_method()
            assert result == "oauth"


class TestReadSecretsEnv:
    """Tests for _read_secrets_env function."""

    def test_parse_simple_file(self, tmp_path):
        """Parse simple KEY=VALUE file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_file = config_dir / "secrets.env"
        secrets_file.write_text("KEY1=value1\nKEY2=value2\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            result = _read_secrets_env()
            assert result["KEY1"] == "value1"
            assert result["KEY2"] == "value2"

    def test_skip_comments(self, tmp_path):
        """Skip comment lines."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_file = config_dir / "secrets.env"
        secrets_file.write_text("# Comment\nKEY=value\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            result = _read_secrets_env()
            assert "KEY" in result
            assert len(result) == 1

    def test_skip_empty_lines(self, tmp_path):
        """Skip empty lines."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_file = config_dir / "secrets.env"
        secrets_file.write_text("\n\nKEY=value\n\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            result = _read_secrets_env()
            assert len(result) == 1

    def test_missing_file(self, tmp_path):
        """Return empty dict for missing file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            result = _read_secrets_env()
            assert result == {}

    def test_value_with_equals(self, tmp_path):
        """Handle values containing equals signs."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        secrets_file = config_dir / "secrets.env"
        secrets_file.write_text("KEY=value=with=equals\n")

        with patch("egg_lib.auth.Config") as mock_config:
            mock_config.USER_CONFIG_DIR = config_dir
            result = _read_secrets_env()
            assert result["KEY"] == "value=with=equals"
