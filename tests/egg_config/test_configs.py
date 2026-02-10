"""Tests for egg_config service configs: github.py, gateway.py, llm.py."""

import json
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from egg_config.configs.gateway import GatewayConfig, RateLimitConfig
from egg_config.configs.github import GitHubConfig, _get_github_username
from egg_config.configs.llm import LLMConfig

# ===========================================================================
# GitHubConfig Tests
# ===========================================================================


class TestGitHubConfigValidate:
    """Tests for GitHubConfig.validate()."""

    def test_valid_token(self):
        """Valid config passes validation."""
        config = GitHubConfig(token="ghp_testtoken123456789012345678901234")
        result = config.validate()
        assert result.is_valid

    def test_empty_token(self):
        """Empty token fails validation."""
        config = GitHubConfig(token="")
        result = config.validate()
        assert not result.is_valid

    def test_expired_token(self):
        """Expired token produces error."""
        config = GitHubConfig(
            token="ghp_testtoken123456789012345678901234",
            token_expires_at=datetime.now() - timedelta(hours=1),
        )
        result = config.validate()
        assert not result.is_valid
        assert any("expired" in e for e in result.errors)

    def test_expiring_soon_token(self):
        """Token expiring soon produces warning."""
        config = GitHubConfig(
            token="ghp_testtoken123456789012345678901234",
            token_expires_at=datetime.now() + timedelta(minutes=2),
        )
        result = config.validate()
        assert result.is_valid
        assert len(result.warnings) > 0

    def test_invalid_readonly_token_warns(self):
        """Invalid readonly token produces warning, not error."""
        config = GitHubConfig(
            token="ghp_testtoken123456789012345678901234",
            readonly_token="invalid",
        )
        result = config.validate()
        assert result.is_valid  # Warnings only for optional tokens
        assert any("readonly_token" in w for w in result.warnings)

    def test_invalid_user_mode_token_warns(self):
        """Invalid user_mode_token produces warning, not error."""
        config = GitHubConfig(
            token="ghp_testtoken123456789012345678901234",
            user_mode_token="invalid",
        )
        result = config.validate()
        assert result.is_valid


class TestGitHubConfigHealthCheck:
    """Tests for GitHubConfig.health_check()."""

    def test_no_token(self):
        """Returns unhealthy when no token configured."""
        config = GitHubConfig(token="")
        result = config.health_check()
        assert not result.healthy
        assert "not configured" in result.message

    def test_success(self):
        """Returns healthy on successful API call."""
        config = GitHubConfig(token="ghp_test123")
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"login": "testuser"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = config.health_check()
            assert result.healthy
            assert "testuser" in result.message

    def test_401_error(self):
        """Returns unhealthy on 401."""
        config = GitHubConfig(token="ghp_invalid")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 401, "Unauthorized", {}, None),
        ):
            result = config.health_check()
            assert not result.healthy
            assert "invalid" in result.message.lower()

    def test_500_error(self):
        """Returns unhealthy on server error."""
        config = GitHubConfig(token="ghp_test")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "Server Error", {}, None),
        ):
            result = config.health_check()
            assert not result.healthy
            assert "500" in result.message

    def test_connection_error(self):
        """Returns unhealthy on connection failure."""
        config = GitHubConfig(token="ghp_test")
        with patch("urllib.request.urlopen", side_effect=ConnectionError("timeout")):
            result = config.health_check()
            assert not result.healthy


class TestGitHubConfigToDict:
    """Tests for GitHubConfig.to_dict()."""

    def test_masks_secrets(self):
        """Masks token values in dict output."""
        config = GitHubConfig(
            token="ghp_verylongtokenthatshouldbmasked",
            readonly_token="ghp_readonly123456789012345678",
        )
        d = config.to_dict()
        assert d["token"] != "ghp_verylongtokenthatshouldbmasked"
        assert d["username"] == "james-in-a-box"

    def test_includes_expiry(self):
        """Includes expiry when set."""
        expires = datetime(2024, 6, 1)
        config = GitHubConfig(
            token="ghp_test",
            token_expires_at=expires,
        )
        d = config.to_dict()
        assert "token_expires_at" in d

    def test_includes_source(self):
        """Includes token source when set."""
        config = GitHubConfig(token="ghp_test", _token_source="environment")
        d = config.to_dict()
        assert d["_token_source"] == "environment"


