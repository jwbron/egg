"""Tests for shared egg_config configs (gateway, github, llm)."""

import json
import os
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from egg_config.configs.gateway import GatewayConfig, RateLimitConfig
from egg_config.configs.github import GitHubConfig
from egg_config.configs.llm import LLMConfig


class TestRateLimitConfig:
    """Tests for RateLimitConfig dataclass."""

    def test_defaults(self):
        """Default rate limits."""
        config = RateLimitConfig()
        assert config.git_push == 1000
        assert config.gh_pr_create == 500
        assert config.gh_pr_comment == 2000
        assert config.combined == 4000

    def test_custom_values(self):
        """Custom rate limits."""
        config = RateLimitConfig(git_push=100, combined=200)
        assert config.git_push == 100
        assert config.combined == 200


class TestGatewayConfig:
    """Tests for GatewayConfig class."""

    def test_defaults(self):
        """Default configuration values."""
        config = GatewayConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 9848
        assert config.secret == ""

    def test_validate_valid(self):
        """Valid config passes validation."""
        config = GatewayConfig(
            secret="a" * 32,
            port=9848,
        )
        result = config.validate()
        assert result.is_valid

    def test_validate_missing_secret(self):
        """Missing secret fails validation."""
        config = GatewayConfig(secret="")
        result = config.validate()
        assert not result.is_valid
        assert any("secret" in e for e in result.errors)

    def test_validate_short_secret_warning(self):
        """Short secret generates warning."""
        config = GatewayConfig(secret="short")
        result = config.validate()
        # Secret is present but short - should have warning
        assert any("shorter" in w for w in result.warnings)

    def test_validate_invalid_port(self):
        """Invalid port fails validation."""
        config = GatewayConfig(secret="a" * 32, port=-1)
        result = config.validate()
        assert not result.is_valid

    def test_validate_all_interfaces_warning(self):
        """Binding to all interfaces generates warning."""
        config = GatewayConfig(secret="a" * 32, host="0.0.0.0")
        result = config.validate()
        assert any("0.0.0.0" in w for w in result.warnings)

    def test_to_dict(self):
        """to_dict masks secret."""
        config = GatewayConfig(
            host="0.0.0.0",
            port=9848,
            secret="super-secret-value-12345",
        )
        d = config.to_dict()
        assert d["host"] == "0.0.0.0"
        assert d["port"] == 9848
        # Secret should be masked
        assert "super-secret-value-12345" not in d["secret"]

    def test_to_dict_rate_limits(self):
        """to_dict includes rate limits."""
        config = GatewayConfig(secret="test")
        d = config.to_dict()
        assert "rate_limits" in d
        assert "git_push" in d["rate_limits"]

    def test_from_env_default(self, monkeypatch, tmp_path):
        """from_env with defaults."""
        monkeypatch.delenv("GATEWAY_HOST", raising=False)
        monkeypatch.delenv("GATEWAY_PORT", raising=False)
        monkeypatch.setenv("EGG_LAUNCHER_SECRET", "test-secret-value")
        config = GatewayConfig.from_env()
        assert config.host == "0.0.0.0"
        assert config.port == 9848
        assert config.secret == "test-secret-value"
        assert config._secret_source == "environment"

    def test_from_env_custom_port(self, monkeypatch):
        """from_env with custom port."""
        monkeypatch.setenv("GATEWAY_PORT", "8080")
        monkeypatch.setenv("EGG_LAUNCHER_SECRET", "secret")
        config = GatewayConfig.from_env()
        assert config.port == 8080

    def test_from_env_invalid_port(self, monkeypatch):
        """from_env with invalid port falls back to default."""
        monkeypatch.setenv("GATEWAY_PORT", "not_a_port")
        monkeypatch.setenv("EGG_LAUNCHER_SECRET", "secret")
        config = GatewayConfig.from_env()
        assert config.port == 9848

    def test_from_env_secret_from_file(self, monkeypatch, tmp_path):
        """from_env reads secret from launcher-secret file."""
        monkeypatch.delenv("EGG_LAUNCHER_SECRET", raising=False)

        secret_dir = tmp_path / ".config" / "egg"
        secret_dir.mkdir(parents=True)
        (secret_dir / "launcher-secret").write_text("file-secret")

        monkeypatch.setenv("HOME", str(tmp_path))
        config = GatewayConfig.from_env()
        assert config.secret == "file-secret"
        assert config._secret_source == "launcher-secret file"

    def test_from_env_auto_generate(self, monkeypatch, tmp_path):
        """from_env auto-generates secret when not available."""
        monkeypatch.delenv("EGG_LAUNCHER_SECRET", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        config = GatewayConfig.from_env()
        assert config.secret != ""
        assert config._secret_source == "auto-generated"

    def test_health_check_no_secret(self):
        """Health check fails without secret."""
        config = GatewayConfig(secret="")
        result = config.health_check()
        assert not result.healthy


class TestGitHubConfig:
    """Tests for GitHubConfig class."""

    def test_defaults(self):
        """Default configuration."""
        config = GitHubConfig()
        assert config.token == ""
        assert config.username == "egg"

    def test_validate_missing_token(self):
        """Missing token fails validation."""
        config = GitHubConfig(token="")
        result = config.validate()
        assert not result.is_valid

    def test_validate_valid_pat(self):
        """Valid PAT passes validation."""
        config = GitHubConfig(token="ghp_" + "a" * 36)
        result = config.validate()
        assert result.is_valid

    def test_validate_expired_token(self):
        """Expired token fails validation."""
        config = GitHubConfig(
            token="ghp_" + "a" * 36,
            token_expires_at=datetime.now() - timedelta(hours=1),
        )
        result = config.validate()
        assert not result.is_valid

    def test_validate_expiring_soon_warning(self):
        """Token expiring soon generates warning."""
        config = GitHubConfig(
            token="ghp_" + "a" * 36,
            token_expires_at=datetime.now() + timedelta(minutes=2),
        )
        result = config.validate()
        assert any("expires" in w for w in result.warnings)

    def test_is_token_expired(self):
        """is_token_expired property."""
        config = GitHubConfig(
            token_expires_at=datetime.now() - timedelta(hours=1)
        )
        assert config.is_token_expired is True

    def test_is_token_not_expired(self):
        """Token not expired."""
        config = GitHubConfig(
            token_expires_at=datetime.now() + timedelta(hours=1)
        )
        assert config.is_token_expired is False

    def test_no_expiration(self):
        """No expiration time means not expired."""
        config = GitHubConfig()
        assert config.is_token_expired is False

    def test_token_expires_soon(self):
        """token_expires_soon property."""
        config = GitHubConfig(
            token_expires_at=datetime.now() + timedelta(minutes=2)
        )
        assert config.token_expires_soon is True

    def test_token_not_expiring_soon(self):
        """Token not expiring soon."""
        config = GitHubConfig(
            token_expires_at=datetime.now() + timedelta(hours=1)
        )
        assert config.token_expires_soon is False

    def test_to_dict_masks_tokens(self):
        """to_dict masks all token fields."""
        config = GitHubConfig(
            token="ghp_secret123",
            readonly_token="ghp_readonly456",
            incognito_token="ghp_incog789",
        )
        d = config.to_dict()
        assert "ghp_secret123" not in d["token"]
        assert "ghp_readonly456" not in d["readonly_token"]
        assert "ghp_incog789" not in d["incognito_token"]
        assert d["username"] == "egg"

    def test_to_dict_with_expiration(self):
        """to_dict includes expiration time."""
        dt = datetime(2024, 1, 1, 12, 0, 0)
        config = GitHubConfig(token_expires_at=dt)
        d = config.to_dict()
        assert "token_expires_at" in d

    def test_from_env_with_token(self, monkeypatch, tmp_path):
        """from_env with GITHUB_TOKEN env var."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_env_token")
        monkeypatch.setenv("HOME", str(tmp_path))
        config = GitHubConfig.from_env()
        assert config.token == "ghp_test_env_token"
        assert config._token_source == "environment"

    def test_from_env_no_token(self, monkeypatch, tmp_path):
        """from_env with no token available."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_INCOGNITO_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        config = GitHubConfig.from_env()
        assert config.token == ""

    def test_health_check_no_token(self):
        """Health check fails without token."""
        config = GitHubConfig(token="")
        result = config.health_check()
        assert not result.healthy


class TestLLMConfig:
    """Tests for LLMConfig class."""

    def test_defaults(self):
        """Default configuration."""
        config = LLMConfig()
        assert config.anthropic_api_key == ""
        assert config.timeout == 7200

    def test_validate_api_key_method(self, monkeypatch):
        """Validate with api_key method."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="")
        result = config.validate()
        assert not result.is_valid

    def test_validate_oauth_method(self, monkeypatch):
        """Validate with oauth method (no key needed)."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        config = LLMConfig(anthropic_api_key="")
        result = config.validate()
        assert result.is_valid
        assert any("OAuth" in w or "oauth" in w.lower() for w in result.warnings)

    def test_validate_custom_base_url_warning(self, monkeypatch):
        """Custom base URL generates warning."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        config = LLMConfig(anthropic_base_url="https://custom.api.com")
        result = config.validate()
        assert any("base URL" in w for w in result.warnings)

    def test_to_dict(self):
        """to_dict masks API key."""
        config = LLMConfig(
            anthropic_api_key="sk-ant-secret",
            timeout=3600,
        )
        d = config.to_dict()
        assert "sk-ant-secret" not in d["anthropic_api_key"]
        assert d["timeout"] == 3600

    def test_from_env(self, monkeypatch):
        """from_env loads from environment."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://custom.com")
        monkeypatch.setenv("LLM_MODEL", "claude-3-sonnet")
        monkeypatch.setenv("LLM_TIMEOUT", "3600")
        config = LLMConfig.from_env()
        assert config.anthropic_api_key == "sk-ant-test"
        assert config.anthropic_base_url == "https://custom.com"
        assert config.model == "claude-3-sonnet"
        assert config.timeout == 3600

    def test_from_env_defaults(self, monkeypatch):
        """from_env with defaults."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("LLM_TIMEOUT", raising=False)
        config = LLMConfig.from_env()
        assert config.anthropic_api_key == ""
        assert config.timeout == 7200

    def test_from_env_invalid_timeout(self, monkeypatch):
        """from_env with invalid timeout falls back to default."""
        monkeypatch.setenv("LLM_TIMEOUT", "not_a_number")
        config = LLMConfig.from_env()
        assert config.timeout == 7200

    def test_health_check_no_key(self, monkeypatch):
        """Health check fails without API key (api_key method)."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="")
        result = config.health_check()
        assert not result.healthy

    def test_health_check_oauth_skips(self, monkeypatch):
        """Health check with OAuth mode is skipped."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        config = LLMConfig(anthropic_api_key="")
        result = config.health_check()
        assert result.healthy  # Skipped = healthy

    def test_validate_valid_api_key(self, monkeypatch):
        """Valid API key passes validation."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="sk-ant-api03-" + "a" * 90)
        result = config.validate()
        assert result.is_valid

    def test_validate_invalid_api_key_format(self, monkeypatch):
        """Invalid API key format fails validation."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="bad-key")
        result = config.validate()
        assert not result.is_valid

    def test_validate_unknown_auth_method(self, monkeypatch):
        """Unknown auth method generates warning."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "unknown")
        config = LLMConfig(anthropic_api_key="")
        result = config.validate()
        assert any("Unknown auth method" in w for w in result.warnings)


