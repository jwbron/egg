"""
Tests for ``proxy_anthropic_messages`` streaming resilience (issue #1907).

Covers:
    (a) Pre-stream ``httpx.ReadError`` is retried once and transparently
        succeeds — downstream sees only the retry's bytes.
    (b) Pre-stream ``httpx.ReadError`` that persists across retries is
        surfaced as a 502 JSON error payload (not a truncated stream).
    (c) Mid-stream ``httpx.ReadError`` (after bytes have flowed) ends the
        downstream stream with a well-formed SSE ``event: error`` envelope
        and does NOT propagate an uncaught exception into waitress.
    (d) The transcript buffer's ``_capture_streaming_response`` still runs
        with partial accumulator data when the upstream resets mid-stream.

The tests inject a fake ``httpx.Client`` via ``get_anthropic_client()``.
The fake's ``send(req, stream=True)`` returns a synthetic response object
whose ``iter_bytes()`` yields configurable chunks and optionally raises
``httpx.ReadError`` / ``httpx.RemoteProtocolError`` at configurable points.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

# conftest.py loads modules with hyphen-in-path handling. Reuse those.
TEST_LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")

import gateway  # noqa: E402  (loaded via conftest)

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """
    Stand-in for ``httpx.Response`` with ``stream=True``.

    ``chunks`` is the sequence of bytes yielded by ``iter_bytes()``. If
    ``raise_at`` is an int N, the iterator yields the first N chunks and then
    raises ``raise_exc`` (defaults to ``httpx.ReadError``). A ``raise_at`` of
    0 means "raise before yielding any bytes" — emulates a TCP reset that
    hits the pool before the first body byte crosses the gateway boundary.
    """

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        raise_at: int | None = None,
        raise_exc: type[BaseException] = httpx.ReadError,
        raise_message: str = "Server disconnected without sending a response.",
    ) -> None:
        self._chunks = list(chunks or [])
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {"content-type": "text/event-stream"})
        self._raise_at = raise_at
        self._raise_exc = raise_exc
        self._raise_message = raise_message
        self.closed = False

    def iter_bytes(self) -> Any:
        for idx, chunk in enumerate(self._chunks):
            if self._raise_at is not None and idx == self._raise_at:
                raise self._raise_exc(self._raise_message)
            yield chunk
        if self._raise_at is not None and self._raise_at >= len(self._chunks):
            raise self._raise_exc(self._raise_message)

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """
    Stand-in for ``httpx.Client``.

    ``send(req, stream=True)`` pops the next ``_FakeResponse`` off
    ``self.responses``. If the response is an Exception instance, it is
    raised (used to simulate a reset that happens during header read, before
    ``iter_bytes()`` is ever called).
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.sent = 0

    def build_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Request:
        return httpx.Request(
            method, f"https://example.invalid{url}", headers=headers or {}, content=content
        )

    def send(self, request: httpx.Request, stream: bool = False) -> Any:
        self.sent += 1
        if not self.responses:
            raise AssertionError("fake client exhausted: no more responses queued")
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def post(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - not exercised
        raise AssertionError("post() should not be called by streaming tests")


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def flask_client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


@pytest.fixture
def stub_credentials():
    """Stub credential injection so the proxy doesn't 401 under test."""
    mock_cred = MagicMock()
    mock_cred.header_name = "x-api-key"
    mock_cred.header_value = "test-key"

    mock_manager = MagicMock()
    mock_manager.get_credential.return_value = mock_cred

    with patch.object(gateway, "get_credentials_manager", return_value=mock_manager):
        yield


@pytest.fixture
def no_session():
    """No session lookup → container_id=None → skip transcript capture path."""
    mock_sm = MagicMock()
    mock_sm.get_session_by_ip.return_value = None
    with patch.object(gateway, "get_session_manager", return_value=mock_sm):
        yield


@pytest.fixture
def with_session():
    """
    Session lookup returns a session with a container_id, so the transcript
    buffer capture path runs (used to verify partial-capture on mid-stream
    error).
    """
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container-1907"

    mock_sm = MagicMock()
    mock_sm.get_session_by_ip.return_value = mock_session
    with patch.object(gateway, "get_session_manager", return_value=mock_sm):
        yield


def _stream_body() -> bytes:
    """A minimal well-formed SSE body split across chunks for realism."""
    return (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"model":"claude-test","usage":{"input_tokens":1}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )


STREAM_REQUEST_BODY = json.dumps(
    {
        "model": "claude-test",
        "max_tokens": 16,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
).encode()


# --------------------------------------------------------------------------- #
# (a) Pre-stream reset retried successfully
# --------------------------------------------------------------------------- #


class TestPreStreamRetry:
    def test_read_error_before_bytes_is_retried_and_succeeds(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        First attempt: server TCP-resets before any body byte arrives.
        Second attempt: clean stream.

        Expected: downstream sees ONLY the successful retry's bytes. No
        truncation. No error event. Response status 200.
        """
        body = _stream_body()
        # Split into 3 chunks so we exercise the "first_chunk peek + continue
        # iterator" path.
        chunks = [body[:100], body[100:250], body[250:]]

        fake = _FakeClient(
            responses=[
                # First attempt — reset before any chunk.
                _FakeResponse(chunks=[b"ignored"], raise_at=0),
                # Second attempt — full clean stream.
                _FakeResponse(chunks=chunks),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")
        got = resp.get_data()
        # Downstream should have received exactly the retry's body bytes —
        # no mid-stream error envelope, no duplicated bytes from the failed
        # first attempt.
        assert got == body
        assert b"upstream connection reset" not in got
        # Both attempts were consumed.
        assert fake.sent == 2
        assert fake.responses == []


# --------------------------------------------------------------------------- #
# (b) Pre-stream reset persists across retries → 502 JSON error
# --------------------------------------------------------------------------- #


class TestPreStreamRetryExhausted:
    def test_persistent_read_error_returns_502_json(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        Both attempts fail with ReadError before any bytes. Gateway must
        return a 502 JSON error_payload — the agent must never see a
        truncated SSE stream in this case.
        """
        fake = _FakeClient(
            responses=[
                _FakeResponse(chunks=[b""], raise_at=0),
                _FakeResponse(chunks=[b""], raise_at=0),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 502
        payload = json.loads(resp.get_data())
        assert payload["error"]["type"] == "api_error"
        assert "connection reset" in payload["error"]["message"].lower()
        # Exactly MAX_PRE_STREAM_RETRIES+1 = 2 attempts consumed.
        assert fake.sent == 2

    def test_persistent_remote_protocol_error_returns_502_json(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """Same as above but for RemoteProtocolError — both must be handled."""
        fake = _FakeClient(
            responses=[
                _FakeResponse(chunks=[b""], raise_at=0, raise_exc=httpx.RemoteProtocolError),
                _FakeResponse(chunks=[b""], raise_at=0, raise_exc=httpx.RemoteProtocolError),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 502
        payload = json.loads(resp.get_data())
        assert payload["error"]["type"] == "api_error"

    def test_read_error_raised_from_send_itself_is_retried(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        Some TCP resets surface from ``client.send()`` before ``iter_bytes()``
        is ever called (when the response headers themselves fail to read).
        The retry loop must handle that path too.
        """
        body = _stream_body()
        fake = _FakeClient(
            responses=[
                httpx.ReadError("reset during header read"),
                _FakeResponse(chunks=[body]),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.get_data() == body
        assert fake.sent == 2


# --------------------------------------------------------------------------- #
# (c) Mid-stream reset → terminating SSE event: error envelope
# --------------------------------------------------------------------------- #


class TestMidStreamGracefulClose:
    def test_mid_stream_read_error_emits_sse_error_event(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        The stream yields a valid prefix, then the upstream resets. The
        gateway must not re-issue the request (would duplicate output). It
        must emit a well-formed terminating ``event: error`` envelope and
        close cleanly.
        """
        body = _stream_body()
        # Split body into 3 chunks, raise after yielding 2.
        chunks = [body[:100], body[100:260], body[260:]]

        fake = _FakeClient(
            responses=[
                _FakeResponse(chunks=chunks, raise_at=2),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        # Status is 200 (the original response headers already went out by
        # the time we detected the reset — the error is emitted in-band).
        assert resp.status_code == 200
        got = resp.get_data()

        # Downstream saw the good prefix...
        assert got.startswith(chunks[0])
        assert chunks[1] in got
        # ...followed by a well-formed SSE error envelope.
        assert b"event: error\n" in got
        error_line_start = got.index(b'data: {"type":"error"')
        # Parse the JSON payload immediately after 'data: '.
        tail = got[error_line_start + len(b"data: ") :]
        # SSE data lines end at the next newline.
        json_bytes = tail.split(b"\n", 1)[0]
        error_envelope = json.loads(json_bytes)
        assert error_envelope["type"] == "error"
        assert error_envelope["error"]["type"] == "api_error"
        assert "reset" in error_envelope["error"]["message"].lower()

        # The request was made exactly once — no retry was attempted post-stream.
        assert fake.sent == 1

    def test_mid_stream_remote_protocol_error_emits_sse_error_event(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        body = _stream_body()
        chunks = [body[:80], body[80:], b"never-yielded"]

        fake = _FakeClient(
            responses=[
                _FakeResponse(chunks=chunks, raise_at=2, raise_exc=httpx.RemoteProtocolError),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        got = resp.get_data()
        assert b"event: error" in got
        assert b'"type":"api_error"' in got


# --------------------------------------------------------------------------- #
# (d) Transcript capture still runs on mid-stream error with partial data
# --------------------------------------------------------------------------- #


class TestTranscriptCaptureOnMidStreamError:
    def test_capture_invoked_with_partial_accumulator(
        self, flask_client, stub_credentials, with_session
    ) -> None:
        """
        When the stream resets after some bytes (including at least a
        ``message_start`` + ``content_block_delta``), the ``finally`` block
        must still invoke ``_capture_streaming_response`` with whatever the
        accumulator managed to parse so far.
        """
        body = _stream_body()
        # Split so the first two chunks carry message_start + a partial
        # content_block_delta line, then reset.
        chunks = [body[:140], body[140:300], body[300:]]

        fake = _FakeClient(
            responses=[
                _FakeResponse(chunks=chunks, raise_at=2),
            ]
        )

        captured_calls: list[dict[str, Any]] = []

        def _capture_spy(**kwargs: Any) -> None:
            captured_calls.append(kwargs)

        with (
            patch.object(gateway, "get_anthropic_client", return_value=fake),
            patch.object(gateway, "_capture_streaming_response", side_effect=_capture_spy),
        ):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )
            # Drain the response so the generator runs to completion and
            # fires its finally block.
            _ = resp.get_data()

        assert resp.status_code == 200
        assert len(captured_calls) == 1, (
            f"expected transcript capture to run exactly once, got {len(captured_calls)}"
        )
        call = captured_calls[0]
        assert call["container_id"] == "test-container-1907"
        # The accumulator parsed at least the model from message_start.
        result = call["result"]
        assert isinstance(result, tuple)
        content, usage, model, stop_reason = result
        assert model == "claude-test"
        # message_start also recorded input_tokens.
        assert usage is not None
        assert usage.get("input_tokens") == 1


# --------------------------------------------------------------------------- #
# Gap-targeted tests added by tester role (issue #1907)
#
# The coder's test file above covers the four scenarios called out in
# task-1-3 (a/b/c/d). These additional tests target adjacent gaps:
#
#   - baseline happy-path (no reset) still behaves identically to pre-fix.
#   - non-retryable transport errors (ConnectError / TimeoutException) are
#     NOT swept into the pre-stream retry loop and keep their existing
#     502/504 handlers.
#   - the retry uses the same headers + body as the original attempt.
#   - the first response status is preserved when there is no reset.
#   - structured log events `upstream_reset_retry` and
#     `upstream_reset_midstream` are emitted with the documented shape.
#   - upstream.close() is invoked in generate()'s finally block on both the
#     happy path and after a mid-stream reset.
#   - mid-stream reset after the very last chunk (iter_bytes raises AFTER
#     all chunks) still emits the error envelope cleanly.
#   - empty first chunk (upstream yields nothing yet opens the response)
#     does NOT trigger a retry.
# --------------------------------------------------------------------------- #


class TestBaselineHappyPath:
    def test_clean_stream_no_retry_no_error_envelope(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        Sanity check: a clean upstream stream is forwarded verbatim, with
        status preserved and no retry attempted.
        """
        body = _stream_body()
        chunks = [body[:120], body[120:240], body[240:]]
        fake = _FakeClient(responses=[_FakeResponse(chunks=chunks)])

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.get_data() == body
        # No retry should have been consumed.
        assert fake.sent == 1
        # No synthetic error envelope should be emitted on the happy path.
        assert b"event: error" not in resp.get_data()

    def test_upstream_non_200_status_is_forwarded_without_retry(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        A clean 500 response from upstream (with body) is forwarded as-is.
        Retry only applies to transport resets, not upstream application
        errors.
        """
        body = b'event: error\ndata: {"type":"error"}\n\n'
        fake = _FakeClient(
            responses=[_FakeResponse(chunks=[body], status_code=500)],
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 500
        assert resp.get_data() == body
        assert fake.sent == 1


class TestNonRetryableErrors:
    def test_connect_error_is_not_retried_and_returns_502(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        ConnectError (DNS failure, refused socket) must hit the existing
        ConnectError handler and return 502 "Failed to connect". It must
        NOT be swept into the ReadError retry loop.
        """
        fake = _FakeClient(
            responses=[httpx.ConnectError("refused"), httpx.ConnectError("refused")],
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 502
        payload = json.loads(resp.get_data())
        # The ConnectError handler prefixes with "Failed to connect"; the
        # retry-exhausted ReadError handler prefixes with "connection reset".
        # This assertion ensures we took the correct branch.
        assert "Failed to connect" in payload["error"]["message"]
        # Only one attempt: no retry was performed for a ConnectError.
        assert fake.sent == 1

    def test_timeout_exception_is_not_retried_and_returns_504(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """TimeoutException must hit the existing 504 handler, unretried."""
        fake = _FakeClient(
            responses=[httpx.TimeoutException("slow"), httpx.TimeoutException("slow")],
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 504
        payload = json.loads(resp.get_data())
        assert payload["error"]["type"] == "api_error"
        assert "timed out" in payload["error"]["message"].lower()
        assert fake.sent == 1


class TestRetryRequestIdempotency:
    def test_retry_rebuilds_request_with_same_headers_and_body(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        On retry, the coder's implementation calls client.build_request()
        again with the same headers + body. Each send() call must therefore
        carry an identical (method, url, body) tuple. The injected
        x-api-key must be present on both attempts (credentials are looked
        up once before the loop).
        """
        body = _stream_body()

        class _RecordingClient(_FakeClient):
            def __init__(self, responses: list[Any]) -> None:
                super().__init__(responses)
                self.sent_requests: list[httpx.Request] = []

            def send(self, request: httpx.Request, stream: bool = False) -> Any:
                self.sent_requests.append(request)
                return super().send(request, stream=stream)

        fake = _RecordingClient(
            responses=[
                _FakeResponse(chunks=[b""], raise_at=0),
                _FakeResponse(chunks=[body]),
            ]
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.get_data() == body
        assert len(fake.sent_requests) == 2

        r1, r2 = fake.sent_requests
        assert r1.method == r2.method == "POST"
        assert r1.url.path == r2.url.path == "/v1/messages"
        # Body is identical across attempts.
        assert r1.content == r2.content == STREAM_REQUEST_BODY
        # Injected auth header is present on both attempts.
        assert r1.headers.get("x-api-key") == "test-key"
        assert r2.headers.get("x-api-key") == "test-key"


class TestStructuredLogging:
    def test_upstream_reset_retry_logged_at_info(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        On pre-stream retry, logger.info should be called with the
        structured event name ``upstream_reset_retry`` (contract task-1-1).
        """
        body = _stream_body()
        fake = _FakeClient(
            responses=[
                _FakeResponse(chunks=[b""], raise_at=0),
                _FakeResponse(chunks=[body]),
            ]
        )

        with (
            patch.object(gateway, "get_anthropic_client", return_value=fake),
            patch.object(gateway, "logger") as mock_logger,
        ):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        # Find the logger.info call for upstream_reset_retry.
        info_events = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert "upstream_reset_retry" in info_events, (
            f"expected 'upstream_reset_retry' INFO log event, got {info_events}"
        )

    def test_upstream_reset_midstream_logged_at_warning_with_bytes(
        self, flask_client, stub_credentials, with_session
    ) -> None:
        """
        On mid-stream reset, logger.warning should be called with
        ``upstream_reset_midstream`` and include ``bytes_transferred``
        context (contract task-1-2 acceptance).

        Uses ``with_session`` so the accumulator path runs and the
        ``bytes_seen`` counter is actually incremented per chunk —
        ``bytes_transferred`` should then reflect the chunk bytes the
        accumulator saw prior to the reset.
        """
        body = _stream_body()
        chunks = [body[:100], body[100:260], body[260:]]
        fake = _FakeClient(
            responses=[_FakeResponse(chunks=chunks, raise_at=2)],
        )

        with (
            patch.object(gateway, "get_anthropic_client", return_value=fake),
            patch.object(gateway, "logger") as mock_logger,
        ):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )
            # Drain so the generator runs to completion and fires its
            # except/finally blocks.
            _ = resp.get_data()

        warning_calls = [c for c in mock_logger.warning.call_args_list if c.args]
        matching = [c for c in warning_calls if c.args and c.args[0] == "upstream_reset_midstream"]
        assert matching, (
            f"expected 'upstream_reset_midstream' WARNING log, got "
            f"{[c.args for c in warning_calls]}"
        )
        kwargs = matching[0].kwargs
        # Bytes transferred context is present and > 0 (we yielded the
        # first two chunks before the reset fired on the third).
        assert "bytes_transferred" in kwargs
        assert isinstance(kwargs["bytes_transferred"], int)
        assert kwargs["bytes_transferred"] == len(chunks[0]) + len(chunks[1])


class TestUpstreamClosed:
    def test_upstream_close_called_on_happy_path(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """``generate()``'s finally block must close the upstream on success."""
        body = _stream_body()
        resp_obj = _FakeResponse(chunks=[body])
        fake = _FakeClient(responses=[resp_obj])

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )
            _ = resp.get_data()

        assert resp_obj.closed is True

    def test_upstream_close_called_after_midstream_reset(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """finally must still run (and close upstream) after mid-stream reset."""
        body = _stream_body()
        chunks = [body[:80], body[80:], b"never-yielded"]
        resp_obj = _FakeResponse(chunks=chunks, raise_at=2)
        fake = _FakeClient(responses=[resp_obj])

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )
            _ = resp.get_data()

        assert resp_obj.closed is True

    def test_failed_upstream_closed_before_retry(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        The pre-stream retry loop must close the failed upstream before
        opening a new one. Otherwise we'd leak connections.
        """
        body = _stream_body()
        failed = _FakeResponse(chunks=[b""], raise_at=0)
        good = _FakeResponse(chunks=[body])
        fake = _FakeClient(responses=[failed, good])

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        # The failed upstream was closed during retry. The good upstream
        # was closed by generate()'s finally block after the response.
        assert failed.closed is True
        # good.closed only becomes True after the response is fully drained
        # (the Flask test client drains streams automatically, but we check
        # after explicitly reading the data).
        _ = resp.get_data()
        assert good.closed is True


class TestEdgeCases:
    def test_empty_first_chunk_does_not_trigger_retry(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        If upstream's iter_bytes() yields an empty b"" first (legal per
        httpx docs — e.g. chunked transfer with empty padding), that is
        NOT a reset and must not trigger a retry.
        """
        body = _stream_body()
        fake = _FakeClient(
            responses=[_FakeResponse(chunks=[b"", body])],
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        # Only one send() consumed.
        assert fake.sent == 1
        # Downstream received the real body (empty chunk is harmless; it's
        # OK whether it is or isn't passed through as long as the real
        # body is intact).
        assert body in resp.get_data()

    def test_reset_after_final_chunk_emits_error_envelope(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        Some httpx transports raise ReadError AFTER the final body chunk
        (during the trailing read for Content-Length verification). The
        gateway should still emit the terminating SSE error envelope
        rather than letting the exception escape.
        """
        body = _stream_body()
        chunks = [body[:120], body[120:]]
        # raise_at = len(chunks) means "raise after yielding all chunks".
        fake = _FakeClient(
            responses=[_FakeResponse(chunks=chunks, raise_at=len(chunks))],
        )

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=STREAM_REQUEST_BODY,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        got = resp.get_data()
        # Valid body chunks were delivered before the terminator.
        assert chunks[0] in got
        assert chunks[1] in got
        # And the terminator error envelope was appended.
        assert b"event: error" in got
        assert b'"type":"api_error"' in got

    def test_non_streaming_path_is_unchanged_by_fix(
        self, flask_client, stub_credentials, no_session
    ) -> None:
        """
        Non-streaming /v1/messages must be unaffected by the streaming
        retry logic. It uses client.post() which this test doesn't stub
        — so we build a fake whose post() returns a canned response.
        """
        body = b'{"id":"msg_test","content":[{"type":"text","text":"hi"}]}'

        class _NonStreamingFake(_FakeClient):
            def post(self, *_args: Any, **_kwargs: Any) -> Any:
                r = MagicMock()
                r.status_code = 200
                r.content = body
                r.headers = httpx.Headers({"content-type": "application/json"})
                return r

            def send(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
                raise AssertionError("send() must not be called on a non-streaming request")

        fake = _NonStreamingFake(responses=[])
        non_streaming_body = json.dumps(
            {
                "model": "claude-test",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()

        with patch.object(gateway, "get_anthropic_client", return_value=fake):
            resp = flask_client.post(
                "/v1/messages",
                data=non_streaming_body,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.get_data() == body