class TestGitHubConfigProperties:
    """Tests for GitHubConfig properties."""

    def test_is_token_expired_false(self):
        """Token not expired when expiry in future."""
        config = GitHubConfig(token_expires_at=datetime.now() + timedelta(hours=1))
        assert not config.is_token_expired

    def test_is_token_expired_true(self):
        """Token expired when expiry in past."""
        config = GitHubConfig(token_expires_at=datetime.now() - timedelta(hours=1))
        assert config.is_token_expired

    def test_is_token_expired_no_expiry(self):
        """Token not expired when no expiry set."""
        config = GitHubConfig()
        assert not config.is_token_expired

    def test_token_expires_soon_true(self):
        """Token expires soon within 5 minutes."""
        config = GitHubConfig(token_expires_at=datetime.now() + timedelta(minutes=2))
        assert config.token_expires_soon

    def test_token_expires_soon_false(self):
        """Token not expiring soon when > 5 minutes."""
        config = GitHubConfig(token_expires_at=datetime.now() + timedelta(hours=1))
        assert not config.token_expires_soon

    def test_token_expires_soon_no_expiry(self):
        """Not expiring soon when no expiry set."""
        config = GitHubConfig()
        assert not config.token_expires_soon


class TestGitHubConfigFromEnv:
    """Tests for GitHubConfig.from_env()."""

    def test_from_env_variable(self, monkeypatch):
        """Loads token from GITHUB_TOKEN env var."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        with patch("egg_config.configs.github._get_github_username", return_value="testuser"):
            config = GitHubConfig.from_env()
            assert config.token == "ghp_envtoken"
            assert config._token_source == "environment"

    def test_from_secrets_env(self, monkeypatch, tmp_path):
        """Loads token from secrets.env file."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("GITHUB_TOKEN=ghp_fromfile\n")
        with patch("egg_config.configs.github.Path.home", return_value=tmp_path):
            config_dir = tmp_path / ".config" / "egg"
            config_dir.mkdir(parents=True)
            sf = config_dir / "secrets.env"
            sf.write_text("GITHUB_TOKEN=ghp_fromfile\n")
            with patch("egg_config.configs.github._get_github_username", return_value="testuser"):
                config = GitHubConfig.from_env()
                assert config.token == "ghp_fromfile"
                assert config._token_source == "secrets.env"

    def test_from_token_file(self, monkeypatch, tmp_path):
        """Loads token from github-token file."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_READONLY_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        config_dir = tmp_path / ".config" / "egg"
        config_dir.mkdir(parents=True)
        (config_dir / "github-token").write_text("ghp_fromtokenfile\n")
        with patch("egg_config.configs.github.Path.home", return_value=tmp_path):
            with patch("egg_config.configs.github._get_github_username", return_value="testuser"):
                config = GitHubConfig.from_env()
                assert config.token == "ghp_fromtokenfile"
                assert config._token_source == "github-token file"


class TestGetGithubUsername:
    """Tests for _get_github_username helper."""

    def test_returns_username_from_config(self, tmp_path):
        """Returns username from repositories.yaml."""
        config_dir = tmp_path / ".config" / "egg"
        config_dir.mkdir(parents=True)
        (config_dir / "repositories.yaml").write_text("github_username: myuser\n")
        with patch("egg_config.configs.github.Path.home", return_value=tmp_path):
            assert _get_github_username() == "myuser"

    def test_returns_default(self, tmp_path):
        """Returns 'james-in-a-box' when config not found."""
        with patch("egg_config.configs.github.Path.home", return_value=tmp_path):
            assert _get_github_username() == "james-in-a-box"


# ===========================================================================
# GatewayConfig Tests
# ===========================================================================


class TestRateLimitConfig:
    """Tests for RateLimitConfig."""

    def test_defaults(self):
        """Default rate limits are set."""
        config = RateLimitConfig()
        assert config.git_push == 1000
        assert config.combined == 4000


class TestGatewayConfigValidate:
    """Tests for GatewayConfig.validate()."""

    def test_valid_config(self):
        """Valid config passes validation."""
        config = GatewayConfig(
            secret="a" * 32,
            port=9848,
        )
        result = config.validate()
        assert result.is_valid

    def test_empty_secret(self):
        """Empty secret fails validation."""
        config = GatewayConfig(secret="")
        result = config.validate()
        assert not result.is_valid

    def test_short_secret_warns(self):
        """Short secret produces warning."""
        config = GatewayConfig(secret="short")
        result = config.validate()
        assert result.is_valid
        assert any("shorter" in w for w in result.warnings)

    def test_invalid_port(self):
        """Invalid port fails validation."""
        config = GatewayConfig(secret="a" * 32, port=-1)
        result = config.validate()
        assert not result.is_valid

    def test_warns_all_interfaces(self):
        """Warns when bound to 0.0.0.0."""
        config = GatewayConfig(secret="a" * 32, host="0.0.0.0")
        result = config.validate()
        assert any("0.0.0.0" in w for w in result.warnings)


class TestGatewayConfigHealthCheck:
    """Tests for GatewayConfig.health_check()."""

    def test_no_secret(self):
        """Returns unhealthy when no secret."""
        config = GatewayConfig(secret="")
        result = config.health_check()
        assert not result.healthy
        assert "not configured" in result.message

    def test_success(self):
        """Returns healthy on successful check."""
        config = GatewayConfig(secret="test-secret", port=9848)
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = config.health_check()
            assert result.healthy

    def test_401_error(self):
        """Returns unhealthy on 401."""
        config = GatewayConfig(secret="bad-secret")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 401, "Unauthorized", {}, None),
        ):
            result = config.health_check()
            assert not result.healthy
            assert "Invalid" in result.message

    def test_url_error(self):
        """Returns unhealthy when gateway not reachable."""
        config = GatewayConfig(secret="secret")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            result = config.health_check()
            assert not result.healthy
            assert "not reachable" in result.message

    def test_generic_error(self):
        """Returns unhealthy on generic error."""
        config = GatewayConfig(secret="secret")
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            result = config.health_check()
            assert not result.healthy


class TestGatewayConfigToDict:
    """Tests for GatewayConfig.to_dict()."""

    def test_masks_secret(self):
        """Secret is masked in dict output."""
        config = GatewayConfig(secret="super-secret-value-that-is-long")
        d = config.to_dict()
        assert d["secret"] != "super-secret-value-that-is-long"
        assert d["host"] == "0.0.0.0"
        assert d["port"] == 9848
        assert "rate_limits" in d

    def test_includes_rate_limits(self):
        """Includes all rate limit values."""
        config = GatewayConfig(secret="s")
        d = config.to_dict()
        assert d["rate_limits"]["git_push"] == 1000


class TestGatewayConfigFromEnv:
    """Tests for GatewayConfig.from_env()."""

    def test_from_env_variable(self, monkeypatch):
        """Loads secret from EGG_LAUNCHER_SECRET env var."""
        monkeypatch.setenv("EGG_LAUNCHER_SECRET", "env-secret-value")
        config = GatewayConfig.from_env()
        assert config.secret == "env-secret-value"
        assert config._secret_source == "environment"

    def test_custom_port(self, monkeypatch):
        """Loads custom port from env."""
        monkeypatch.setenv("EGG_LAUNCHER_SECRET", "secret")
        monkeypatch.setenv("GATEWAY_PORT", "1234")
        config = GatewayConfig.from_env()
        assert config.port == 1234

    def test_invalid_port_defaults(self, monkeypatch):
        """Invalid port defaults to 9848."""
        monkeypatch.setenv("EGG_LAUNCHER_SECRET", "secret")
        monkeypatch.setenv("GATEWAY_PORT", "not-a-number")
        config = GatewayConfig.from_env()
        assert config.port == 9848

    def test_from_file(self, monkeypatch, tmp_path):
        """Loads secret from launcher-secret file."""
        monkeypatch.delenv("EGG_LAUNCHER_SECRET", raising=False)
        config_dir = tmp_path / ".config" / "egg"
        config_dir.mkdir(parents=True)
        (config_dir / "launcher-secret").write_text("file-secret")
        with patch("egg_config.configs.gateway.Path.home", return_value=tmp_path):
            config = GatewayConfig.from_env()
            assert config.secret == "file-secret"
            assert config._secret_source == "launcher-secret file"

    def test_auto_generates(self, monkeypatch, tmp_path):
        """Auto-generates secret when nothing configured."""
        monkeypatch.delenv("EGG_LAUNCHER_SECRET", raising=False)
        with patch("egg_config.configs.gateway.Path.home", return_value=tmp_path):
            config = GatewayConfig.from_env()
            assert config.secret  # Non-empty
            assert config._secret_source == "auto-generated"


class TestGatewayConfigEnsureSecretPersisted:
    """Tests for GatewayConfig.ensure_secret_persisted()."""

    def test_saves_to_file(self, tmp_path):
        """Saves secret to disk."""
        config = GatewayConfig(secret="my-secret")
        config_dir = tmp_path / ".config" / "egg"
        with patch("egg_config.configs.gateway.Path.home", return_value=tmp_path):
            result = config.ensure_secret_persisted()
            assert result is True
            assert (config_dir / "launcher-secret").read_text() == "my-secret"

    def test_already_exists_matching(self, tmp_path):
        """Returns True when matching secret already exists."""
        config_dir = tmp_path / ".config" / "egg"
        config_dir.mkdir(parents=True)
        (config_dir / "launcher-secret").write_text("my-secret")
        config = GatewayConfig(secret="my-secret")
        with patch("egg_config.configs.gateway.Path.home", return_value=tmp_path):
            assert config.ensure_secret_persisted() is True


# ===========================================================================
# LLMConfig Tests
# ===========================================================================


class TestLLMConfigValidate:
    """Tests for LLMConfig.validate()."""

    def test_valid_api_key(self, monkeypatch):
        """Valid API key passes validation."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(
            anthropic_api_key="sk-ant-api03-testkey123456789012345678901234567890123456"
        )
        result = config.validate()
        assert result.is_valid

    def test_empty_api_key(self, monkeypatch):
        """Empty API key fails validation in api_key mode."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="")
        result = config.validate()
        assert not result.is_valid

    def test_oauth_mode_no_key(self, monkeypatch):
        """OAuth mode passes without API key."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        config = LLMConfig(anthropic_api_key="")
        result = config.validate()
        assert result.is_valid
        assert any("OAuth" in w for w in result.warnings)

    def test_custom_base_url_warns(self, monkeypatch):
        """Custom base URL produces warning."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        config = LLMConfig(anthropic_base_url="https://custom.api.com")
        result = config.validate()
        assert any("custom" in w.lower() for w in result.warnings)

    def test_unknown_auth_method(self, monkeypatch):
        """Unknown auth method produces warning."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "unknown")
        config = LLMConfig()
        result = config.validate()
        assert any("Unknown" in w for w in result.warnings)


