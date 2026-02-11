"""Tests for Anthropic API proxy endpoints."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add gateway to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "gateway"))


class TestIsStreamingRequest:
    """Test _is_streaming_request helper."""

    def test_stream_true(self):
        """Test detection of stream: true."""
        from gateway.gateway import _is_streaming_request

        body = json.dumps({"model": "claude-3", "stream": True}).encode()
        assert _is_streaming_request(body) is True

    def test_stream_false(self):
        """Test detection of stream: false."""
        from gateway.gateway import _is_streaming_request

        body = json.dumps({"model": "claude-3", "stream": False}).encode()
        assert _is_streaming_request(body) is False

    def test_stream_missing(self):
        """Test when stream key is missing."""
        from gateway.gateway import _is_streaming_request

        body = json.dumps({"model": "claude-3"}).encode()
        assert _is_streaming_request(body) is False

    def test_stream_in_string(self):
        """Test that stream in string content is not detected as streaming."""
        from gateway.gateway import _is_streaming_request

        # Stream appears in message content but not as a parameter
        body = json.dumps(
            {
                "model": "claude-3",
                "messages": [{"role": "user", "content": '"stream":true in my text'}],
            }
        ).encode()
        assert _is_streaming_request(body) is False

    def test_invalid_json(self):
        """Test handling of invalid JSON."""
        from gateway.gateway import _is_streaming_request

        assert _is_streaming_request(b"not json") is False

    def test_empty_body(self):
        """Test handling of empty body."""
        from gateway.gateway import _is_streaming_request

        assert _is_streaming_request(b"") is False


class TestInjectAnthropicCredentials:
    """Test _inject_anthropic_credentials helper."""

    @pytest.fixture
    def mock_credentials_manager(self):
        """Create a mock credentials manager."""
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            manager = MagicMock()
            mock_get.return_value = manager
            yield manager

    def test_injects_api_key(self, mock_credentials_manager):
        """Test that API key is injected."""
        from gateway.gateway import _inject_anthropic_credentials

        mock_cred = MagicMock()
        mock_cred.header_name = "x-api-key"
        mock_cred.header_value = "sk-ant-test"
        mock_credentials_manager.get_credential.return_value = mock_cred

        headers = {"Content-Type": "application/json"}
        result_headers, error = _inject_anthropic_credentials(headers)

        assert error is None
        assert result_headers["x-api-key"] == "sk-ant-test"

    def test_injects_oauth_token(self, mock_credentials_manager):
        """Test that OAuth token is injected."""
        from gateway.gateway import _inject_anthropic_credentials

        mock_cred = MagicMock()
        mock_cred.header_name = "Authorization"
        mock_cred.header_value = "Bearer oauth-token-123"
        mock_credentials_manager.get_credential.return_value = mock_cred

        headers = {"Content-Type": "application/json"}
        result_headers, error = _inject_anthropic_credentials(headers)

        assert error is None
        assert result_headers["Authorization"] == "Bearer oauth-token-123"

    def test_preserves_client_authorization(self, mock_credentials_manager):
        """Test that client-provided Authorization header is preserved."""
        from gateway.gateway import _inject_anthropic_credentials

        mock_credentials_manager.get_credential.return_value = None

        headers = {"Content-Type": "application/json", "Authorization": "Bearer user-token"}
        result_headers, error = _inject_anthropic_credentials(headers)

        assert error is None
        assert result_headers["Authorization"] == "Bearer user-token"

    def test_preserves_client_api_key(self, mock_credentials_manager):
        """Test that client-provided x-api-key header is preserved."""
        from gateway.gateway import _inject_anthropic_credentials

        mock_credentials_manager.get_credential.return_value = None

        headers = {"Content-Type": "application/json", "x-api-key": "user-api-key"}
        result_headers, error = _inject_anthropic_credentials(headers)

        assert error is None
        assert result_headers["x-api-key"] == "user-api-key"

    def test_error_no_credentials(self, mock_credentials_manager):
        """Test error when no credentials available."""
        from gateway.gateway import _inject_anthropic_credentials, app

        mock_credentials_manager.get_credential.return_value = None

        headers = {"Content-Type": "application/json"}
        # _inject_anthropic_credentials uses jsonify() which requires app context
        with app.app_context():
            _result_headers, error = _inject_anthropic_credentials(headers)

        assert error is not None
        # error is (response, status_code) tuple
        assert error[1] == 401


class TestGetForwardedHeaders:
    """Test _get_forwarded_headers helper."""

    def test_blocks_sensitive_headers(self):
        """Test that sensitive headers are blocked."""
        from werkzeug.datastructures import Headers

        from gateway.gateway import _get_forwarded_headers

        incoming = Headers(
            [
                ("Host", "malicious.com"),
                ("Content-Length", "100"),
                ("Transfer-Encoding", "chunked"),
                ("Authorization", "Bearer secret"),
                ("x-api-key", "sk-secret"),
                ("Connection", "keep-alive"),
                ("anthropic-version", "2024-01-01"),
                ("X-Custom-Header", "allowed"),
            ]
        )

        result = _get_forwarded_headers(incoming)

        # Sensitive headers should be blocked (check both lowercase and original case)
        assert "host" not in result
        assert "Host" not in result
        assert "content-length" not in result
        assert "Content-Length" not in result
        assert "transfer-encoding" not in result
        assert "Transfer-Encoding" not in result
        assert "authorization" not in result
        assert "Authorization" not in result
        assert "x-api-key" not in result
        assert "connection" not in result
        assert "Connection" not in result

        # Safe headers should be forwarded
        assert result.get("anthropic-version") == "2024-01-01"
        assert result.get("X-Custom-Header") == "allowed"


class TestFilterResponseHeaders:
    """Test _filter_response_headers helper."""

    def test_filters_hop_by_hop_headers(self):
        """Test that hop-by-hop headers are filtered."""
        from httpx import Headers

        from gateway.gateway import _filter_response_headers

        incoming = Headers(
            [
                ("content-type", "application/json"),
                ("content-encoding", "gzip"),
                ("transfer-encoding", "chunked"),
                ("connection", "keep-alive"),
                ("x-request-id", "req-123"),
            ]
        )

        result = _filter_response_headers(incoming)

        # Hop-by-hop headers should be filtered
        assert "content-encoding" not in result
        assert "transfer-encoding" not in result
        assert "connection" not in result

        # Other headers should pass through
        assert result["content-type"] == "application/json"
        assert result["x-request-id"] == "req-123"


class TestProxyMessagesEndpoint:
    """Test /v1/messages endpoint."""

    @pytest.fixture
    def client(self):
        """Create a test client for the Flask app."""
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    @pytest.fixture
    def mock_httpx_client(self):
        """Mock the httpx client."""
        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def mock_credentials(self):
        """Mock credentials manager to return valid credential."""
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            manager = MagicMock()
            cred = MagicMock()
            cred.header_name = "x-api-key"
            cred.header_value = "sk-ant-test"
            manager.get_credential.return_value = cred
            mock_get.return_value = manager
            yield manager

    def test_non_streaming_success(self, client, mock_httpx_client, mock_credentials):
        """Test non-streaming request success."""
        from httpx import Headers

        # Mock successful response
        mock_response = MagicMock()
        mock_response.content = json.dumps({"content": "Hello"}).encode()
        mock_response.status_code = 200
        mock_response.headers = Headers(
            [
                ("content-type", "application/json"),
                ("x-request-id", "req-123"),
            ]
        )
        mock_httpx_client.post.return_value = mock_response

        response = client.post(
            "/v1/messages",
            data=json.dumps({"model": "claude-3", "stream": False}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["content"] == "Hello"

    def test_error_passthrough(self, client, mock_httpx_client, mock_credentials):
        """Test that Anthropic API errors are passed through."""
        from httpx import Headers

        # Mock error response
        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {"error": {"type": "invalid_request_error", "message": "Bad request"}}
        ).encode()
        mock_response.status_code = 400
        mock_response.headers = Headers(
            [
                ("content-type", "application/json"),
                ("x-request-id", "req-456"),
            ]
        )
        mock_httpx_client.post.return_value = mock_response

        response = client.post(
            "/v1/messages",
            data=json.dumps({"model": "invalid"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["error"]["type"] == "invalid_request_error"

    def test_rate_limit_passthrough(self, client, mock_httpx_client, mock_credentials):
        """Test that 429 rate limit responses are passed through."""
        from httpx import Headers

        mock_response = MagicMock()
        mock_response.content = json.dumps(
            {"error": {"type": "rate_limit_error", "message": "Rate limited"}}
        ).encode()
        mock_response.status_code = 429
        mock_response.headers = Headers(
            [
                ("content-type", "application/json"),
                ("retry-after", "60"),
            ]
        )
        mock_httpx_client.post.return_value = mock_response

        response = client.post(
            "/v1/messages",
            data=json.dumps({"model": "claude-3"}),
            content_type="application/json",
        )

        assert response.status_code == 429

    def test_no_credentials_returns_401(self, client, mock_httpx_client):
        """Test that missing credentials returns 401."""
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            manager = MagicMock()
            manager.get_credential.return_value = None
            mock_get.return_value = manager

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3"}),
                content_type="application/json",
            )

            assert response.status_code == 401
            data = json.loads(response.data)
            assert data["error"]["type"] == "authentication_error"

    def test_connection_error_returns_502(self, client, mock_httpx_client, mock_credentials):
        """Test that connection errors return 502."""
        import httpx

        mock_httpx_client.post.side_effect = httpx.ConnectError("Connection refused")

        response = client.post(
            "/v1/messages",
            data=json.dumps({"model": "claude-3"}),
            content_type="application/json",
        )

        assert response.status_code == 502
        data = json.loads(response.data)
        assert (
            "Connection" in data["error"]["message"]
            or "connect" in data["error"]["message"].lower()
        )

    def test_timeout_error_returns_504(self, client, mock_httpx_client, mock_credentials):
        """Test that timeout errors return 504."""
        import httpx

        mock_httpx_client.post.side_effect = httpx.TimeoutException("Request timed out")

        response = client.post(
            "/v1/messages",
            data=json.dumps({"model": "claude-3"}),
            content_type="application/json",
        )

        assert response.status_code == 504
        data = json.loads(response.data)
        assert (
            "timeout" in data["error"]["message"].lower()
            or "timed out" in data["error"]["message"].lower()
        )


class TestProxyCountTokensEndpoint:
    """Test /v1/messages/count_tokens endpoint."""

    @pytest.fixture
    def client(self):
        """Create a test client for the Flask app."""
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    @pytest.fixture
    def mock_httpx_client(self):
        """Mock the httpx client."""
        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client
            yield mock_client

    @pytest.fixture
    def mock_credentials(self):
        """Mock credentials manager."""
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            manager = MagicMock()
            cred = MagicMock()
            cred.header_name = "x-api-key"
            cred.header_value = "sk-ant-test"
            manager.get_credential.return_value = cred
            mock_get.return_value = manager
            yield manager

    def test_count_tokens_success(self, client, mock_httpx_client, mock_credentials):
        """Test successful token counting."""
        from httpx import Headers

        mock_response = MagicMock()
        mock_response.content = json.dumps({"input_tokens": 42}).encode()
        mock_response.status_code = 200
        mock_response.headers = Headers([("content-type", "application/json")])
        mock_httpx_client.post.return_value = mock_response

        response = client.post(
            "/v1/messages/count_tokens",
            data=json.dumps({"model": "claude-3", "messages": []}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["input_tokens"] == 42

    def test_count_tokens_no_credentials(self, client, mock_httpx_client):
        """Test that missing credentials returns 401."""
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            manager = MagicMock()
            manager.get_credential.return_value = None
            mock_get.return_value = manager

            response = client.post(
                "/v1/messages/count_tokens",
                data=json.dumps({"model": "claude-3"}),
                content_type="application/json",
            )

            assert response.status_code == 401


class TestStreamingResponse:
    """Test streaming response handling."""

    @pytest.fixture
    def client(self):
        """Create a test client for the Flask app."""
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    @pytest.fixture
    def mock_credentials(self):
        """Mock credentials manager."""
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            manager = MagicMock()
            cred = MagicMock()
            cred.header_name = "x-api-key"
            cred.header_value = "sk-ant-test"
            manager.get_credential.return_value = cred
            mock_get.return_value = manager
            yield manager

    def test_streaming_request_detected(self, client, mock_credentials):
        """Test that streaming requests use streaming handler."""
        from httpx import Headers

        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client

            # Create a mock response for send(stream=True)
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = Headers([("content-type", "text/event-stream")])

            # Simulate SSE data chunks
            sse_chunks = [
                b'event: message_start\ndata: {"type":"message_start"}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
            mock_response.iter_bytes = MagicMock(return_value=iter(sse_chunks))
            mock_response.close = MagicMock()

            mock_client.build_request.return_value = MagicMock()
            mock_client.send.return_value = mock_response

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3", "stream": True}),
                content_type="application/json",
            )

            # Verify streaming was used (send called with stream=True)
            mock_client.send.assert_called_once()
            call_kwargs = mock_client.send.call_args[1]
            assert call_kwargs.get("stream") is True
            assert response.status_code == 200

            # Collect all streamed data
            data = b"".join(response.response)
            assert b"message_start" in data
            assert b"message_stop" in data

    def test_streaming_content_type_forwarded(self, client, mock_credentials):
        """Test that Content-Type is forwarded from upstream."""
        from httpx import Headers

        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client

            mock_response = MagicMock()
            mock_response.status_code = 200
            # Anthropic sends text/event-stream; charset=utf-8
            mock_response.headers = Headers([("content-type", "text/event-stream; charset=utf-8")])
            mock_response.iter_bytes = MagicMock(return_value=iter([b"data: test\n\n"]))
            mock_response.close = MagicMock()

            mock_client.build_request.return_value = MagicMock()
            mock_client.send.return_value = mock_response

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3", "stream": True}),
                content_type="application/json",
            )

            assert "text/event-stream" in response.content_type


class TestFilterBlockedTools:
    """Test _filter_blocked_tools helper for private mode security."""

    def test_filters_web_search_in_private_mode(self):
        """Test that web_search is removed in private mode."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps(
            {
                "model": "claude-3",
                "tools": [
                    {"name": "web_search", "description": "Search the web"},
                    {"name": "Read", "description": "Read files"},
                ],
            }
        ).encode()

        result = json.loads(_filter_blocked_tools(body, session_mode="private"))

        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "Read"

    def test_filters_web_fetch_in_private_mode(self):
        """Test that web_fetch is removed in private mode."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps(
            {
                "model": "claude-3",
                "tools": [
                    {"name": "WebFetch", "description": "Fetch URLs"},
                    {"name": "Bash", "description": "Run commands"},
                ],
            }
        ).encode()

        result = json.loads(_filter_blocked_tools(body, session_mode="private"))

        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "Bash"

    def test_filters_all_blocked_tools(self):
        """Test that all blocked tool variants are removed."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps(
            {
                "model": "claude-3",
                "tools": [
                    {"name": "web_search"},
                    {"name": "WebSearch"},
                    {"name": "web_fetch"},
                    {"name": "WebFetch"},
                    {"name": "Read"},
                ],
            }
        ).encode()

        result = json.loads(_filter_blocked_tools(body, session_mode="private"))

        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "Read"

    def test_no_filtering_in_public_mode(self):
        """Test that tools are not filtered in public mode."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps(
            {
                "model": "claude-3",
                "tools": [
                    {"name": "web_search"},
                    {"name": "Read"},
                ],
            }
        ).encode()

        result = _filter_blocked_tools(body, session_mode="public")

        # Should return original body unchanged
        result_json = json.loads(result)
        assert len(result_json["tools"]) == 2

    def test_no_filtering_when_session_mode_is_none(self):
        """Test that tools are not filtered when session_mode is None."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps(
            {
                "model": "claude-3",
                "tools": [
                    {"name": "web_search"},
                    {"name": "Read"},
                ],
            }
        ).encode()

        result = _filter_blocked_tools(body, session_mode=None)
        result_json = json.loads(result)
        assert len(result_json["tools"]) == 2

    def test_handles_missing_tools_key(self):
        """Test that requests without tools are passed through."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps({"model": "claude-3", "messages": []}).encode()

        result = _filter_blocked_tools(body, session_mode="private")

        # Should return original body unchanged
        assert result == body

    def test_handles_invalid_json(self):
        """Test that invalid JSON is passed through unchanged."""
        from gateway.gateway import _filter_blocked_tools

        body = b"not valid json"

        result = _filter_blocked_tools(body, session_mode="private")

        # Should return original body unchanged
        assert result == body

    def test_handles_empty_tools_array(self):
        """Test that empty tools array is handled correctly."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps({"model": "claude-3", "tools": []}).encode()

        result = _filter_blocked_tools(body, session_mode="private")
        result_json = json.loads(result)

        assert result_json["tools"] == []

    def test_preserves_other_request_fields(self):
        """Test that other request fields are preserved after filtering."""
        from gateway.gateway import _filter_blocked_tools

        body = json.dumps(
            {
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
                "stream": True,
                "tools": [
                    {"name": "web_search"},
                    {"name": "Read"},
                ],
            }
        ).encode()

        result = json.loads(_filter_blocked_tools(body, session_mode="private"))

        assert result["model"] == "claude-3"
        assert result["messages"] == [{"role": "user", "content": "Hello"}]
        assert result["max_tokens"] == 1024
        assert result["stream"] is True
        assert len(result["tools"]) == 1


