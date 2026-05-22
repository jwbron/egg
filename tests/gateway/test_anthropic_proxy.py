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

    # ------------------------------------------------------------------
    # Upstream TCP-reset resilience tests (issue #1907).
    #
    # The gateway's /v1/messages proxy wraps the upstream stream with two
    # complementary pieces of resilience machinery in proxy_anthropic_messages():
    #
    #   (A) Pre-stream bounded (1x) retry around ``client.send()`` and the
    #       first-chunk prime — if httpx raises ReadError or
    #       RemoteProtocolError before any byte has flowed downstream, the
    #       gateway tears down the failed upstream and reissues the request
    #       transparently. The SDK never sees the reset.
    #
    #   (B) Mid-stream synthetic error frame — if the reset arrives after a
    #       chunk has already been yielded downstream, the gateway catches
    #       the exception inside ``generate()``, emits an Anthropic-style
    #       ``event: error`` SSE frame, and closes the stream cleanly so the
    #       downstream SDK fails gracefully instead of dying on a truncated
    #       socket.
    #
    # Each scenario below drives the exact failure mode that triggered the
    # original crash (pipeline issue-1901 lost 282s / 32 turns / $1.33 of
    # context to a bare ReadError propagated from httpcore).
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_then_raise(chunks, exc):
        """Yield each chunk in order, then raise ``exc``.

        Small helper for simulating upstream TCP resets during SSE streaming.
        Passing an empty ``chunks`` list causes ``exc`` to be raised on the
        very first ``next()`` pull (simulating a reset before any byte is
        forwarded downstream). Passing N chunks causes the raise to land on
        pull N+1 (simulating a mid-stream reset after N chunks have been
        yielded).
        """
        yield from chunks
        raise exc

    def test_streaming_send_reset_retries_once(self, client, mock_credentials):
        """(a) client.send() raises ReadError once; retry succeeds with clean 200 SSE.

        Covers task-1-1 acceptance: ``When client.send() raises ReadError
        once, the retry succeeds and downstream sees a clean 200 SSE response.``
        """
        import httpx
        from httpx import Headers

        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client

            good_chunks = [
                b'event: message_start\ndata: {"type":"message_start"}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
            good_response = MagicMock()
            good_response.status_code = 200
            good_response.headers = Headers([("content-type", "text/event-stream")])
            good_response.iter_bytes = MagicMock(return_value=iter(good_chunks))
            good_response.close = MagicMock()

            mock_client.build_request.return_value = MagicMock()
            # First send() raises ReadError; second returns the good response.
            mock_client.send.side_effect = [
                httpx.ReadError("connection reset by peer"),
                good_response,
            ]

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3", "stream": True}),
                content_type="application/json",
            )

            # The gateway retried exactly once (bounded 1x retry).
            assert mock_client.send.call_count == 2
            # Downstream saw a clean 200 with the full good response body.
            assert response.status_code == 200
            body = b"".join(response.response)
            assert b"message_start" in body
            assert b"message_stop" in body
            # Retry succeeded, so no synthetic error frame should be emitted.
            assert b"event: error" not in body
            # Upstream released via the finally: branch.
            good_response.close.assert_called()

    def test_streaming_first_chunk_reset_retries_once(self, client, mock_credentials):
        """(b) iter_bytes() raises on first pull once; retry succeeds.

        Covers task-1-1 acceptance: ``When the first iter_bytes() call
        raises ReadError, the gateway re-primes and produces a normal stream.``
        Simulates an upstream reset that happens after the TCP connection
        established but before the first SSE byte — a common
        connection-pool-staleness failure mode at Anthropic's edge.
        """
        import httpx
        from httpx import Headers

        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client

            # First upstream: iter_bytes returns a generator that raises on
            # the first next() pull — i.e., no chunk is ever yielded downstream.
            bad_response = MagicMock()
            bad_response.status_code = 200
            bad_response.headers = Headers([("content-type", "text/event-stream")])
            bad_response.iter_bytes = MagicMock(
                return_value=self._iter_then_raise(
                    [], httpx.ReadError("peer reset during first-chunk prime")
                )
            )
            bad_response.close = MagicMock()

            # Second upstream: healthy normal stream.
            good_chunks = [
                b'event: message_start\ndata: {"type":"message_start"}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
            good_response = MagicMock()
            good_response.status_code = 200
            good_response.headers = Headers([("content-type", "text/event-stream")])
            good_response.iter_bytes = MagicMock(return_value=iter(good_chunks))
            good_response.close = MagicMock()

            mock_client.build_request.return_value = MagicMock()
            mock_client.send.side_effect = [bad_response, good_response]

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3", "stream": True}),
                content_type="application/json",
            )

            # Exactly one retry happened.
            assert mock_client.send.call_count == 2
            # The failed upstream must be closed so the connection isn't leaked
            # back into the httpx connection pool in a half-open state.
            bad_response.close.assert_called()
            # Downstream saw a clean 200 with the healthy stream body.
            assert response.status_code == 200
            body = b"".join(response.response)
            assert b"message_start" in body
            assert b"message_stop" in body
            assert b"event: error" not in body
            good_response.close.assert_called()

    def test_streaming_midstream_reset_yields_synthetic_error_frame(self, client, mock_credentials):
        """(c) RemoteProtocolError after first chunk; body ends with synthetic error frame.

        Covers task-1-2 acceptance: ``When iter_bytes() raises after one
        chunk has been yielded, the downstream body contains the original
        chunk followed by a well-formed `event: error` SSE frame, the
        stream closes without raising, and _capture_streaming_response
        still runs.`` No retry is attempted here because a byte has already
        flowed downstream — the gateway cannot re-issue idempotently once
        the SDK has begun parsing SSE events.
        """
        import httpx
        from httpx import Headers

        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client

            first_chunk = (
                b"event: message_start\n"
                b'data: {"type":"message_start","message":'
                b'{"id":"msg_abc","model":"claude-3","role":"assistant"}}\n\n'
            )
            mid_reset_response = MagicMock()
            mid_reset_response.status_code = 200
            mid_reset_response.headers = Headers([("content-type", "text/event-stream")])
            # Yield one valid SSE chunk, then simulate Anthropic's edge
            # dropping the connection mid-stream (the exact failure mode
            # from #1901 — httpx surfaces it as RemoteProtocolError when
            # the peer closes without sending a complete body).
            mid_reset_response.iter_bytes = MagicMock(
                return_value=self._iter_then_raise(
                    [first_chunk],
                    httpx.RemoteProtocolError(
                        "peer closed connection without sending complete message body"
                    ),
                )
            )
            mid_reset_response.close = MagicMock()

            mock_client.build_request.return_value = MagicMock()
            mock_client.send.return_value = mid_reset_response

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3", "stream": True}),
                content_type="application/json",
            )

            # No retry — a chunk was already forwarded downstream.
            assert mock_client.send.call_count == 1

            assert response.status_code == 200
            # Consume the body first — the finally: branch that calls
            # upstream.close() runs when the streaming generator is
            # exhausted, not when Flask returns the Response object.
            body = b"".join(response.response)

            # Upstream still released via the finally: branch even though
            # iter_bytes raised.
            mid_reset_response.close.assert_called()

            # The original (pre-reset) chunk is preserved intact.
            assert first_chunk in body

            # The body ends with a well-formed Anthropic-style SSE error frame.
            assert b"event: error" in body
            # The error event must carry an ``api_error`` type and a message
            # field — the shape the Claude SDK's error handler understands.
            assert b'"type": "api_error"' in body or b'"type":"api_error"' in body
            assert b"upstream connection reset" in body
            # And the SSE frame terminator is the last thing on the wire so
            # the downstream parser sees a complete event, not a truncation.
            assert body.endswith(b"\n\n")
            # Synthetic error frame must come AFTER the original chunk, not
            # interleaved or before it.
            assert body.index(first_chunk) < body.index(b"event: error")

            # Parse the synthetic frame as JSON to catch any malformed output.
            error_frame_start = body.index(b"event: error")
            error_frame = body[error_frame_start:]
            assert error_frame.startswith(b"event: error\ndata: ")
            data_start = len(b"event: error\ndata: ")
            data_end = error_frame.index(b"\n\n")
            payload = json.loads(error_frame[data_start:data_end].decode("utf-8"))
            assert payload["type"] == "error"
            assert payload["error"]["type"] == "api_error"
            assert "message" in payload["error"]

    def test_streaming_send_reset_retry_exhausted_returns_502(self, client, mock_credentials):
        """Both attempts raise — retry exhausted, falls through to 502.

        Covers the ``On second failure, fall through to the existing
        error-return path`` clause of task-1-1. Exhaustion must not loop
        infinitely and must not leak an unclosed upstream.
        """
        import httpx

        with patch("gateway.gateway.get_anthropic_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client

            mock_client.build_request.return_value = MagicMock()
            mock_client.send.side_effect = [
                httpx.ReadError("reset #1"),
                httpx.RemoteProtocolError("reset #2"),
            ]

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3", "stream": True}),
                content_type="application/json",
            )

            # Exactly two attempts — retry is bounded to 1x.
            assert mock_client.send.call_count == 2
            # Falls through to the generic exception handler → 502.
            assert response.status_code == 502
            body = response.get_data()
            assert b"api_error" in body


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