class TestLLMConfigHealthCheck:
    """Tests for LLMConfig.health_check()."""

    def test_no_key_api_mode(self, monkeypatch):
        """Returns unhealthy when no API key in api_key mode."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="")
        result = config.health_check()
        assert not result.healthy

    def test_no_key_oauth_mode(self, monkeypatch):
        """Returns healthy in OAuth mode without key."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")
        config = LLMConfig(anthropic_api_key="")
        result = config.health_check()
        assert result.healthy
        assert "OAuth" in result.message

    def test_success(self, monkeypatch):
        """Returns healthy on successful API call."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="sk-ant-api03-test")
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = config.health_check()
            assert result.healthy

    def test_401_error(self, monkeypatch):
        """Returns unhealthy on 401."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="sk-ant-api03-invalid")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 401, "Unauthorized", {}, None),
        ):
            result = config.health_check()
            assert not result.healthy

    def test_400_error_still_healthy(self, monkeypatch):
        """400 is still healthy (auth worked, request was bad)."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="sk-ant-api03-test")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 400, "Bad Request", {}, None),
        ):
            result = config.health_check()
            assert result.healthy

    def test_server_error(self, monkeypatch):
        """Returns unhealthy on 500."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="sk-ant-api03-test")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "Server Error", {}, None),
        ):
            result = config.health_check()
            assert not result.healthy

    def test_connection_failure(self, monkeypatch):
        """Returns unhealthy on connection failure."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(anthropic_api_key="sk-ant-api03-test")
        with patch("urllib.request.urlopen", side_effect=ConnectionError("down")):
            result = config.health_check()
            assert not result.healthy

    def test_custom_base_url(self, monkeypatch):
        """Uses custom base URL when set."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "api_key")
        config = LLMConfig(
            anthropic_api_key="sk-ant-api03-test",
            anthropic_base_url="https://custom.api.com",
        )
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            config.health_check()
            call_args = mock_urlopen.call_args[0][0]
            assert "custom.api.com" in call_args.full_url