class TestParseSSEResponse:
    """Tests for _parse_sse_response helper that reassembles streaming responses."""

    def test_parse_simple_text_response(self):
        """Test parsing a simple text response from SSE chunks."""
        from gateway.gateway import _parse_sse_response

        chunks = [
            b'data: {"type":"message_start","message":{"id":"msg_123","model":"claude-opus-4-5-20251101","role":"assistant"}}\n\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" there!"}}\n\n',
            b'data: {"type":"content_block_stop","index":0}\n\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":10}}\n\n',
            b"data: [DONE]\n\n",
        ]

        content, usage, model, stop_reason = _parse_sse_response(chunks)

        assert content is not None
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Hello there!"
        assert usage == {"output_tokens": 10}
        assert model == "claude-opus-4-5-20251101"
        assert stop_reason == "end_turn"

    def test_parse_tool_use_response(self):
        """Test parsing a response with tool use from SSE chunks."""
        from gateway.gateway import _parse_sse_response

        chunks = [
            b'data: {"type":"message_start","message":{"model":"claude-3-5-sonnet-20241022"}}\n\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_123","name":"Bash"}}\n\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"com"}}\n\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"mand\\": \\"ls\\"}"}}\n\n',
            b'data: {"type":"content_block_stop","index":0}\n\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":25}}\n\n',
            b"data: [DONE]\n\n",
        ]

        content, usage, model, stop_reason = _parse_sse_response(chunks)

        assert content is not None
        assert len(content) == 1
        assert content[0]["type"] == "tool_use"
        assert content[0]["id"] == "toolu_123"
        assert content[0]["name"] == "Bash"
        assert content[0]["input"] == {"command": "ls"}
        assert stop_reason == "tool_use"

    def test_parse_multiple_content_blocks(self):
        """Test parsing a response with multiple content blocks."""
        from gateway.gateway import _parse_sse_response

        chunks = [
            b'data: {"type":"message_start","message":{"model":"claude-3"}}\n\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"First block"}}\n\n',
            b'data: {"type":"content_block_stop","index":0}\n\n',
            b'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n',
            b'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Second block"}}\n\n',
            b'data: {"type":"content_block_stop","index":1}\n\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
            b"data: [DONE]\n\n",
        ]

        content, usage, model, stop_reason = _parse_sse_response(chunks)

        assert content is not None
        assert len(content) == 2
        assert content[0]["text"] == "First block"
        assert content[1]["text"] == "Second block"

    def test_parse_empty_response(self):
        """Test parsing empty chunks."""
        from gateway.gateway import _parse_sse_response

        chunks = []
        content, usage, model, stop_reason = _parse_sse_response(chunks)

        assert content is None
        assert usage is None
        assert model is None
        assert stop_reason is None

    def test_parse_malformed_sse(self):
        """Test parsing malformed SSE data."""
        from gateway.gateway import _parse_sse_response

        chunks = [
            b"data: not json\n\n",
            b"invalid line without data prefix\n\n",
            b"data: [DONE]\n\n",
        ]

        # Should not raise, just return empty/None
        content, usage, model, stop_reason = _parse_sse_response(chunks)
        assert content is None or len(content) == 0

    def test_parse_tool_use_with_incomplete_json(self):
        """Test parsing tool_use with incomplete JSON sets input_parse_error flag."""
        from gateway.gateway import _parse_sse_response

        # Simulate a streaming response where the tool_use input JSON is incomplete
        # (e.g., connection dropped mid-stream)
        chunks = [
            b'data: {"type":"message_start","message":{"model":"claude-3"}}\n\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_broken","name":"Bash"}}\n\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\": \\"ls"}}\n\n',
            # Incomplete JSON - missing closing brace and quote
            b'data: {"type":"content_block_stop","index":0}\n\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
            b"data: [DONE]\n\n",
        ]

        content, usage, model, stop_reason = _parse_sse_response(chunks)

        assert content is not None
        assert len(content) == 1
        assert content[0]["type"] == "tool_use"
        assert content[0]["id"] == "toolu_broken"
        assert content[0]["name"] == "Bash"
        # Should have empty input due to parse failure
        assert content[0]["input"] == {}
        # Should have input_parse_error flag set
        assert content[0].get("input_parse_error") is True
        # Should have raw_partial_input for debugging
        assert "raw_partial_input" in content[0]
        assert '{"command": "ls' in content[0]["raw_partial_input"]

    def test_parse_tool_use_with_long_incomplete_json_truncates(self):
        """Test that raw_partial_input is truncated for very long incomplete JSON."""
        from gateway.gateway import RAW_INPUT_TRUNCATE_SIZE, _parse_sse_response

        # Create a very long incomplete JSON input
        long_value = "x" * 2000  # Longer than RAW_INPUT_TRUNCATE_SIZE (1000)
        partial_json = f'{{"command": "{long_value}'

        chunks = [
            b'data: {"type":"message_start","message":{"model":"claude-3"}}\n\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_long","name":"Bash"}}\n\n',
            f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"input_json_delta","partial_json":"{partial_json.replace(chr(34), chr(92) + chr(34))}"}}}}\n\n'.encode(),
            b'data: {"type":"content_block_stop","index":0}\n\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
            b"data: [DONE]\n\n",
        ]

        content, usage, model, stop_reason = _parse_sse_response(chunks)

        assert content is not None
        assert len(content) == 1
        assert content[0].get("input_parse_error") is True
        # raw_partial_input should be truncated to RAW_INPUT_TRUNCATE_SIZE
        raw_input = content[0].get("raw_partial_input", "")
        assert len(raw_input) == RAW_INPUT_TRUNCATE_SIZE