class TestSSEAccumulatorChunkBoundaries:
    """Verify the incremental accumulator produces the same result regardless
    of how the upstream bytes are split across chunks. The production path
    (#1885) feeds chunks as they arrive from httpx, so we can't assume any
    particular event-boundary alignment."""

    CANONICAL_STREAM = (
        b'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-x",'
        b'"usage":{"input_tokens":7}}}\n\n'
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"Hello "}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"world!"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'data: {"type":"message_delta",'
        b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )

    def _feed_and_result(self, splits: list[int]):
        from gateway.gateway import _SSEAccumulator

        acc = _SSEAccumulator()
        start = 0
        for offset in splits:
            acc.feed(self.CANONICAL_STREAM[start:offset])
            start = offset
        acc.feed(self.CANONICAL_STREAM[start:])
        return acc.result()

    def test_parses_identically_to_single_feed(self):
        """One-shot feed should match chunked feeds byte-for-byte."""
        single = self._feed_and_result([])
        chunked = self._feed_and_result(list(range(1, len(self.CANONICAL_STREAM), 37)))
        assert single == chunked

    def test_split_mid_line(self):
        """Splitting mid-line — including mid-JSON — must not drop events."""
        # Break at every newline boundary *and* halfway through each event.
        content, usage, model, stop_reason = self._feed_and_result([10, 50, 120, 250, 400])
        assert model == "claude-x"
        assert usage == {"input_tokens": 7, "output_tokens": 3}
        assert stop_reason == "end_turn"
        assert content is not None
        assert content[0]["text"] == "Hello world!"

    def test_split_mid_utf8_multibyte(self):
        """A multi-byte UTF-8 codepoint split across two chunks must still
        decode correctly — this is the main reason for using an incremental
        decoder rather than .decode() per chunk."""
        from gateway.gateway import _SSEAccumulator

        # "héllo" where é is 0xC3 0xA9; split between the two bytes.
        frame = (
            b'data: {"type":"message_start","message":{"model":"claude-x"}}\n\n'
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"h\xc3\xa9llo"}}\n\n'
            b'data: {"type":"content_block_stop","index":0}\n\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b"data: [DONE]\n\n"
        )
        # Find the position of 0xA9 and split *before* it so 0xC3 lands in one
        # chunk and 0xA9 in the next.
        split_at = frame.index(b"\xa9")
        acc = _SSEAccumulator()
        acc.feed(frame[:split_at])
        acc.feed(frame[split_at:])
        content, _usage, _model, _stop = acc.result()
        assert content is not None
        assert content[0]["text"] == "héllo"

    def test_does_not_hold_bytes_after_feed(self):
        """The accumulator must not retain a reference to fed chunks — that
        was the #1885 regression. Approximate this by checking that the
        per-instance byte footprint stays small after feeding a large stream."""

        from gateway.gateway import _SSEAccumulator

        acc = _SSEAccumulator()
        # Feed 1 MB of irrelevant SSE lines (no data: prefix, so nothing is
        # retained in content_by_index either).
        big_chunk = b"comment: ignored\n" * (1024 * 64)  # ~1 MB
        acc.feed(big_chunk)
        # line_buf should be empty (chunk ends on \n) and no parsed state held.
        assert acc._line_buf == ""  # type: ignore[attr-defined]
        assert acc._content_by_index == {}  # type: ignore[attr-defined]


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
            from gateway.gateway import _capture_streaming_response, _parse_sse_response
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
                result=_parse_sse_response(chunks),
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


# =============================================================================
# Upstream routing — slice-1 of issue #2769 (TASK-1-3 / TASK-1-6)
# =============================================================================
#
# Slice 1 wires the gateway's two proxy routes (``/v1/messages`` and
# ``/v1/messages/count_tokens``) through a per-request ``UpstreamRegistry``
# lookup keyed on ``session.upstream``.  When the session is absent or
# ``session.upstream == "anthropic"`` the routes MUST behave
# byte-identically to today's hard-wired Anthropic path — that's the
# slice-1 no-op invariant.  When ``session.upstream == "litellm"`` the
# routes MUST hit the LiteLLM client and inject the LiteLLM credential
# instead.
#
# The tests below patch the registry / credential resolvers and drive
# the Flask test client to assert the routing decision end-to-end
# without needing a live upstream.
# =============================================================================