class TestLLMConfigToDict:
    """Tests for LLMConfig.to_dict()."""

    def test_masks_api_key(self):
        """API key is masked."""
        config = LLMConfig(anthropic_api_key="sk-ant-api03-longkey123")
        d = config.to_dict()
        assert d["anthropic_api_key"] != "sk-ant-api03-longkey123"

    def test_default_base_url(self):
        """Default base URL shown as [default]."""
        config = LLMConfig()
        d = config.to_dict()
        assert d["anthropic_base_url"] == "[default]"

    def test_default_model(self):
        """Default model shown as [provider default]."""
        config = LLMConfig()
        d = config.to_dict()
        assert d["model"] == "[provider default]"


class TestLLMConfigFromEnv:
    """Tests for LLMConfig.from_env()."""

    def test_loads_from_env(self, monkeypatch):
        """Loads config from environment variables."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://custom.com")
        monkeypatch.setenv("LLM_MODEL", "claude-3-opus")
        monkeypatch.setenv("LLM_TIMEOUT", "3600")
        config = LLMConfig.from_env()
        assert config.anthropic_api_key == "sk-ant-api03-test"
        assert config.anthropic_base_url == "https://custom.com"
        assert config.model == "claude-3-opus"
        assert config.timeout == 3600

    def test_invalid_timeout_defaults(self, monkeypatch):
        """Invalid timeout defaults to 7200."""
        monkeypatch.setenv("LLM_TIMEOUT", "not-a-number")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        config = LLMConfig.from_env()
        assert config.timeout == 7200

    def test_empty_env(self, monkeypatch):
        """Loads defaults when env vars are empty."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("LLM_TIMEOUT", raising=False)
        config = LLMConfig.from_env()
        assert config.anthropic_api_key == ""
        assert config.anthropic_base_url == ""
        assert config.model == ""
        assert config.timeout == 7200
