"""Unit tests for Anthropic API proxy (Phase 2b).

Tests the Anthropic API proxy endpoints and credential management:
- POST /v1/messages (streaming and non-streaming)
- POST /v1/messages/count_tokens
- Credential injection (API key and OAuth)
- Blocked tools filtering in private mode
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# Test credential manager in isolation
class TestAnthropicCredentialsManager:
    """Tests for AnthropicCredentialsManager class."""

    def test_load_api_key(self, tmp_path):
        """Test loading an API key from secrets file."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-api03-test-key-that-is-definitely-long-enough-for-validation-12345678"
        )

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert cred.header_name == "x-api-key"
        assert cred.header_value.startswith("sk-ant-")
        assert cred.is_api_key is True
        assert cred.is_oauth is False

    def test_load_oauth_token(self, tmp_path):
        """Test loading an OAuth token from secrets file."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "ANTHROPIC_OAUTH_TOKEN=oauth-token-that-is-long-enough-for-validation"
        )

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert cred.header_name == "Authorization"
        assert cred.header_value == "Bearer oauth-token-that-is-long-enough-for-validation"
        assert cred.is_oauth is True
        assert cred.is_api_key is False

    def test_oauth_takes_precedence_over_api_key(self, tmp_path):
        """Test that OAuth token takes precedence when both are configured."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-api03-test-key-that-is-definitely-long-enough-for-validation-12345678\n"
            "ANTHROPIC_OAUTH_TOKEN=oauth-token-that-is-long-enough-for-validation"
        )

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert cred.is_oauth is True

    def test_missing_secrets_file_returns_none(self, tmp_path):
        """Test that missing secrets file returns None credential."""
        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=tmp_path / "nonexistent.env")
        cred = manager.get_credential()

        assert cred is None

    def test_empty_secrets_file_returns_none(self, tmp_path):
        """Test that empty secrets file returns None credential."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("")

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None

    def test_mtime_based_cache_invalidation(self, tmp_path):
        """Test that credentials are reloaded when file changes."""
        import time

        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-api03-original-key-that-is-definitely-long-enough-12345678"
        )

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred1 = manager.get_credential()
        assert "original" in cred1.header_value

        # Wait briefly and update file to get new mtime
        time.sleep(0.01)
        secrets_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-api03-updated-key-that-is-definitely-long-enough-12345678"
        )

        cred2 = manager.get_credential()
        assert "updated" in cred2.header_value

    def test_short_api_key_rejected(self, tmp_path):
        """Test that API keys that are too short are rejected."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("ANTHROPIC_API_KEY=sk-ant-too-short")

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None

    def test_short_oauth_token_rejected(self, tmp_path):
        """Test that OAuth tokens that are too short are rejected."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("ANTHROPIC_OAUTH_TOKEN=short")

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is None

    def test_quoted_values_handled(self, tmp_path):
        """Test that quoted values in secrets file are handled correctly."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            'ANTHROPIC_API_KEY="sk-ant-api03-quoted-key-that-is-definitely-long-enough-12345678"'
        )

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None
        assert "quoted" in cred.header_value

    def test_comments_ignored(self, tmp_path):
        """Test that comments in secrets file are ignored."""
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text(
            "# This is a comment\n"
            "ANTHROPIC_API_KEY=sk-ant-api03-test-key-that-is-definitely-long-enough-for-validation-12345678\n"
            "# Another comment"
        )

        from gateway.anthropic_credentials import AnthropicCredentialsManager

        manager = AnthropicCredentialsManager(secrets_path=secrets_file)
        cred = manager.get_credential()

        assert cred is not None


class TestParseEnvFile:
    """Tests for parse_env_file function."""

    def test_parse_simple_values(self, tmp_path):
        """Test parsing simple key=value pairs."""
        from gateway.anthropic_credentials import parse_env_file

        env_file = tmp_path / "test.env"
        env_file.write_text("KEY1=value1\nKEY2=value2")

        result = parse_env_file(env_file)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_parse_quoted_values(self, tmp_path):
        """Test parsing quoted values."""
        from gateway.anthropic_credentials import parse_env_file

        env_file = tmp_path / "test.env"
        env_file.write_text("KEY1=\"value with spaces\"\nKEY2='single quoted'")

        result = parse_env_file(env_file)
        assert result == {"KEY1": "value with spaces", "KEY2": "single quoted"}

    def test_skip_comments_and_empty_lines(self, tmp_path):
        """Test that comments and empty lines are skipped."""
        from gateway.anthropic_credentials import parse_env_file

        env_file = tmp_path / "test.env"
        env_file.write_text("# comment\n\nKEY=value\n  # indented comment")

        result = parse_env_file(env_file)
        assert result == {"KEY": "value"}