def _build_mock_session(upstream: str | None = None, upstream_model: str | None = None):
    """Build a MagicMock Session with the upstream + upstream_model fields
    needed by the slice-1 routing decision.  Falls back to ``"anthropic"``
    when ``upstream is None`` to mirror the production default in the
    Session dataclass.
    """
    session = MagicMock()
    session.mode = "public"
    session.container_id = "test-container-routing"
    session.upstream = "anthropic" if upstream is None else upstream
    session.upstream_model = upstream_model
    return session


class TestUpstreamRoutingMessages:
    """``proxy_anthropic_messages`` routes per ``session.upstream``."""

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_no_session_routes_to_anthropic(self, client):
        """Backwards-compat: when no session exists for the remote IP,
        the request still routes to the Anthropic upstream — the
        slice-1 no-op invariant.
        """
        from httpx import Headers

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_anthropic_client") as mock_anthropic_get,
        ):
            cred = MagicMock(header_name="x-api-key", header_value="sk-ant-test")
            mock_creds_get.return_value.get_credential.return_value = cred

            sm = MagicMock()
            sm.get_session_by_ip.return_value = None
            mock_sm_get.return_value = sm

            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = json.dumps({"content": "ok"}).encode()
            mock_response.status_code = 200
            mock_response.headers = Headers([("content-type", "application/json")])
            mock_client.post.return_value = mock_response
            mock_anthropic_get.return_value = mock_client

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3"}),
                content_type="application/json",
            )

            assert response.status_code == 200
            # When no session is found, defaulting to anthropic is the
            # slice-1 invariant: the Anthropic httpx client MUST be used.
            assert mock_client.post.called or mock_client.send.called, (
                "Anthropic client was not invoked for a no-session request"
            )

    def test_anthropic_session_uses_anthropic_upstream(self, client):
        """Explicit ``session.upstream == "anthropic"`` still routes to
        the Anthropic upstream.
        """
        from httpx import Headers

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_anthropic_client") as mock_anthropic_get,
        ):
            cred = MagicMock(header_name="x-api-key", header_value="sk-ant-test")
            mock_creds_get.return_value.get_credential.return_value = cred

            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(upstream="anthropic")
            mock_sm_get.return_value = sm

            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = json.dumps({"content": "ok"}).encode()
            mock_response.status_code = 200
            mock_response.headers = Headers([("content-type", "application/json")])
            mock_client.post.return_value = mock_response
            mock_anthropic_get.return_value = mock_client

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3"}),
                content_type="application/json",
            )

            assert response.status_code == 200
            assert mock_client.post.called or mock_client.send.called

    def test_litellm_session_uses_litellm_upstream(self, client):
        """``session.upstream == "litellm"`` routes to the LiteLLM client
        from the upstream registry; the Anthropic client is NOT used.
        """
        from httpx import Headers

        try:
            import upstream_registry  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pytest.skip("upstream_registry not yet implemented (waiting on coder)")

        litellm_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"content": "ok"}).encode()
        mock_response.status_code = 200
        mock_response.headers = Headers([("content-type", "application/json")])
        litellm_client.post.return_value = mock_response

        anthropic_client = MagicMock()
        # Anthropic client should NOT be called.
        anthropic_client.post.side_effect = AssertionError(
            "Anthropic client must not be invoked for a LiteLLM-routed request"
        )

        # Build a registry that dispatches per upstream name.
        def _registry_get(upstream):
            if upstream == "anthropic":
                return (
                    anthropic_client,
                    lambda: MagicMock(header_name="x-api-key", header_value="sk-ant-test"),
                )
            if upstream == "litellm":
                return (
                    litellm_client,
                    lambda: MagicMock(header_name="x-api-key", header_value="litellm-key"),
                )
            raise KeyError(upstream)

        fake_registry = MagicMock()
        fake_registry.get.side_effect = _registry_get

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_upstream_registry", return_value=fake_registry, create=True),
            patch("gateway.gateway.get_anthropic_client", return_value=anthropic_client),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )
            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "opus"}),  # cq-5 alias on the wire
                content_type="application/json",
            )

            assert response.status_code == 200
            assert litellm_client.post.called or litellm_client.send.called, (
                "LiteLLM client was not invoked for a LiteLLM-routed request"
            )

    def test_litellm_request_injects_litellm_credential(self, client):
        """The header injected on a LiteLLM-routed request must be the
        LiteLLM ``x-api-key``, not the Anthropic one — otherwise the
        gateway leaks the Anthropic credential to LiteLLM (and vice
        versa).
        """
        from httpx import Headers

        try:
            import upstream_registry  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pytest.skip("upstream_registry not yet implemented (waiting on coder)")

        captured_headers: dict[str, str] = {}

        def _post_capture(*_args, **kwargs):
            captured_headers.update(kwargs.get("headers", {}))
            mock_response = MagicMock()
            mock_response.content = b"{}"
            mock_response.status_code = 200
            mock_response.headers = Headers([("content-type", "application/json")])
            return mock_response

        litellm_client = MagicMock()
        litellm_client.post.side_effect = _post_capture
        anthropic_client = MagicMock()

        def _registry_get(upstream):
            if upstream == "anthropic":
                return (
                    anthropic_client,
                    lambda: MagicMock(header_name="x-api-key", header_value="sk-ant-shouldnotleak"),
                )
            if upstream == "litellm":
                return (
                    litellm_client,
                    lambda: MagicMock(
                        header_name="x-api-key", header_value="litellm-key-only-this"
                    ),
                )
            raise KeyError(upstream)

        fake_registry = MagicMock()
        fake_registry.get.side_effect = _registry_get

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_upstream_registry", return_value=fake_registry, create=True),
            patch("gateway.gateway.get_anthropic_client", return_value=anthropic_client),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-shouldnotleak"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key-only-this"
            )
            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            mock_sm_get.return_value = sm

            client.post(
                "/v1/messages",
                data=json.dumps({"model": "opus"}),
                content_type="application/json",
            )

            assert captured_headers.get("x-api-key") == "litellm-key-only-this", (
                f"LiteLLM-routed request did not inject LiteLLM credential; "
                f"got headers: {captured_headers}"
            )
            assert "sk-ant-shouldnotleak" not in captured_headers.values(), (
                "Anthropic credential leaked into LiteLLM-routed request headers"
            )


