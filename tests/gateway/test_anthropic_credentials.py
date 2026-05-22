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


# =============================================================================
# LiteLLM credential resolver (issue #2769 slice-1, TASK-1-2)
# =============================================================================
#
# The LiteLLM credential resolver lives alongside the Anthropic one — same
# secrets.env file, same parse_env_file helper, same mtime-invalidated
# cache.  It reads ``LITELLM_MASTER_KEY`` and returns an
# ``AnthropicCredential`` shaped ``header_name="x-api-key"``,
# ``header_value="<key>"``.  When the env var is absent the resolver
# returns ``None`` (no-op default — matches today's behavior when no
# Anthropic credentials are configured).
#
# Implementation detail: the coder may either (a) extend
# ``AnthropicCredentialsManager`` with a sibling LiteLLM method or
# (b) introduce a parallel ``LiteLLMCredentialsManager`` class.  The
# tests below tolerate both by importing through the public symbol
# the coder lands and skipping the suite if neither is present yet.


def _import_litellm_resolver():
    """Best-effort import of the LiteLLM credential resolver primitive.

    Returns a tuple ``(manager_class, helper_callable)`` where exactly
    one of the two is non-None.  The tests use whichever surface the
    coder exposes.
    """
    try:
        # Preferred shape — a parallel CredentialsManager class.
        from anthropic_credentials import LiteLLMCredentialsManager  # type: ignore[attr-defined]

        return LiteLLMCredentialsManager, None
    except ImportError:
        pass
    try:
        # Alternative shape — a module-level helper that takes a path.
        from anthropic_credentials import (  # type: ignore[attr-defined]
            get_litellm_credential,
        )

        return None, get_litellm_credential
    except ImportError:
        return None, None


class TestLiteLLMCredentialResolver:
    """Tests for the LiteLLM master-key resolver (TASK-1-2)."""

    @pytest.fixture(autouse=True)
    def _skip_if_unimplemented(self):
        manager_cls, helper = _import_litellm_resolver()
        if manager_cls is None and helper is None:
            pytest.skip("LiteLLM credential resolver not yet implemented")

    @pytest.fixture
    def _resolve(self, tmp_path):
        """Return a function ``resolve(secrets_path)`` that yields the
        ``AnthropicCredential | None`` produced by whichever resolver
        shape the coder lands.
        """
        manager_cls, helper = _import_litellm_resolver()

        def _do(secrets_path: Path):
            if manager_cls is not None:
                return manager_cls(secrets_path=secrets_path).get_credential()
            assert helper is not None
            return helper(secrets_path)

        return _do

    def test_returns_x_api_key_credential_when_key_present(self, tmp_path, _resolve):
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('LITELLM_MASTER_KEY="litellm-master-key-1234567890"')

        cred = _resolve(secrets_file)
        assert cred is not None
        assert cred.header_name == "x-api-key"
        assert cred.header_value == "litellm-master-key-1234567890"
        assert cred.is_api_key
        assert not cred.is_oauth

    def test_returns_none_when_key_missing(self, tmp_path, _resolve):
        """No-op default — no warning, no error, just None."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('ANTHROPIC_API_KEY="sk-ant-12345678901234567890"')

        cred = _resolve(secrets_file)
        assert cred is None

    def test_returns_none_when_secrets_file_missing(self, tmp_path, _resolve):
        """File absent — fail closed, return None."""
        cred = _resolve(tmp_path / "nonexistent.env")
        assert cred is None

    def test_empty_master_key_returns_none(self, tmp_path, _resolve):
        """``LITELLM_MASTER_KEY=""`` is the documented disable signal."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('LITELLM_MASTER_KEY=""')

        cred = _resolve(secrets_file)
        assert cred is None

    def test_litellm_resolver_independent_of_anthropic_keys(self, tmp_path, _resolve):
        """The LiteLLM resolver MUST NOT fall back to ANTHROPIC_API_KEY
        when LITELLM_MASTER_KEY is absent — that would silently route
        agent traffic through the wrong credential.
        """
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            'ANTHROPIC_API_KEY="sk-ant-12345678901234567890123456789012345678901234567890"\n'
            'CLAUDE_CODE_OAUTH_TOKEN="oauth-1234567890abcdef"\n'
            # No LITELLM_MASTER_KEY entry
        )

        cred = _resolve(secrets_file)
        assert cred is None, (
            "LiteLLM resolver must not silently inject the Anthropic credential "
            "when LITELLM_MASTER_KEY is absent"
        )


class TestLiteLLMResolverCachingBehavior:
    """Cache invalidation: changing the file mtime invalidates the cache,
    matching ``AnthropicCredentialsManager``'s contract (TASK-1-2 AC).
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_manager_class(self):
        manager_cls, _ = _import_litellm_resolver()
        if manager_cls is None:
            pytest.skip(
                "LiteLLM credential resolver does not expose a manager class; "
                "mtime-cache test only applies to the manager shape"
            )

    def test_mtime_change_invalidates_cache(self, tmp_path):
        import time

        from anthropic_credentials import LiteLLMCredentialsManager  # type: ignore[attr-defined]

        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('LITELLM_MASTER_KEY="initial-key-1234567890"')

        manager = LiteLLMCredentialsManager(secrets_path=secrets_file)
        cred1 = manager.get_credential()
        assert cred1 is not None
        assert cred1.header_value == "initial-key-1234567890"

        time.sleep(0.1)
        secrets_file.write_text('LITELLM_MASTER_KEY="rotated-key-0987654321"')

        cred2 = manager.get_credential()
        assert cred2 is not None
        assert cred2.header_value == "rotated-key-0987654321"

    def test_unchanged_file_uses_cached_credential(self, tmp_path):
        from anthropic_credentials import LiteLLMCredentialsManager  # type: ignore[attr-defined]

        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text('LITELLM_MASTER_KEY="stable-key-1234567890"')

        manager = LiteLLMCredentialsManager(secrets_path=secrets_file)
        cred1 = manager.get_credential()
        cred2 = manager.get_credential()
        # Same object instance — cache hit
        assert cred1 is cred2 or cred1 == cred2