class TestTranscriptCaptureFunctions:
    """Tests for transcript capture helper functions."""

    def test_capture_non_streaming_response(self, tmp_path):
        """Test capturing a non-streaming API response."""
        import time
        from unittest.mock import patch

        # Patch buffer directory
        with patch("gateway.transcript_buffer.BUFFER_DIR", tmp_path):
            from gateway.gateway import _capture_non_streaming_response
            from gateway.transcript_buffer import TranscriptBuffer

            container_id = "test-container-123"
            request_json = {
                "model": "claude-opus-4-5",
                "messages": [{"role": "user", "content": "Hello"}],
            }
            response_body = json.dumps(
                {
                    "content": [{"type": "text", "text": "Hi there!"}],
                    "model": "claude-opus-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            ).encode()

            _capture_non_streaming_response(
                container_id=container_id,
                request_json=request_json,
                response_body=response_body,
                start_time=time.time() - 0.5,  # 500ms ago
            )

            # Verify buffer was written
            buffer = TranscriptBuffer(container_id, buffer_dir=tmp_path)
            entries = buffer.read_entries()
            assert len(entries) == 1

            entry = entries[0]
            assert entry["streaming"] is False
            assert entry["request"]["model"] == "claude-opus-4-5"
            assert entry["response"]["content"] == [{"type": "text", "text": "Hi there!"}]
            assert entry["response"]["stop_reason"] == "end_turn"
            assert entry["duration_ms"] >= 400  # At least 400ms

    def test_capture_streaming_response(self, tmp_path):
        """Test capturing a streaming API response."""
        import time
        from unittest.mock import patch

        with patch("gateway.transcript_buffer.BUFFER_DIR", tmp_path):
            from gateway.gateway import _capture_streaming_response
            from gateway.transcript_buffer import TranscriptBuffer

            container_id = "test-container-456"
            request_json = {
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hi"}],
            }
            chunks = [
                b'data: {"type":"message_start","message":{"model":"claude-3"}}\n\n',
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello!"}}\n\n',
                b'data: {"type":"content_block_stop","index":0}\n\n',
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n',
                b"data: [DONE]\n\n",
            ]

            _capture_streaming_response(
                container_id=container_id,
                request_json=request_json,
                chunks=chunks,
                start_time=time.time() - 0.3,
            )

            # Verify buffer was written
            buffer = TranscriptBuffer(container_id, buffer_dir=tmp_path)
            entries = buffer.read_entries()
            assert len(entries) == 1

            entry = entries[0]
            assert entry["streaming"] is True
            assert entry["response"]["content"][0]["text"] == "Hello!"
            assert entry["response"]["stop_reason"] == "end_turn"
            assert entry["response"]["usage"] == {"output_tokens": 5}

    def test_capture_handles_invalid_response(self, tmp_path):
        """Test that capture handles invalid response gracefully."""
        import time
        from unittest.mock import patch

        with patch("gateway.transcript_buffer.BUFFER_DIR", tmp_path):
            from gateway.gateway import _capture_non_streaming_response
            from gateway.transcript_buffer import TranscriptBuffer

            container_id = "test-container-789"

            # Invalid JSON response
            _capture_non_streaming_response(
                container_id=container_id,
                request_json={"model": "claude-3"},
                response_body=b"not json",
                start_time=time.time(),
            )

            # Should not crash, and should not write to buffer
            buffer = TranscriptBuffer(container_id, buffer_dir=tmp_path)
            entries = buffer.read_entries()
            assert len(entries) == 0