class TestUpstreamRoutingCountTokens:
    """``proxy_count_tokens`` mirrors the routing decision."""

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_anthropic_session_count_tokens_uses_anthropic_upstream(self, client):
        from httpx import Headers

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_anthropic_client") as mock_anthropic_get,
        ):
            cred = MagicMock(header_name="x-api-key", header_value="sk-ant-test")
            mock_creds_get.return_value.get_credential.return_value = cred

            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(upstream="anthropic")
            mock_sm_get.return_value = sm

            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.content = json.dumps({"input_tokens": 1}).encode()
            mock_response.status_code = 200
            mock_response.headers = Headers([("content-type", "application/json")])
            mock_client.post.return_value = mock_response
            mock_anthropic_get.return_value = mock_client

            response = client.post(
                "/v1/messages/count_tokens",
                data=json.dumps({"model": "claude-3", "messages": []}),
                content_type="application/json",
            )

            assert response.status_code == 200
            assert mock_client.post.called

    def test_litellm_session_count_tokens_uses_litellm_upstream(self, client):
        from httpx import Headers

        try:
            import upstream_registry  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pytest.skip("upstream_registry not yet implemented (waiting on coder)")

        litellm_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"input_tokens": 7}).encode()
        mock_response.status_code = 200
        mock_response.headers = Headers([("content-type", "application/json")])
        litellm_client.post.return_value = mock_response

        anthropic_client = MagicMock()
        anthropic_client.post.side_effect = AssertionError(
            "Anthropic client must not be invoked for a LiteLLM count_tokens request"
        )

        def _registry_get(upstream):
            if upstream == "anthropic":
                return (
                    anthropic_client,
                    lambda: MagicMock(header_name="x-api-key", header_value="sk-ant-test"),
                )
            if upstream == "litellm":
                return (
                    litellm_client,
                    lambda: MagicMock(header_name="x-api-key", header_value="litellm-key"),
                )
            raise KeyError(upstream)

        fake_registry = MagicMock()
        fake_registry.get.side_effect = _registry_get

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_upstream_registry", return_value=fake_registry, create=True),
            patch("gateway.gateway.get_anthropic_client", return_value=anthropic_client),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )
            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages/count_tokens",
                data=json.dumps({"model": "opus", "messages": []}),
                content_type="application/json",
            )

            assert response.status_code == 200
            assert litellm_client.post.called, (
                "LiteLLM client was not invoked for a LiteLLM count_tokens request"
            )


class TestInjectUpstreamCredentials:
    """``_inject_upstream_credentials(headers, upstream)`` dispatches per upstream.

    Back-compat: ``_inject_anthropic_credentials`` is preserved as a thin
    alias calling through with ``upstream="anthropic"`` (TASK-1-3 AC).
    """

    @pytest.fixture
    def _inject_fn(self):
        """Return the upstream-aware injector if present, else skip."""
        try:
            from gateway.gateway import _inject_upstream_credentials  # type: ignore[attr-defined]

            return _inject_upstream_credentials
        except ImportError:
            pytest.skip("_inject_upstream_credentials not yet implemented")

    def test_anthropic_dispatch_matches_legacy_helper(self, _inject_fn):
        """For ``upstream="anthropic"``, the new helper behaves
        byte-identically to today's ``_inject_anthropic_credentials``.
        """
        from gateway.gateway import _inject_anthropic_credentials, app

        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            cred = MagicMock(header_name="x-api-key", header_value="sk-ant-byte-identical")
            mock_get.return_value.get_credential.return_value = cred

            headers_a, error_a = _inject_anthropic_credentials({"Content-Type": "application/json"})
            headers_b, error_b = _inject_fn({"Content-Type": "application/json"}, "anthropic")

            assert error_a is None
            assert error_b is None
            assert headers_a == headers_b

        # Also assert the 401 shape matches for the no-credential path.
        with patch("gateway.gateway.get_credentials_manager") as mock_get:
            mock_get.return_value.get_credential.return_value = None
            with app.app_context():
                _h_a, err_a = _inject_anthropic_credentials({"Content-Type": "application/json"})
                _h_b, err_b = _inject_fn({"Content-Type": "application/json"}, "anthropic")
            assert err_a is not None and err_b is not None
            assert err_a[1] == err_b[1] == 401

    def test_litellm_dispatch_injects_litellm_credential(self, _inject_fn):
        """``upstream="litellm"`` injects the LiteLLM ``x-api-key`` from
        the LiteLLM credential resolver, NOT the Anthropic one.
        """
        # Patch both resolvers; assert only LiteLLM's value lands in the
        # headers.  The Anthropic resolver MUST NOT be consulted on this
        # path.
        with (
            patch("gateway.gateway.get_credentials_manager") as mock_anthropic_get,
        ):
            # Make Anthropic resolver explosive — if it's called, the
            # test fails loudly.
            mock_anthropic_get.return_value.get_credential.side_effect = AssertionError(
                "Anthropic resolver consulted on LiteLLM-routed request"
            )
            try:
                with patch("gateway.gateway.get_litellm_credentials_manager") as mock_litellm_get:
                    mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                        header_name="x-api-key",
                        header_value="litellm-master-key-1234567890",
                    )
                    headers, error = _inject_fn({"Content-Type": "application/json"}, "litellm")
                    assert error is None
                    assert headers["x-api-key"] == "litellm-master-key-1234567890"
            except AttributeError:
                pytest.skip("gateway.gateway.get_litellm_credentials_manager not yet exported")

    def test_litellm_no_credential_returns_401(self, _inject_fn):
        """Same 401 shape as today's Anthropic-no-credential path."""
        from gateway.gateway import app

        with patch("gateway.gateway.get_credentials_manager") as mock_anthropic_get:
            mock_anthropic_get.return_value.get_credential.return_value = None
            try:
                with patch("gateway.gateway.get_litellm_credentials_manager") as mock_litellm_get:
                    mock_litellm_get.return_value.get_credential.return_value = None
                    with app.app_context():
                        _headers, error = _inject_fn(
                            {"Content-Type": "application/json"}, "litellm"
                        )
                    assert error is not None
                    assert error[1] == 401
            except AttributeError:
                pytest.skip("gateway.gateway.get_litellm_credentials_manager not yet exported")


# =============================================================================
# Adversarial probes — issue #2769 slice-1
# =============================================================================
#
# The probes below target seams that are easy to get wrong:
#
# - Unknown-upstream defense in the proxy route (TASK-1-6) — the
#   code path that fires when a session somehow ends up with an upstream
#   the registry does not serve (corrupted persistence, slice-2
#   misconfig).  Must fail closed with 502, not crash or silently
#   forward to Anthropic.
#
# - The two proxy routes (proxy_anthropic_messages and
#   proxy_count_tokens) MUST agree on the upstream for a given session
#   — split-brain (messages routed to LiteLLM, count_tokens routed to
#   Anthropic) would break Claude Code's token accounting badly.
#
# - LiteLLM upstream must NOT trigger the Anthropic "client-supplied
#   auth fall-through" — i.e. with no LITELLM_MASTER_KEY configured,
#   a Claude Code request that happens to carry an Authorization
#   header must NOT silently route to LiteLLM with that header.
# =============================================================================