class TestGatewayConfigEnsureSecret:
    """Tests for GatewayConfig.ensure_secret_persisted."""

    def test_ensure_secret_persisted_new(self, monkeypatch, tmp_path):
        """Save secret to new file."""
        monkeypatch.setenv("HOME", str(tmp_path))
        config = GatewayConfig(secret="my-secret")
        result = config.ensure_secret_persisted()
        assert result is True

        secret_file = tmp_path / ".config" / "egg" / "launcher-secret"
        assert secret_file.read_text() == "my-secret"

    def test_ensure_secret_persisted_existing_match(self, monkeypatch, tmp_path):
        """No-op when file already has matching secret."""
        monkeypatch.setenv("HOME", str(tmp_path))
        secret_dir = tmp_path / ".config" / "egg"
        secret_dir.mkdir(parents=True)
        (secret_dir / "launcher-secret").write_text("my-secret")

        config = GatewayConfig(secret="my-secret")
        result = config.ensure_secret_persisted()
        assert result is True

    def test_ensure_secret_persisted_overwrite(self, monkeypatch, tmp_path):
        """Overwrite file with different secret."""
        monkeypatch.setenv("HOME", str(tmp_path))
        secret_dir = tmp_path / ".config" / "egg"
        secret_dir.mkdir(parents=True)
        (secret_dir / "launcher-secret").write_text("old-secret")

        config = GatewayConfig(secret="new-secret")
        result = config.ensure_secret_persisted()
        assert result is True
        assert (secret_dir / "launcher-secret").read_text() == "new-secret"