class TestBlockedToolsFiltering:
    """Tests for blocked tools filtering in private mode."""

    def test_filters_web_tools_in_private_mode(self):
        """Test that web tools are filtered in private mode."""
        from gateway.gateway import _filter_blocked_tools

        request_body = json.dumps(
            {
                "model": "claude-3-opus-20240229",
                "max_tokens": 1024,
                "tools": [
                    {"name": "bash", "description": "Run bash commands"},
                    {"name": "web_search", "description": "Search the web"},
                    {"name": "WebFetch", "description": "Fetch web pages"},
                ],
                "messages": [{"role": "user", "content": "Hello"}],
            }
        ).encode()

        result = _filter_blocked_tools(request_body, "private")
        result_json = json.loads(result)

        tool_names = [t["name"] for t in result_json["tools"]]
        assert "bash" in tool_names
        assert "web_search" not in tool_names
        assert "WebFetch" not in tool_names

    def test_does_not_filter_in_public_mode(self):
        """Test that tools are not filtered in public mode."""
        from gateway.gateway import _filter_blocked_tools

        request_body = json.dumps(
            {
                "model": "claude-3-opus-20240229",
                "tools": [
                    {"name": "web_search", "description": "Search the web"},
                ],
                "messages": [{"role": "user", "content": "Hello"}],
            }
        ).encode()

        result = _filter_blocked_tools(request_body, "public")
        result_json = json.loads(result)

        assert len(result_json["tools"]) == 1
        assert result_json["tools"][0]["name"] == "web_search"

    def test_handles_no_tools_key(self):
        """Test that requests without tools key are passed through."""
        from gateway.gateway import _filter_blocked_tools

        request_body = json.dumps(
            {"model": "claude-3-opus-20240229", "messages": [{"role": "user", "content": "Hello"}]}
        ).encode()

        result = _filter_blocked_tools(request_body, "private")
        assert result == request_body

    def test_handles_invalid_json(self):
        """Test that invalid JSON is passed through unchanged."""
        from gateway.gateway import _filter_blocked_tools

        request_body = b"not valid json"
        result = _filter_blocked_tools(request_body, "private")
        assert result == request_body


class TestStreamingDetection:
    """Tests for streaming request detection."""

    def test_detects_streaming_true(self):
        """Test that stream=true is detected."""
        from gateway.gateway import _is_streaming_request

        body = json.dumps({"stream": True}).encode()
        assert _is_streaming_request(body) is True

    def test_detects_streaming_false(self):
        """Test that stream=false is not detected as streaming."""
        from gateway.gateway import _is_streaming_request

        body = json.dumps({"stream": False}).encode()
        assert _is_streaming_request(body) is False

    def test_missing_stream_key(self):
        """Test that missing stream key defaults to False."""
        from gateway.gateway import _is_streaming_request

        body = json.dumps({"model": "claude-3-opus-20240229"}).encode()
        assert _is_streaming_request(body) is False

    def test_invalid_json(self):
        """Test that invalid JSON returns False."""
        from gateway.gateway import _is_streaming_request

        assert _is_streaming_request(b"not json") is False


class TestHeaderFiltering:
    """Tests for header filtering functions."""

    def test_blocks_auth_headers(self):
        """Test that auth headers are blocked from forwarding."""
        from gateway.gateway import _get_forwarded_headers

        # Simulate Flask request headers as list of tuples
        headers = [
            ("Content-Type", "application/json"),
            ("Authorization", "Bearer secret"),
            ("x-api-key", "sk-ant-secret"),
            ("Host", "api.anthropic.com"),
            ("X-Custom-Header", "custom-value"),
        ]

        result = _get_forwarded_headers(headers)

        assert "Content-Type" in result
        assert "X-Custom-Header" in result
        assert "Authorization" not in result
        assert "x-api-key" not in result
        assert "Host" not in result

    def test_filters_response_headers(self):
        """Test that problematic response headers are filtered."""
        from gateway.gateway import _filter_response_headers

        headers = {
            "content-type": "application/json",
            "x-request-id": "abc123",
            "content-encoding": "gzip",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
        }

        result = _filter_response_headers(headers)

        assert "content-type" in result
        assert "x-request-id" in result
        assert "content-encoding" not in result
        assert "transfer-encoding" not in result
        assert "connection" not in result


@pytest.fixture
def mock_anthropic_dependencies():
    """Mock all dependencies for Anthropic proxy tests."""
    with (
        patch("gateway.gateway.get_credentials_manager") as mock_cred_mgr,
        patch("gateway.gateway.get_session_manager") as mock_session_mgr,
        patch("gateway.gateway.get_anthropic_client") as mock_client,
    ):
        # Configure credentials manager
        mock_cred = MagicMock()
        mock_cred.header_name = "x-api-key"
        mock_cred.header_value = "sk-ant-test-key"
        mock_cred_mgr.return_value.get_credential.return_value = mock_cred

        # Configure session manager (no session by default - public mode)
        mock_session_mgr.return_value.get_session_by_ip.return_value = None

        yield {
            "credentials_manager": mock_cred_mgr,
            "session_manager": mock_session_mgr,
            "anthropic_client": mock_client,
        }