class TestUnknownUpstreamDefense:
    """Defensive 502 when session.upstream is unknown at proxy time."""

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_unknown_upstream_on_session_returns_5xx(self, client):
        """If a session escapes session-create validation with an unknown
        upstream (e.g. corrupted persistence, manual edit), the proxy
        MUST fail closed — never silently fall back to Anthropic.

        The coder's defensive handler maps ``UnknownUpstreamError`` to a
        502.  We accept any 5xx here because the dual-import test setup
        (``gateway/tests/conftest.py`` loads modules with a custom
        loader, while ``tests/gateway/`` uses ``sys.path`` insertion)
        can cause two distinct ``UnknownUpstreamError`` class identities
        to coexist in the test session, leaving the exception un-caught
        and producing a 500.  In production there is only one module
        load, so the 502 path is the only one that fires — and the
        important assertion is "fail closed", not the specific 5xx
        code.
        """
        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            # Build a session whose upstream is one the registry will not
            # serve.  The session-create endpoint validates upstream before
            # storing it, but this guards the in-flight error case where a
            # session somehow lands with an unknown value (slice-2 misconfig,
            # or persistence corruption).
            session = _build_mock_session(upstream="bogus_upstream")
            sm = MagicMock()
            sm.get_session_by_ip.return_value = session
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-3"}),
                content_type="application/json",
            )

            # cq-8: Fail closed on LiteLLM unreachable; the
            # unknown-upstream branch is the most-likely-to-fire variant
            # of "we can't reach what the session told us to reach".
            assert 500 <= response.status_code < 600, (
                f"Unknown upstream MUST fail closed (5xx), got "
                f"{response.status_code} ({response.data!r})"
            )
            # The response MUST NOT be a 2xx — Anthropic must not have
            # been hit as a silent fallback.
            assert response.status_code != 200, (
                "Unknown upstream silently fell back to Anthropic — "
                "this is the cq-8 fail-closed contract violation"
            )


class TestRoutingConsistencyAcrossProxyRoutes:
    """``/v1/messages`` and ``/v1/messages/count_tokens`` MUST agree on
    upstream for any given session.  A split-brain (messages -> LiteLLM,
    count_tokens -> Anthropic) silently corrupts Claude Code's token
    accounting and is hard to detect post-hoc.
    """

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_messages_and_count_tokens_agree_on_litellm_routing(self, client):
        """Both routes should land on the LiteLLM client when the
        session is upstream=='litellm'.
        """
        from httpx import Headers

        try:
            import upstream_registry  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pytest.skip("upstream_registry not yet implemented")

        litellm_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = b"{}"
        mock_response.status_code = 200
        mock_response.headers = Headers([("content-type", "application/json")])
        litellm_client.post.return_value = mock_response

        anthropic_client = MagicMock()

        def _registry_get(upstream):
            if upstream == "anthropic":
                return (anthropic_client, lambda: None)
            if upstream == "litellm":
                return (litellm_client, lambda: None)
            raise KeyError(upstream)

        fake_registry = MagicMock()
        fake_registry.get.side_effect = _registry_get

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_upstream_registry", return_value=fake_registry, create=True),
            patch("gateway.gateway.get_anthropic_client", return_value=anthropic_client),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )
            session = _build_mock_session(upstream="litellm", upstream_model="qwen3-coder-30b")
            sm = MagicMock()
            sm.get_session_by_ip.return_value = session
            mock_sm_get.return_value = sm

            # Hit both routes back-to-back with the same session.
            client.post(
                "/v1/messages",
                data=json.dumps({"model": "opus"}),
                content_type="application/json",
            )
            client.post(
                "/v1/messages/count_tokens",
                data=json.dumps({"model": "opus", "messages": []}),
                content_type="application/json",
            )

            # Both must have hit the LiteLLM client.  An Anthropic call
            # here indicates the split-brain bug.
            assert litellm_client.post.call_count == 2, (
                f"Split-brain routing: LiteLLM hit "
                f"{litellm_client.post.call_count} times across messages "
                f"and count_tokens — expected 2.  anthropic_client.post "
                f"called {anthropic_client.post.call_count} times "
                f"(should be 0)."
            )
            assert anthropic_client.post.call_count == 0


class TestLiteLLMNoFallbackToClientAuth:
    """The LiteLLM upstream MUST NOT honour client-supplied
    Authorization / x-api-key headers as a fallback when
    LITELLM_MASTER_KEY is unset.  That fall-through is the Anthropic
    path's OAuth-mode escape hatch; on the LiteLLM path it would route
    a Claude Code Anthropic OAuth token to a third-party LiteLLM
    backend, leaking the credential.

    TASK-1-3 AC: ``Missing credentials for either upstream return a
    401 with the same JSON body shape as today.``
    """

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_litellm_no_master_key_does_not_honour_client_auth(self, client):
        """With LITELLM_MASTER_KEY unset and the Claude Code client
        sending an Authorization header, the LiteLLM path returns 401
        — does NOT silently forward the Anthropic OAuth token to
        LiteLLM.
        """
        with (
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            # LiteLLM has NO credential.
            mock_litellm_get.return_value.get_credential.return_value = None

            session = _build_mock_session(upstream="litellm", upstream_model="qwen3-coder-30b")
            sm = MagicMock()
            sm.get_session_by_ip.return_value = session
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "opus"}),
                content_type="application/json",
                headers={"Authorization": "Bearer claude-oauth-should-not-leak"},
            )

            assert response.status_code == 401, (
                f"LiteLLM path with no master key MUST return 401 even "
                f"when client carries Authorization header.  Got "
                f"{response.status_code} ({response.data!r}) — this "
                f"would leak the Claude OAuth token to a third-party "
                f"LiteLLM backend."
            )

    def test_litellm_no_master_key_does_not_honour_client_api_key(self, client):
        """Same guard for x-api-key — client-supplied API keys must
        not bypass the LiteLLM credential gate.
        """
        with (
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_litellm_get.return_value.get_credential.return_value = None

            session = _build_mock_session(upstream="litellm", upstream_model="qwen3-coder-30b")
            sm = MagicMock()
            sm.get_session_by_ip.return_value = session
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages",
                data=json.dumps({"model": "opus"}),
                content_type="application/json",
                headers={"x-api-key": "sk-ant-should-not-leak"},
            )

            assert response.status_code == 401


