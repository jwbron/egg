"""Tests for anthropic_credentials module.

Covers credential loading, precedence, and the get_api_key_credential()
method added to fix issue #1686 (OAuth tokens rejected by Messages API).
"""

import tempfile
from pathlib import Path

import pytest


def _make_manager(secrets_content: str):
    """Create a credentials manager with a temp secrets file."""
    from anthropic_credentials import AnthropicCredentialsManager

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
    tmp.write(secrets_content)
    tmp.flush()
    return AnthropicCredentialsManager(secrets_path=Path(tmp.name))


# ---------------------------------------------------------------------------
# get_credential() — existing behaviour (OAuth takes precedence)
# ---------------------------------------------------------------------------


class TestGetCredential:
    def test_api_key_only(self):
        mgr = _make_manager('ANTHROPIC_API_KEY="sk-ant-' + "x" * 80 + '"')
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_api_key
        assert cred.header_name == "x-api-key"
        assert cred.header_value.startswith("sk-ant-")

    def test_oauth_only(self):
        mgr = _make_manager('CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"')
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_oauth
        assert cred.header_name == "Authorization"
        assert cred.header_value.startswith("Bearer ")

    def test_both_configured_prefers_oauth(self):
        mgr = _make_manager(
            'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"\n'
            'ANTHROPIC_API_KEY="sk-ant-' + "k" * 80 + '"\n'
        )
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_oauth, "get_credential() should prefer OAuth when both present"

    def test_no_credentials(self):
        mgr = _make_manager("# empty\n")
        assert mgr.get_credential() is None

    def test_missing_file(self):
        from anthropic_credentials import AnthropicCredentialsManager

        mgr = AnthropicCredentialsManager(secrets_path=Path("/nonexistent/secrets.env"))
        assert mgr.get_credential() is None


# ---------------------------------------------------------------------------
# get_api_key_credential() — new method (issue #1686)
# ---------------------------------------------------------------------------


class TestGetApiKeyCredential:
    def test_api_key_only(self):
        mgr = _make_manager('ANTHROPIC_API_KEY="sk-ant-' + "x" * 80 + '"')
        cred = mgr.get_api_key_credential()
        assert cred is not None
        assert cred.is_api_key
        assert cred.header_name == "x-api-key"

    def test_oauth_only_returns_none(self):
        """When only OAuth is configured, get_api_key_credential returns None."""
        mgr = _make_manager('CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"')
        cred = mgr.get_api_key_credential()
        assert cred is None

    def test_both_configured_returns_api_key(self):
        """Core fix for #1686: when both are configured, proxy gets the API key."""
        api_key = "sk-ant-" + "k" * 80
        mgr = _make_manager(
            'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"\n'
            f'ANTHROPIC_API_KEY="{api_key}"\n'
        )
        cred = mgr.get_api_key_credential()
        assert cred is not None
        assert cred.is_api_key
        assert cred.header_value == api_key

    def test_both_configured_get_credential_still_returns_oauth(self):
        """Ensure get_credential() behavior is unchanged — still prefers OAuth."""
        mgr = _make_manager(
            'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"\n'
            'ANTHROPIC_API_KEY="sk-ant-' + "k" * 80 + '"\n'
        )
        # get_credential still returns OAuth
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_oauth

        # get_api_key_credential returns API key
        api_cred = mgr.get_api_key_credential()
        assert api_cred is not None
        assert api_cred.is_api_key

    def test_missing_file(self):
        from anthropic_credentials import AnthropicCredentialsManager

        mgr = AnthropicCredentialsManager(secrets_path=Path("/nonexistent/secrets.env"))
        assert mgr.get_api_key_credential() is None

    def test_api_key_too_short_returns_none(self):
        mgr = _make_manager('ANTHROPIC_API_KEY="sk-ant-short"')
        assert mgr.get_api_key_credential() is None


# ---------------------------------------------------------------------------
# parse_env_file
# ---------------------------------------------------------------------------


class TestParseEnvFile:
    def test_basic_parsing(self):
        from anthropic_credentials import parse_env_file

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
        tmp.write('KEY1=value1\nKEY2="value2"\nKEY3=\'value3\'\n# comment\n')
        tmp.flush()

        result = parse_env_file(Path(tmp.name))
        assert result == {"KEY1": "value1", "KEY2": "value2", "KEY3": "value3"}

    def test_nonexistent_file(self):
        from anthropic_credentials import parse_env_file

        result = parse_env_file(Path("/nonexistent/file.env"))
        assert result == {}