class TestGitHubConfigValidation:
    """Additional validation tests for GitHubConfig."""

    def test_validate_invalid_token_format(self):
        """Invalid token format fails."""
        config = GitHubConfig(token="not-a-github-token")
        result = config.validate()
        assert not result.is_valid

    def test_validate_readonly_token_warning(self):
        """Invalid readonly token generates warning."""
        config = GitHubConfig(
            token="ghp_" + "a" * 36,
            readonly_token="bad-format",
        )
        result = config.validate()
        assert any("readonly_token" in w for w in result.warnings)

    def test_validate_incognito_token_warning(self):
        """Invalid incognito token generates warning."""
        config = GitHubConfig(
            token="ghp_" + "a" * 36,
            incognito_token="bad-format",
        )
        result = config.validate()
        assert any("incognito_token" in w for w in result.warnings)

    def test_from_env_readonly_token(self, monkeypatch, tmp_path):
        """from_env loads readonly token."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
        monkeypatch.setenv("GITHUB_READONLY_TOKEN", "ghp_readonly")
        monkeypatch.setenv("HOME", str(tmp_path))
        config = GitHubConfig.from_env()
        assert config.readonly_token == "ghp_readonly"

    def test_from_env_incognito_token(self, monkeypatch, tmp_path):
        """from_env loads incognito token."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
        monkeypatch.setenv("GITHUB_INCOGNITO_TOKEN", "ghp_incog")
        monkeypatch.setenv("HOME", str(tmp_path))
        config = GitHubConfig.from_env()
        assert config.incognito_token == "ghp_incog"