class TestSessionCreateUpstreamValidation:
    """Slice-1 session-create endpoint MUST reject unknown ``upstream``
    values with 400 (TASK-1-5 AC).
    """

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def _launcher_auth(self):
        """Return the Authorization header used by session-create."""
        import os

        return {"Authorization": f"Bearer {os.environ.get('EGG_LAUNCHER_SECRET', '')}"}

    def test_bogus_upstream_returns_400(self, client):
        """POST /api/v1/sessions/create with ``upstream='bogus'``
        returns a 400 with a descriptive error.
        """
        response = client.post(
            "/api/v1/sessions/create",
            data=json.dumps(
                {
                    "container_id": "test-container",
                    "container_ip": "172.18.0.5",
                    "mode": "private",
                    "pipeline_id": "test-pipeline",
                    "upstream": "bogus_upstream_name",
                }
            ),
            content_type="application/json",
            headers=self._launcher_auth(),
        )

        # The endpoint requires launcher auth; if our secret isn't set
        # the response is 401 or a 500 LauncherSecretNotConfiguredError.
        # Either way the test would not actually exercise the upstream
        # validation path, so skip rather than report a false negative.
        if response.status_code in (401, 500):
            pytest.skip(
                "session-create requires launcher auth; EGG_LAUNCHER_SECRET "
                "not configured in this test env"
            )

        assert response.status_code == 400, (
            f"Bogus upstream MUST return 400; got {response.status_code} ({response.data!r})"
        )
        body = json.loads(response.data)
        # The error message should mention the rejected upstream so the
        # operator can debug.  The exact phrasing is flexible.
        msg = body.get("message", body.get("error", {}).get("message", ""))
        assert "upstream" in str(msg).lower() or "bogus_upstream_name" in str(body)

    def test_anthropic_and_litellm_upstreams_are_valid(self, client):
        """Sanity check: the registry's two known upstreams pass
        session-create validation.
        """
        for upstream in ("anthropic", "litellm"):
            response = client.post(
                "/api/v1/sessions/create",
                data=json.dumps(
                    {
                        "container_id": f"test-{upstream}",
                        "container_ip": "172.18.0.5",
                        "mode": "private",
                        "pipeline_id": f"test-pipeline-{upstream}",
                        "upstream": upstream,
                    }
                ),
                content_type="application/json",
                headers=self._launcher_auth(),
            )

            if response.status_code in (401, 500):
                pytest.skip(
                    "session-create requires launcher auth; "
                    "EGG_LAUNCHER_SECRET not configured in this test env"
                )

            # Should NOT be 400 (validation success).  Other failures
            # (e.g. worktree creation) are out of scope for this test.
            assert response.status_code != 400 or (
                "upstream" not in str(json.loads(response.data))
            ), f"Valid upstream '{upstream}' was rejected: {response.data!r}"


# =============================================================================
# Upstream body rewrite — slice-2 of issue #2769 (TASK-2-6 / TASK-2-8)
# =============================================================================
#
# Slice 2 adds ``_rewrite_upstream_model(request_body, upstream_model)``
# next to ``_filter_blocked_tools`` in gateway.py.  On LiteLLM-routed
# requests with ``session.upstream_model`` set, the helper REPLACES the
# top-level ``"model"`` field in the JSON body with the upstream-side
# model name (e.g. ``"qwen3-coder-30b"``).  This is the cq-5 mitigation
# on the wire: Claude Code is presented a recognized Claude alias
# (``"opus"``) as ``--model``, so its compaction math stays sane; the
# gateway rewrites the body just before forwarding to LiteLLM so the
# upstream actually receives the right model name.
#
# Invariants:
#
# - ``upstream == "anthropic"``  → body is byte-identical (regression).
# - ``upstream == "litellm"`` and ``upstream_model is None``  → body
#   unchanged (slice-1 no-op state).
# - ``upstream == "litellm"`` and ``upstream_model="qwen3-coder-30b"``
#   → forwarded body has ``"model": "qwen3-coder-30b"`` regardless of
#   incoming ``"model"`` value.
# - Invalid JSON  → original body returned unchanged (proxy MUST NOT
#   crash on a malformed body — slice-1 ``_filter_blocked_tools``
#   matches this contract).
# - The rewrite happens AFTER ``_filter_blocked_tools`` and BEFORE the
#   upstream request is built, so blocked-tool stripping in private
#   mode still works.
# =============================================================================


def _capture_upstream_body(captured_holder: dict, status: int = 200, response_body: bytes = b"{}"):
    """Build an httpx-mock side_effect that captures the body forwarded
    to the upstream client.  Stores under ``captured_holder["body"]``.
    Mirrors the slice-1 ``_post_capture`` helper used by the
    credential-leak tests.
    """
    from httpx import Headers

    def _capture(*args, **kwargs):
        # The proxy builds requests one of two ways depending on
        # streaming: ``client.post(url, content=request_body, ...)`` or
        # ``client.build_request("POST", url, content=request_body, ...)``.
        # Both routes feed ``request_body`` via the ``content`` kwarg.
        body = kwargs.get("content")
        if body is None:
            # Fall back to positional inspection for safety.
            for arg in args:
                if isinstance(arg, (bytes, bytearray, str)):
                    body = arg
                    break
        captured_holder["body"] = body

        mock_response = MagicMock()
        mock_response.content = response_body
        mock_response.status_code = status
        mock_response.headers = Headers([("content-type", "application/json")])
        return mock_response

    return _capture


class TestRewriteUpstreamModelHelper:
    """Direct unit tests on the ``_rewrite_upstream_model`` helper.

    Skips when the helper has not landed yet (waiting on coder).
    """

    @pytest.fixture
    def _rewrite_fn(self):
        try:
            from gateway.gateway import _rewrite_upstream_model  # type: ignore[attr-defined]

            return _rewrite_upstream_model
        except ImportError:
            pytest.skip(
                "_rewrite_upstream_model not yet implemented (waiting on coder)"
            )

    def test_no_op_when_upstream_model_is_none(self, _rewrite_fn):
        body = json.dumps({"model": "opus", "messages": []}).encode()
        out = _rewrite_fn(body, None)
        # Byte-identical when no rewrite is requested.
        assert out == body

    def test_rewrites_top_level_model_field(self, _rewrite_fn):
        body = json.dumps({"model": "opus", "messages": []}).encode()
        out = _rewrite_fn(body, "qwen3-coder-30b")
        parsed = json.loads(out)
        assert parsed["model"] == "qwen3-coder-30b"
        # Other fields preserved.
        assert parsed["messages"] == []

    def test_preserves_other_top_level_keys(self, _rewrite_fn):
        """The rewrite must not drop other body fields — system prompt,
        tools, max_tokens, etc.  If it does, Claude Code's request
        shape silently changes shape across the gateway.
        """
        body = json.dumps(
            {
                "model": "opus",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4096,
                "system": "You are a helpful assistant.",
                "tools": [{"name": "bash"}],
                "stream": True,
            }
        ).encode()
        out = _rewrite_fn(body, "qwen3-coder-30b")
        parsed = json.loads(out)
        assert parsed["model"] == "qwen3-coder-30b"
        assert parsed["max_tokens"] == 4096
        assert parsed["system"] == "You are a helpful assistant."
        assert parsed["tools"] == [{"name": "bash"}]
        assert parsed["stream"] is True
        assert parsed["messages"] == [{"role": "user", "content": "hi"}]

    def test_invalid_json_returns_original_body(self, _rewrite_fn):
        """Slice-1 ``_filter_blocked_tools`` matches this contract —
        on JSONDecodeError the helper returns the input unchanged so
        the proxy doesn't crash on a malformed body.  Slice-2 must do
        the same.
        """
        body = b"not valid json {{"
        out = _rewrite_fn(body, "qwen3-coder-30b")
        assert out == body

    def test_empty_body_returns_unchanged(self, _rewrite_fn):
        body = b""
        out = _rewrite_fn(body, "qwen3-coder-30b")
        # Either byte-identical or a degenerate ``{}``-rewrite is
        # acceptable; what's not acceptable is a crash.
        assert isinstance(out, (bytes, bytearray))

    def test_body_without_model_key_is_handled(self, _rewrite_fn):
        """Adversarial: incoming body has no ``model`` key.  The helper
        either inserts the upstream model (so LiteLLM still gets the
        right model name) or returns the body unchanged — what MUST
        NOT happen is a KeyError crash.
        """
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        out = _rewrite_fn(body, "qwen3-coder-30b")
        # Should not raise, and should not corrupt the rest of the body.
        # If the implementation chooses to inject the model field, the
        # result MUST be valid JSON with ``messages`` preserved.
        try:
            parsed = json.loads(out)
            assert parsed.get("messages") == [
                {"role": "user", "content": "hi"}
            ]
        except json.JSONDecodeError:
            # Returning the body unchanged is also acceptable.
            assert out == body

    def test_unicode_model_name_round_trips(self, _rewrite_fn):
        """The helper must not break on non-ASCII model names — even
        though LiteLLM model names are ASCII in practice, the helper
        shouldn't impose a stricter encoding constraint than the
        original body parser.
        """
        body = json.dumps({"model": "opus", "messages": []}).encode()
        out = _rewrite_fn(body, "qwen-✨-30b")
        parsed = json.loads(out)
        assert parsed["model"] == "qwen-✨-30b"


