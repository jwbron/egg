"""Tests for anthropic_credentials module.

Covers credential loading, precedence, and the get_api_key_credential()
method added to fix issue #1686 (OAuth tokens rejected by Messages API).
"""

from pathlib import Path

from anthropic_credentials import AnthropicCredentialsManager, parse_env_file


def _make_manager(tmp_path: Path, secrets_content: str) -> AnthropicCredentialsManager:
    """Create a credentials manager backed by a temp secrets file."""
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text(secrets_content)
    return AnthropicCredentialsManager(secrets_path=secrets_file)


# ---------------------------------------------------------------------------
# get_credential() — existing behaviour (OAuth takes precedence)
# ---------------------------------------------------------------------------


class TestGetCredential:
    def test_api_key_only(self, tmp_path):
        mgr = _make_manager(tmp_path, 'ANTHROPIC_API_KEY="sk-ant-' + "x" * 80 + '"')
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_api_key
        assert cred.header_name == "x-api-key"
        assert cred.header_value.startswith("sk-ant-")

    def test_oauth_only(self, tmp_path):
        mgr = _make_manager(tmp_path, 'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"')
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_oauth
        assert cred.header_name == "Authorization"
        assert cred.header_value.startswith("Bearer ")

    def test_both_configured_prefers_oauth(self, tmp_path):
        mgr = _make_manager(
            tmp_path,
            'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"\n'
            'ANTHROPIC_API_KEY="sk-ant-' + "k" * 80 + '"\n',
        )
        cred = mgr.get_credential()
        assert cred is not None
        assert cred.is_oauth, "get_credential() should prefer OAuth when both present"

    def test_no_credentials(self, tmp_path):
        mgr = _make_manager(tmp_path, "# empty\n")
        assert mgr.get_credential() is None

    def test_missing_file(self):
        mgr = AnthropicCredentialsManager(secrets_path=Path("/nonexistent/secrets.env"))
        assert mgr.get_credential() is None


# ---------------------------------------------------------------------------
# get_api_key_credential() — new method (issue #1686)
# ---------------------------------------------------------------------------


class TestGetApiKeyCredential:
    def test_api_key_only(self, tmp_path):
        mgr = _make_manager(tmp_path, 'ANTHROPIC_API_KEY="sk-ant-' + "x" * 80 + '"')
        cred = mgr.get_api_key_credential()
        assert cred is not None
        assert cred.is_api_key
        assert cred.header_name == "x-api-key"

    def test_oauth_only_returns_none(self, tmp_path):
        """When only OAuth is configured, get_api_key_credential returns None."""
        mgr = _make_manager(tmp_path, 'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"')
        cred = mgr.get_api_key_credential()
        assert cred is None

    def test_both_configured_returns_api_key(self, tmp_path):
        """Core fix for #1686: when both are configured, proxy gets the API key."""
        api_key = "sk-ant-" + "k" * 80
        mgr = _make_manager(
            tmp_path,
            'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + f'"\nANTHROPIC_API_KEY="{api_key}"\n',
        )
        cred = mgr.get_api_key_credential()
        assert cred is not None
        assert cred.is_api_key
        assert cred.header_value == api_key

    def test_both_configured_get_credential_still_returns_oauth(self, tmp_path):
        """Ensure get_credential() behavior is unchanged — still prefers OAuth."""
        mgr = _make_manager(
            tmp_path,
            'CLAUDE_CODE_OAUTH_TOKEN="' + "t" * 30 + '"\n'
            'ANTHROPIC_API_KEY="sk-ant-' + "k" * 80 + '"\n',
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
        mgr = AnthropicCredentialsManager(secrets_path=Path("/nonexistent/secrets.env"))
        assert mgr.get_api_key_credential() is None

    def test_api_key_too_short_returns_none(self, tmp_path):
        mgr = _make_manager(tmp_path, 'ANTHROPIC_API_KEY="sk-ant-short"')
        assert mgr.get_api_key_credential() is None


# ---------------------------------------------------------------------------
# parse_env_file
# ---------------------------------------------------------------------------


class TestParseEnvFile:
    def test_basic_parsing(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text("KEY1=value1\nKEY2=\"value2\"\nKEY3='value3'\n# comment\n")

        result = parse_env_file(env_file)
        assert result == {"KEY1": "value1", "KEY2": "value2", "KEY3": "value3"}

    def test_nonexistent_file(self):
        result = parse_env_file(Path("/nonexistent/file.env"))
        assert result == {}