@pytest.fixture
def anthropic_client(mock_anthropic_dependencies):
    """Create Flask test client for Anthropic proxy tests."""
    from gateway.gateway import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestProxyAnthropicMessages:
    """Tests for POST /v1/messages endpoint."""

    def test_non_streaming_request(self, anthropic_client, mock_anthropic_dependencies):
        """Test non-streaming API request."""
        mock_response = MagicMock()
        mock_response.content = b'{"content": [{"type": "text", "text": "Hello"}]}'
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json", "x-request-id": "123"}

        mock_client = mock_anthropic_dependencies["anthropic_client"].return_value
        mock_client.post.return_value = mock_response

        response = anthropic_client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200

    def test_filters_tools_in_private_mode(self, anthropic_client, mock_anthropic_dependencies):
        """Test that web tools are filtered in private mode."""
        # Configure session to be in private mode
        mock_session = MagicMock()
        mock_session.mode = "private"
        session_mgr = mock_anthropic_dependencies["session_manager"].return_value
        session_mgr.get_session_by_ip.return_value = mock_session

        mock_response = MagicMock()
        mock_response.content = b'{"content": []}'
        mock_response.status_code = 200
        mock_response.headers = {}

        mock_client = mock_anthropic_dependencies["anthropic_client"].return_value
        mock_client.post.return_value = mock_response

        response = anthropic_client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 1024,
                    "tools": [
                        {"name": "bash", "description": "Run commands"},
                        {"name": "web_search", "description": "Search web"},
                    ],
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            ),
            content_type="application/json",
        )

        # Verify the request was made
        assert response.status_code == 200

        # Verify tools were filtered by checking what was sent
        call_args = mock_client.post.call_args
        sent_body = json.loads(call_args.kwargs["content"])
        tool_names = [t["name"] for t in sent_body.get("tools", [])]
        assert "bash" in tool_names
        assert "web_search" not in tool_names

    def test_no_credentials_returns_401(self, anthropic_client, mock_anthropic_dependencies):
        """Test that missing credentials returns 401."""
        # No gateway credentials
        cred_mgr = mock_anthropic_dependencies["credentials_manager"].return_value
        cred_mgr.get_credential.return_value = None

        response = anthropic_client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data
        assert data["error"]["type"] == "authentication_error"


class TestProxyCountTokens:
    """Tests for POST /v1/messages/count_tokens endpoint."""

    def test_count_tokens_request(self, anthropic_client, mock_anthropic_dependencies):
        """Test token counting API request."""
        mock_response = MagicMock()
        mock_response.content = b'{"input_tokens": 10}'
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}

        mock_client = mock_anthropic_dependencies["anthropic_client"].return_value
        mock_client.post.return_value = mock_response

        response = anthropic_client.post(
            "/v1/messages/count_tokens",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "messages": [{"role": "user", "content": "Hello world"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert "input_tokens" in data

    def test_no_credentials_returns_401(self, anthropic_client, mock_anthropic_dependencies):
        """Test that missing credentials returns 401."""
        cred_mgr = mock_anthropic_dependencies["credentials_manager"].return_value
        cred_mgr.get_credential.return_value = None

        response = anthropic_client.post(
            "/v1/messages/count_tokens",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 401


class TestCredentialInjection:
    """Tests for credential injection function."""

    def test_injects_api_key(self):
        """Test that API key is injected correctly."""
        from gateway.gateway import _inject_anthropic_credentials

        with patch("gateway.gateway.get_credentials_manager") as mock_mgr:
            mock_cred = MagicMock()
            mock_cred.header_name = "x-api-key"
            mock_cred.header_value = "sk-ant-test-key"
            mock_mgr.return_value.get_credential.return_value = mock_cred

            headers = {"Content-Type": "application/json"}
            result, error = _inject_anthropic_credentials(headers)

            assert error is None
            assert result["x-api-key"] == "sk-ant-test-key"

    def test_injects_oauth_token(self):
        """Test that OAuth token is injected correctly."""
        from gateway.gateway import _inject_anthropic_credentials

        with patch("gateway.gateway.get_credentials_manager") as mock_mgr:
            mock_cred = MagicMock()
            mock_cred.header_name = "Authorization"
            mock_cred.header_value = "Bearer oauth-token"
            mock_mgr.return_value.get_credential.return_value = mock_cred

            headers = {"Content-Type": "application/json"}
            result, error = _inject_anthropic_credentials(headers)

            assert error is None
            assert result["Authorization"] == "Bearer oauth-token"

    def test_allows_client_auth_when_no_gateway_cred(self):
        """Test that client-provided auth is allowed when no gateway cred."""
        from gateway.gateway import _inject_anthropic_credentials

        with patch("gateway.gateway.get_credentials_manager") as mock_mgr:
            mock_mgr.return_value.get_credential.return_value = None

            # Client provides their own auth
            headers = {"Content-Type": "application/json", "Authorization": "Bearer client-token"}
            result, error = _inject_anthropic_credentials(headers)

            assert error is None
            assert result["Authorization"] == "Bearer client-token"

    def test_returns_error_when_no_auth(self):
        """Test that error is returned when no auth is available."""
        from gateway.gateway import _inject_anthropic_credentials, app

        with patch("gateway.gateway.get_credentials_manager") as mock_mgr:
            mock_mgr.return_value.get_credential.return_value = None

            # Need app context for jsonify
            with app.app_context():
                headers = {"Content-Type": "application/json"}
                result, error = _inject_anthropic_credentials(headers)

                assert error is not None