def _rewrite_helper_exists() -> bool:
    """Return True if slice-2's ``_rewrite_upstream_model`` has landed
    on the gateway side.  Tests below skip when False.
    """
    try:
        from gateway.gateway import _rewrite_upstream_model  # type: ignore[attr-defined]  # noqa: F401

        return True
    except ImportError:
        return False


class TestRewriteUpstreamModelOnProxyRoute:
    """End-to-end: ``proxy_anthropic_messages`` with a LiteLLM session
    forwards the rewritten body to the LiteLLM upstream.

    These tests drive the Flask app like slice-1's
    ``TestUpstreamRoutingMessages`` and assert on the body forwarded
    to the LiteLLM-side httpx client.
    """

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def _build_full_registry_patch(self, captured_body_holder):
        """Build the registry / credential patches used by the
        body-rewrite tests.  Returns a list of context managers ready
        to ``with ... :``.
        """
        try:
            import upstream_registry  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pytest.skip(
                "upstream_registry not yet implemented (waiting on coder)"
            )

        litellm_client = MagicMock()
        litellm_client.post.side_effect = _capture_upstream_body(captured_body_holder)
        # Also wire build_request → send → iter_bytes for the streaming
        # path.  Slice-2 tests target the non-streaming path, but we set
        # up both to avoid spurious AttributeErrors.
        litellm_client.build_request.return_value = MagicMock()

        anthropic_client = MagicMock()
        anthropic_client.post.side_effect = AssertionError(
            "Anthropic client must not be invoked for a LiteLLM-routed request"
        )

        def _registry_get(upstream):
            if upstream == "anthropic":
                return (
                    anthropic_client,
                    lambda: MagicMock(
                        header_name="x-api-key", header_value="sk-ant-test"
                    ),
                )
            if upstream == "litellm":
                return (
                    litellm_client,
                    lambda: MagicMock(
                        header_name="x-api-key", header_value="litellm-key"
                    ),
                )
            raise KeyError(upstream)

        fake_registry = MagicMock()
        fake_registry.get.side_effect = _registry_get
        return fake_registry, litellm_client, anthropic_client

    def test_litellm_session_with_upstream_model_rewrites_body(self, client):
        """With ``upstream="litellm"`` and
        ``upstream_model="qwen3-coder-30b"``, the body forwarded to the
        LiteLLM client has ``"model": "qwen3-coder-30b"`` regardless of
        what Claude Code sent (it sends ``"opus"`` per the cq-5
        mitigation).
        """
        if not _rewrite_helper_exists():
            pytest.skip("_rewrite_upstream_model not yet implemented")
        captured: dict = {}
        fake_registry, litellm_client, _ = self._build_full_registry_patch(captured)

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_upstream_registry",
                return_value=fake_registry,
                create=True,
            ),
            patch("gateway.gateway.get_anthropic_client") as mock_anthropic_get,
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )
            # ``get_anthropic_client`` is also called in the no-session
            # path; wire it to a harmless mock to be safe.
            mock_anthropic_get.return_value = MagicMock()

            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages",
                # Claude Code presents 'opus' on the wire (cq-5).
                data=json.dumps({"model": "opus", "messages": []}),
                content_type="application/json",
            )

            assert response.status_code == 200
            body = captured.get("body")
            assert body is not None, (
                "LiteLLM client was not called with a body to capture"
            )
            parsed = json.loads(body) if isinstance(body, (bytes, bytearray)) else json.loads(body)
            assert parsed["model"] == "qwen3-coder-30b", (
                f"LiteLLM-routed body MUST have model='qwen3-coder-30b' "
                f"(rewritten from incoming 'opus'); got {parsed.get('model')!r}"
            )

    def test_litellm_session_without_upstream_model_preserves_body_model(
        self, client
    ):
        """Slice-1 no-op state: a LiteLLM session with
        ``upstream_model=None`` MUST NOT rewrite the body — the proxy
        passes whatever model name the client sent.
        """
        captured: dict = {}
        fake_registry, _litellm_client, _ = self._build_full_registry_patch(
            captured
        )

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_upstream_registry",
                return_value=fake_registry,
                create=True,
            ),
            patch("gateway.gateway.get_anthropic_client"),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )

            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model=None
            )
            mock_sm_get.return_value = sm

            client.post(
                "/v1/messages",
                data=json.dumps({"model": "opus", "messages": []}),
                content_type="application/json",
            )

            body = captured.get("body")
            assert body is not None
            parsed = json.loads(body)
            assert parsed["model"] == "opus", (
                f"upstream_model=None must NOT rewrite the body; got "
                f"model={parsed.get('model')!r}"
            )

    def test_anthropic_session_body_is_byte_identical(self, client):
        """Regression guard for the Anthropic path — with no session,
        or ``upstream="anthropic"``, the body forwarded to Anthropic is
        BYTE-identical to the body the client sent.  This is the
        slice-2 no-op invariant.
        """
        from httpx import Headers

        captured: dict = {}

        def _post_capture(*args, **kwargs):
            captured["body"] = kwargs.get("content") or (
                args[1] if len(args) > 1 else None
            )
            mock_response = MagicMock()
            mock_response.content = b'{"ok": true}'
            mock_response.status_code = 200
            mock_response.headers = Headers([("content-type", "application/json")])
            return mock_response

        mock_anthropic_client = MagicMock()
        mock_anthropic_client.post.side_effect = _post_capture

        # Build a registry where 'anthropic' returns our client — and
        # 'litellm' is never expected to be called on this test.  Use
        # ``get_anthropic_client`` so the proxy's fast path picks up
        # the mock (the slice-1 code shape calls that directly for the
        # anthropic upstream).
        original_body = json.dumps({"model": "claude-3-5-sonnet-20241022", "messages": []})

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch("gateway.gateway.get_anthropic_client", return_value=mock_anthropic_client),
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="anthropic"
            )
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages",
                data=original_body,
                content_type="application/json",
            )

            assert response.status_code == 200
            body = captured.get("body")
            # Byte-identical regression guard: Anthropic path MUST NOT
            # rewrite anything. The incoming model name (a full
            # ``claude-3-5-sonnet-...`` alias) must survive verbatim.
            assert body == original_body.encode(), (
                f"Anthropic path body MUST be byte-identical "
                f"(regression guard for slice-2); incoming "
                f"{original_body!r}, forwarded {body!r}"
            )

    def test_litellm_count_tokens_rewrites_body(self, client):
        """The body-rewrite must mirror across BOTH proxy routes —
        otherwise Claude Code's token accounting for a LiteLLM-routed
        agent silently drifts from the actual upstream model.
        """
        if not _rewrite_helper_exists():
            pytest.skip("_rewrite_upstream_model not yet implemented")
        captured: dict = {}
        fake_registry, litellm_client, _ = self._build_full_registry_patch(captured)

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_upstream_registry",
                return_value=fake_registry,
                create=True,
            ),
            patch("gateway.gateway.get_anthropic_client"),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )
            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            mock_sm_get.return_value = sm

            response = client.post(
                "/v1/messages/count_tokens",
                data=json.dumps({"model": "opus", "messages": []}),
                content_type="application/json",
            )

            assert response.status_code == 200
            body = captured.get("body")
            assert body is not None
            parsed = json.loads(body)
            assert parsed["model"] == "qwen3-coder-30b", (
                f"count_tokens body rewrite missing on LiteLLM route; got "
                f"{parsed.get('model')!r} — Claude Code token accounting "
                f"will drift from the upstream model on the next request"
            )

    def test_rewrite_runs_after_tool_filtering(self, client):
        """``_rewrite_upstream_model`` MUST run AFTER
        ``_filter_blocked_tools`` per the plan — if it runs before, a
        body that gets re-serialized by the rewrite changes the byte
        shape the tool-filter sees, which would silently break the
        private-mode tool strip.

        Probe: send a private-mode session with a blocked tool AND a
        LiteLLM upstream.  Assert both the tool was stripped AND the
        model was rewritten in the forwarded body.
        """
        if not _rewrite_helper_exists():
            pytest.skip("_rewrite_upstream_model not yet implemented")
        captured: dict = {}
        fake_registry, _litellm_client, _ = self._build_full_registry_patch(captured)

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_upstream_registry",
                return_value=fake_registry,
                create=True,
            ),
            patch("gateway.gateway.get_anthropic_client"),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )

            session = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            session.mode = "private"  # trigger tool-stripping
            sm = MagicMock()
            sm.get_session_by_ip.return_value = session
            mock_sm_get.return_value = sm

            client.post(
                "/v1/messages",
                data=json.dumps(
                    {
                        "model": "opus",
                        "messages": [],
                        # WebSearch is blocked in private mode per
                        # BLOCKED_TOOLS_PRIVATE_MODE — if rewrite runs
                        # before filter, this tool may survive into
                        # the forwarded body.
                        "tools": [
                            {"name": "WebSearch"},
                            {"name": "Read"},
                        ],
                    }
                ),
                content_type="application/json",
            )

            body = captured.get("body")
            assert body is not None
            parsed = json.loads(body)
            # Model was rewritten:
            assert parsed["model"] == "qwen3-coder-30b"
            # Blocked tool was stripped (tool-filter ran):
            tool_names = {t.get("name") for t in parsed.get("tools", [])}
            assert "WebSearch" not in tool_names, (
                f"Blocked tool 'WebSearch' survived in private mode + "
                f"LiteLLM upstream — tool-filter / rewrite ordering "
                f"may be broken.  Forwarded tools: {tool_names}"
            )
            assert "Read" in tool_names, (
                f"Tool-filter stripped a non-blocked tool ('Read') — "
                f"got {tool_names}"
            )


