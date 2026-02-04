"""Tests for Anthropic credentials manager."""

import sys
from pathlib import Path

import pytest

# Add gateway to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "gateway"))

from anthropic_credentials import (
    AnthropicCredential,
    AnthropicCredentialsManager,
    parse_env_file,
    reset_credentials_manager,
)


class TestAnthropicCredential:
    """Test AnthropicCredential dataclass."""

    def test_api_key_type(self):
        """Test API key credential type."""
        cred = AnthropicCredential(header_name="x-api-key", header_value="sk-ant-test")
        assert cred.is_api_key
        assert not cred.is_oauth
        assert cred.header_value == "sk-ant-test"

    def test_oauth_type(self):
        """Test OAuth credential type."""
        cred = AnthropicCredential(header_name="Authorization", header_value="Bearer oauth-token")
        assert cred.is_oauth
        assert not cred.is_api_key
        assert cred.header_value == "Bearer oauth-token"


class TestParseEnvFile:
    """Test parse_env_file function."""

    def test_basic_parsing(self, tmp_path):
        """Test basic KEY=value parsing."""
        env_file = tmp_path / "test.env"
        env_file.write_text("KEY1=value1\nKEY2=value2\n")

        result = parse_env_file(env_file)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_quoted_values(self, tmp_path):
        """Test quoted value parsing."""
        env_file = tmp_path / "test.env"
        env_file.write_text("KEY1=\"quoted value\"\nKEY2='single quoted'\n")

        result = parse_env_file(env_file)
        assert result == {"KEY1": "quoted value", "KEY2": "single quoted"}

    def test_comments_and_empty_lines(self, tmp_path):
        """Test that comments and empty lines are skipped."""
        env_file = tmp_path / "test.env"
        env_file.write_text("# Comment\n\nKEY=value\n")

        result = parse_env_file(env_file)
        assert result == {"KEY": "value"}

    def test_missing_file(self, tmp_path):
        """Test handling of missing file."""
        result = parse_env_file(tmp_path / "nonexistent.env")
        assert result == {}


class TestAnthropicCredentialsManager:
    """Test AnthropicCredentialsManager."""

    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset global manager before each test."""
        reset_credentials_manager()
        yield
        reset_credentials_manager()

    def test_load_api_key_from_secrets(self, tmp_path):
        """Test loading API key from secrets.env."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            'ANTHROPIC_API_KEY="sk-ant-test-key-12345678901234567890123456789012345678901234567890"'
        )

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert cred.is_api_key
        assert cred.header_name == "x-api-key"
        assert "sk-ant-test-key" in cred.header_value

    def test_load_oauth_token_from_secrets(self, tmp_path):
        """Test loading OAuth token from secrets.env."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('ANTHROPIC_OAUTH_TOKEN="oauth-test-token-1234567890"')

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert cred.is_oauth
        assert cred.header_name == "Authorization"
        assert cred.header_value == "Bearer oauth-test-token-1234567890"

    def test_oauth_takes_precedence(self, tmp_path):
        """Test that OAuth token takes precedence over API key."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            'ANTHROPIC_API_KEY="sk-ant-test-key-12345678901234567890123456789012345678901234567890"\n'
            'ANTHROPIC_OAUTH_TOKEN="oauth-test-token-1234567890"'
        )

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert cred.is_oauth  # OAuth takes precedence
        assert cred.header_value == "Bearer oauth-test-token-1234567890"

    def test_missing_secrets_file_returns_none(self, tmp_path):
        """Test that missing secrets file returns None."""
        manager = AnthropicCredentialsManager(secrets_path=tmp_path / "nonexistent.env")
        cred = manager.get_credential()

        assert cred is None

    def test_empty_secrets_file_returns_none(self, tmp_path):
        """Test that empty secrets file returns None."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("")

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None

    def test_no_anthropic_credentials_returns_none(self, tmp_path):
        """Test that file with no Anthropic credentials returns None."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('GITHUB_TOKEN="ghp_test"')

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None

    def test_mtime_based_caching(self, tmp_path):
        """Test that credentials are reloaded when file mtime changes."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('ANTHROPIC_OAUTH_TOKEN="token-v1-with-enough-length"')

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)

        # First access
        cred1 = manager.get_credential()
        assert "token-v1" in cred1.header_value

        # Modify file (updates mtime)
        import time

        time.sleep(0.1)  # Ensure mtime changes
        secrets_file.write_text('ANTHROPIC_OAUTH_TOKEN="token-v2-with-enough-length"')

        # Second access should reload
        cred2 = manager.get_credential()
        assert "token-v2" in cred2.header_value

    def test_reload_clears_cache(self, tmp_path):
        """Test that reload clears cache."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('ANTHROPIC_OAUTH_TOKEN="original-token-with-length"')

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)

        # First access
        cred1 = manager.get_credential()
        assert "original-token" in cred1.header_value

        # Modify file and reload
        secrets_file.write_text('ANTHROPIC_OAUTH_TOKEN="new-token-with-enough-length"')
        manager.reload()

        # Should get new value
        cred2 = manager.get_credential()
        assert "new-token" in cred2.header_value

    def test_short_api_key_rejected(self, tmp_path):
        """Test that short API keys are rejected."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('ANTHROPIC_API_KEY="sk-ant-short"')

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None  # Rejected as too short

    def test_short_oauth_token_rejected(self, tmp_path):
        """Test that short OAuth tokens are rejected."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('ANTHROPIC_OAUTH_TOKEN="short"')

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None  # Rejected as too short
