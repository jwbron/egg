"""Integration tests for Anthropic API proxy.

Tests end-to-end request/response flow through the gateway proxy
with a mock Anthropic API server.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest


class MockAnthropicHandler(BaseHTTPRequestHandler):
    """Mock Anthropic API server for integration testing."""

    def log_message(self, format, *args):
        """Suppress log messages during tests."""
        pass

    def do_POST(self):
        """Handle POST requests to mock Anthropic endpoints."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Store request for verification
        self.server.last_request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else None,
        }

        if self.path == "/v1/messages":
            self._handle_messages(body)
        elif self.path == "/v1/messages/count_tokens":
            self._handle_count_tokens()
        else:
            self.send_error(404, "Not Found")

    def _handle_messages(self, body):
        """Handle /v1/messages endpoint."""
        try:
            request_data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        # Check for streaming
        if request_data.get("stream"):
            self._send_streaming_response()
        else:
            self._send_json_response()

    def _send_json_response(self):
        """Send non-streaming JSON response."""
        response = {
            "id": "msg_test123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello from mock API!"}],
            "model": "claude-3-opus-20240229",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        response_bytes = json.dumps(response).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", "mock-request-id")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _send_streaming_response(self):
        """Send SSE streaming response."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-request-id", "mock-stream-id")
        self.end_headers()

        events = [
            "event: message_start\n"
            'data: {"type": "message_start", "message": {"id": "msg_test"}}\n\n',
            'event: content_block_start\ndata: {"type": "content_block_start", "index": 0}\n\n',
            "event: content_block_delta\n"
            'data: {"type": "content_block_delta", '
            '"delta": {"type": "text_delta", "text": "Hello"}}\n\n',
            'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n',
            'event: message_stop\ndata: {"type": "message_stop"}\n\n',
        ]

        for event in events:
            self.wfile.write(event.encode())
            self.wfile.flush()

    def _handle_count_tokens(self):
        """Handle /v1/messages/count_tokens endpoint."""
        response = {"input_tokens": 42}
        response_bytes = json.dumps(response).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)


@pytest.fixture
def mock_anthropic_server():
    """Start a mock Anthropic API server."""
    server = HTTPServer(("127.0.0.1", 0), MockAnthropicHandler)
    server.last_request = None
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    # Give server time to start
    time.sleep(0.1)

    yield server

    server.shutdown()


@pytest.fixture
def gateway_client_with_mock_anthropic(mock_anthropic_server):
    """Create gateway test client configured to use mock Anthropic server."""
    import httpx

    # Create a real httpx client pointing to our mock server
    mock_port = mock_anthropic_server.server_address[1]
    real_client = httpx.Client(
        base_url=f"http://127.0.0.1:{mock_port}",
        timeout=httpx.Timeout(10.0, connect=5.0),
    )

    # Mock credentials
    mock_cred = MagicMock()
    mock_cred.header_name = "x-api-key"
    mock_cred.header_value = "sk-ant-integration-test-key"

    with (
        patch("gateway.gateway.get_anthropic_client", return_value=real_client),
        patch("gateway.gateway.get_credentials_manager") as mock_cred_mgr,
        patch("gateway.gateway.get_session_manager") as mock_session_mgr,
    ):
        mock_cred_mgr.return_value.get_credential.return_value = mock_cred
        mock_session_mgr.return_value.get_session_by_ip.return_value = None

        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield {
                "client": client,
                "mock_server": mock_anthropic_server,
                "session_manager": mock_session_mgr,
            }

    real_client.close()


class TestAnthropicProxyIntegration:
    """Integration tests for Anthropic API proxy."""

    def test_non_streaming_end_to_end(self, gateway_client_with_mock_anthropic):
        """Test complete non-streaming request/response flow."""
        client = gateway_client_with_mock_anthropic["client"]
        mock_server = gateway_client_with_mock_anthropic["mock_server"]

        response = client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Hello, Claude!"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.get_json()

        # Verify response structure
        assert data["id"] == "msg_test123"
        assert data["type"] == "message"
        assert data["content"][0]["text"] == "Hello from mock API!"

        # Verify request was forwarded with injected credentials
        assert mock_server.last_request is not None
        assert mock_server.last_request["path"] == "/v1/messages"
        assert "x-api-key" in mock_server.last_request["headers"]

    def test_streaming_end_to_end(self, gateway_client_with_mock_anthropic):
        """Test complete streaming request/response flow."""
        client = gateway_client_with_mock_anthropic["client"]

        response = client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 1024,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hello!"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.content_type == "text/event-stream"

        # Collect streaming response
        response_data = b"".join(response.response)

        # Verify SSE events were received
        assert b"message_start" in response_data
        assert b"content_block_delta" in response_data
        assert b"Hello" in response_data
        assert b"message_stop" in response_data

    def test_count_tokens_end_to_end(self, gateway_client_with_mock_anthropic):
        """Test token counting request/response flow."""
        client = gateway_client_with_mock_anthropic["client"]

        response = client.post(
            "/v1/messages/count_tokens",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "messages": [{"role": "user", "content": "Count my tokens!"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["input_tokens"] == 42

    def test_tool_filtering_in_private_mode(self, gateway_client_with_mock_anthropic):
        """Test that tools are filtered before reaching Anthropic API in private mode."""
        client = gateway_client_with_mock_anthropic["client"]
        mock_server = gateway_client_with_mock_anthropic["mock_server"]
        session_mgr = gateway_client_with_mock_anthropic["session_manager"]

        # Configure private mode
        mock_session = MagicMock()
        mock_session.mode = "private"
        session_mgr.return_value.get_session_by_ip.return_value = mock_session

        response = client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 1024,
                    "tools": [
                        {"name": "bash", "description": "Run commands"},
                        {"name": "web_search", "description": "Search the web"},
                        {"name": "WebFetch", "description": "Fetch pages"},
                    ],
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200

        # Verify tools were filtered before reaching mock Anthropic server
        forwarded_request = mock_server.last_request
        assert forwarded_request is not None
        tool_names = [t["name"] for t in forwarded_request["body"].get("tools", [])]
        assert "bash" in tool_names
        assert "web_search" not in tool_names
        assert "WebFetch" not in tool_names

    def test_credential_injection(self, gateway_client_with_mock_anthropic):
        """Test that credentials are injected into forwarded requests."""
        client = gateway_client_with_mock_anthropic["client"]
        mock_server = gateway_client_with_mock_anthropic["mock_server"]

        response = client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Test"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200

        # Verify the API key was injected
        forwarded_headers = mock_server.last_request["headers"]
        assert "x-api-key" in forwarded_headers
        assert forwarded_headers["x-api-key"] == "sk-ant-integration-test-key"

    def test_request_headers_forwarded(self, gateway_client_with_mock_anthropic):
        """Test that allowed headers are forwarded to Anthropic API."""
        client = gateway_client_with_mock_anthropic["client"]
        mock_server = gateway_client_with_mock_anthropic["mock_server"]

        response = client.post(
            "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Test"}],
                }
            ),
            content_type="application/json",
            headers={
                "anthropic-version": "2023-06-01",
                "X-Custom-Header": "custom-value",
            },
        )

        assert response.status_code == 200

        # Verify custom headers were forwarded (case-insensitive check)
        forwarded_headers = mock_server.last_request["headers"]
        # HTTP headers are case-insensitive, so normalize for comparison
        headers_lower = {k.lower(): v for k, v in forwarded_headers.items()}
        assert "anthropic-version" in headers_lower
        assert headers_lower["anthropic-version"] == "2023-06-01"


class TestAnthropicProxyErrorHandling:
    """Integration tests for error handling."""

    def test_handles_connection_error(self):
        """Test graceful handling of connection errors."""
        import httpx

        # Create client pointing to non-existent server
        bad_client = httpx.Client(
            base_url="http://127.0.0.1:59999",  # Port that should be closed
            timeout=httpx.Timeout(1.0, connect=0.5),
        )

        mock_cred = MagicMock()
        mock_cred.header_name = "x-api-key"
        mock_cred.header_value = "sk-ant-test"

        with (
            patch("gateway.gateway.get_anthropic_client", return_value=bad_client),
            patch("gateway.gateway.get_credentials_manager") as mock_cred_mgr,
            patch("gateway.gateway.get_session_manager") as mock_session_mgr,
        ):
            mock_cred_mgr.return_value.get_credential.return_value = mock_cred
            mock_session_mgr.return_value.get_session_by_ip.return_value = None

            from gateway.gateway import app

            app.config["TESTING"] = True
            with app.test_client() as client:
                response = client.post(
                    "/v1/messages",
                    data=json.dumps(
                        {
                            "model": "claude-3-opus-20240229",
                            "messages": [{"role": "user", "content": "Test"}],
                        }
                    ),
                    content_type="application/json",
                )

                # Should return 502 Bad Gateway
                assert response.status_code == 502
                data = response.get_json()
                assert data["error"]["type"] == "api_error"

        bad_client.close()