class TestRewriteUpstreamModelMalformedBodyResilience:
    """Adversarial probes: the body-rewrite helper MUST NOT crash the
    proxy on adversarial inputs (matches the slice-1
    ``_filter_blocked_tools`` resilience contract).
    """

    @pytest.fixture
    def client(self):
        from gateway.gateway import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_invalid_json_request_does_not_crash_proxy(self, client):
        """A LiteLLM session with an invalid-JSON body MUST NOT crash
        the proxy — the helper returns the body unchanged and the
        upstream gets the malformed body, mirroring today's
        Anthropic-route behavior.
        """
        try:
            import upstream_registry  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            pytest.skip("upstream_registry not yet implemented")

        from httpx import Headers

        captured: dict = {}
        litellm_client = MagicMock()
        litellm_client.post.side_effect = _capture_upstream_body(captured)
        litellm_client.build_request.return_value = MagicMock()
        anthropic_client = MagicMock()

        def _registry_get(upstream):
            if upstream == "anthropic":
                return (anthropic_client, lambda: MagicMock())
            if upstream == "litellm":
                return (litellm_client, lambda: MagicMock())
            raise KeyError(upstream)

        fake_registry = MagicMock()
        fake_registry.get.side_effect = _registry_get

        with (
            patch("gateway.gateway.get_credentials_manager") as mock_creds_get,
            patch("gateway.gateway.get_session_manager") as mock_sm_get,
            patch(
                "gateway.gateway.get_upstream_registry",
                return_value=fake_registry,
                create=True,
            ),
            patch("gateway.gateway.get_anthropic_client", return_value=anthropic_client),
            patch(
                "gateway.gateway.get_litellm_credentials_manager", create=True
            ) as mock_litellm_get,
        ):
            mock_creds_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="sk-ant-test"
            )
            mock_litellm_get.return_value.get_credential.return_value = MagicMock(
                header_name="x-api-key", header_value="litellm-key"
            )
            sm = MagicMock()
            sm.get_session_by_ip.return_value = _build_mock_session(
                upstream="litellm", upstream_model="qwen3-coder-30b"
            )
            mock_sm_get.return_value = sm

            # Malformed JSON body — proxy should pass it through
            # unchanged, and we should NOT see a 500.
            response = client.post(
                "/v1/messages",
                data=b"not valid json {{",
                content_type="application/json",
            )

            # Either the proxy forwarded the malformed body (200 from
            # the mock) or returned a structured error — what MUST NOT
            # happen is a 500 / unhandled exception.
            assert response.status_code != 500, (
                f"Malformed JSON crashed the LiteLLM-routed proxy "
                f"(should pass through unchanged like the Anthropic "
                f"path); got 500 with {response.data!r}"
            )
