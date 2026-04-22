"""Tests for egg_harness.providers.retry — retry wrapper with circuit breaker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from egg_harness.providers.base import (
    MessageEnd,
    MessageStart,
    StreamEvent,
    TextDelta,
)
from egg_harness.providers.retry import (
    CircuitOpenError,
    RetryProvider,
    _is_retryable,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events() -> list[StreamEvent]:
    return [
        MessageStart(message_id="msg_001", model="test", role="assistant"),
        TextDelta(text="Hello"),
        MessageEnd(),
    ]


async def _stream_events(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for event in events:
        yield event


def _make_inner_provider(
    responses: list[list[StreamEvent] | Exception],
) -> MagicMock:
    """Create a mock Provider that yields events or raises exceptions.

    Each entry in responses is consumed in order. If it's a list,
    the events are yielded. If it's an Exception, it's raised.
    """
    provider = MagicMock()
    provider.name = "mock-inner"
    call_count = 0

    async def _send_message(**kwargs: Any) -> AsyncIterator[StreamEvent]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        resp = responses[idx]
        if isinstance(resp, Exception):
            raise resp
        async for event in _stream_events(resp):
            yield event

    provider.send_message = _send_message
    return provider


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Create an httpx.HTTPStatusError with the given status code."""
    response = httpx.Response(status_code=status_code, request=httpx.Request("POST", "http://test"))
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=response.request,
        response=response,
    )


async def _collect_events(retry: RetryProvider, **kwargs: Any) -> list[StreamEvent]:
    """Collect all events from a RetryProvider.send_message call."""
    events = []
    default_kwargs = {"messages": [{"role": "user", "content": "hi"}]}
    default_kwargs.update(kwargs)
    async for event in retry.send_message(**default_kwargs):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# TestRetryProviderSuccess
# ---------------------------------------------------------------------------


class TestRetryProviderSuccess:
    @pytest.mark.anyio
    async def test_success_on_first_attempt(self):
        events = _make_events()
        inner = _make_inner_provider([events])
        retry = RetryProvider(inner)
        collected = await _collect_events(retry)
        assert len(collected) == 3

    @pytest.mark.anyio
    async def test_name_delegates_to_inner(self):
        inner = _make_inner_provider([_make_events()])
        retry = RetryProvider(inner)
        assert retry.name == "mock-inner"

    @pytest.mark.anyio
    async def test_success_resets_circuit_breaker(self):
        inner = _make_inner_provider(
            [
                _make_http_status_error(400),
                _make_http_status_error(400),
                _make_events(),  # success
            ]
        )
        retry = RetryProvider(inner)
        # Two non-retryable failures
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 2
        # Success resets the counter
        await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 0


# ---------------------------------------------------------------------------
# TestRetryProviderRetryableErrors
# ---------------------------------------------------------------------------


class TestRetryProviderRetryableErrors:
    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retries_on_429(self, mock_sleep):
        inner = _make_inner_provider(
            [
                _make_http_status_error(429),
                _make_events(),
            ]
        )
        retry = RetryProvider(inner, base_delay=0.01)
        events = await _collect_events(retry)
        assert len(events) == 3
        mock_sleep.assert_called_once()

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retries_on_500(self, mock_sleep):
        inner = _make_inner_provider(
            [
                _make_http_status_error(500),
                _make_events(),
            ]
        )
        retry = RetryProvider(inner, base_delay=0.01)
        events = await _collect_events(retry)
        assert len(events) == 3

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retries_on_502_503_504(self, mock_sleep):
        for code in (502, 503, 504):
            inner = _make_inner_provider(
                [
                    _make_http_status_error(code),
                    _make_events(),
                ]
            )
            retry = RetryProvider(inner, base_delay=0.01)
            events = await _collect_events(retry)
            assert len(events) == 3

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retries_on_connection_error(self, mock_sleep):
        inner = _make_inner_provider(
            [
                httpx.ConnectError("connection refused"),
                _make_events(),
            ]
        )
        retry = RetryProvider(inner, base_delay=0.01)
        events = await _collect_events(retry)
        assert len(events) == 3

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retries_on_read_timeout(self, mock_sleep):
        inner = _make_inner_provider(
            [
                httpx.ReadTimeout("read timeout"),
                _make_events(),
            ]
        )
        retry = RetryProvider(inner, base_delay=0.01)
        events = await _collect_events(retry)
        assert len(events) == 3

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retry_succeeds_after_transient_failure(self, mock_sleep):
        inner = _make_inner_provider(
            [
                _make_http_status_error(429),
                _make_http_status_error(500),
                _make_events(),
            ]
        )
        retry = RetryProvider(inner, max_retries=3, base_delay=0.01)
        events = await _collect_events(retry)
        assert len(events) == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_max_retries_exhausted_raises(self, mock_sleep):
        err = _make_http_status_error(429)
        inner = _make_inner_provider([err, err, err, err])
        retry = RetryProvider(inner, max_retries=3, base_delay=0.01)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        assert mock_sleep.call_count == 3


# ---------------------------------------------------------------------------
# TestRetryProviderNonRetryableErrors
# ---------------------------------------------------------------------------


class TestRetryProviderNonRetryableErrors:
    @pytest.mark.anyio
    async def test_no_retry_on_400(self):
        inner = _make_inner_provider([_make_http_status_error(400)])
        retry = RetryProvider(inner)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)

    @pytest.mark.anyio
    async def test_no_retry_on_401(self):
        inner = _make_inner_provider([_make_http_status_error(401)])
        retry = RetryProvider(inner)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)

    @pytest.mark.anyio
    async def test_no_retry_on_403(self):
        inner = _make_inner_provider([_make_http_status_error(403)])
        retry = RetryProvider(inner)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)

    @pytest.mark.anyio
    async def test_no_retry_on_404(self):
        inner = _make_inner_provider([_make_http_status_error(404)])
        retry = RetryProvider(inner)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)

    @pytest.mark.anyio
    async def test_non_retryable_increments_circuit_counter(self):
        inner = _make_inner_provider([_make_http_status_error(400)])
        retry = RetryProvider(inner)
        assert retry._consecutive_non_retryable_failures == 0
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 1


# ---------------------------------------------------------------------------
# TestRetryProviderCircuitBreaker
# ---------------------------------------------------------------------------


class TestRetryProviderCircuitBreaker:
    @pytest.mark.anyio
    async def test_circuit_opens_after_3_non_retryable(self):
        err = _make_http_status_error(400)
        inner = _make_inner_provider([err, err, err, _make_events()])
        retry = RetryProvider(inner)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 3
        with pytest.raises(CircuitOpenError):
            await _collect_events(retry)

    @pytest.mark.anyio
    async def test_circuit_reset_on_success(self):
        err = _make_http_status_error(400)
        inner = _make_inner_provider([err, err, _make_events(), err, err])
        retry = RetryProvider(inner)
        # Two failures
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 2
        # Success resets
        await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 0
        # Two more failures — circuit should not open (only 2, not 3)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        with pytest.raises(httpx.HTTPStatusError):
            await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 2

    @pytest.mark.anyio
    async def test_circuit_open_error_message(self):
        err = _make_http_status_error(400)
        inner = _make_inner_provider([err, err, err])
        retry = RetryProvider(inner)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                await _collect_events(retry)
        with pytest.raises(CircuitOpenError, match="mock-inner"):
            await _collect_events(retry)

    @pytest.mark.anyio
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_retryable_errors_do_not_increment_circuit(self, mock_sleep):
        inner = _make_inner_provider(
            [
                _make_http_status_error(429),
                _make_http_status_error(429),
                _make_events(),
            ]
        )
        retry = RetryProvider(inner, max_retries=3, base_delay=0.01)
        await _collect_events(retry)
        assert retry._consecutive_non_retryable_failures == 0


# ---------------------------------------------------------------------------
# TestExponentialBackoff
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    @pytest.mark.anyio
    @patch("egg_harness.providers.retry.random.uniform", return_value=0.5)
    @patch("egg_harness.providers.retry._sleep", new_callable=AsyncMock)
    async def test_backoff_increases_with_attempts(self, mock_sleep, mock_random):
        err = _make_http_status_error(429)
        inner = _make_inner_provider([err, err, err, _make_events()])
        retry = RetryProvider(inner, max_retries=3, base_delay=1.0)
        await _collect_events(retry)
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # base_delay * 2^attempt + jitter(0.5)
        # attempt 0: 1.0 * 1 + 0.5 = 1.5
        # attempt 1: 1.0 * 2 + 0.5 = 2.5
        # attempt 2: 1.0 * 4 + 0.5 = 4.5
        assert delays == pytest.approx([1.5, 2.5, 4.5])


# ---------------------------------------------------------------------------
# TestIsRetryable
# ---------------------------------------------------------------------------


class TestIsRetryable:
    def test_httpx_status_error_retryable_codes(self):
        for code in (429, 500, 502, 503, 504):
            assert _is_retryable(_make_http_status_error(code)) is True

    def test_httpx_status_error_non_retryable_codes(self):
        for code in (400, 401, 403, 404, 409, 422):
            assert _is_retryable(_make_http_status_error(code)) is False

    def test_httpx_connect_errors_are_retryable(self):
        assert _is_retryable(httpx.ConnectError("refused")) is True
        assert _is_retryable(httpx.ConnectTimeout("timeout")) is True

    def test_httpx_timeout_errors_are_retryable(self):
        assert _is_retryable(httpx.ReadTimeout("timeout")) is True
        assert _is_retryable(httpx.WriteTimeout("timeout")) is True
        assert _is_retryable(httpx.PoolTimeout("timeout")) is True

    def test_anthropic_sdk_status_code_attribute(self):
        exc = Exception("API error")
        exc.status_code = 429
        assert _is_retryable(exc) is True

        exc2 = Exception("API error")
        exc2.status_code = 400
        assert _is_retryable(exc2) is False

    def test_stdlib_connection_error_is_retryable(self):
        assert _is_retryable(ConnectionError("reset")) is True

    def test_stdlib_timeout_error_is_retryable(self):
        assert _is_retryable(TimeoutError("timed out")) is True

    def test_stdlib_os_error_is_retryable(self):
        assert _is_retryable(OSError("network error")) is True

    def test_generic_exception_is_not_retryable(self):
        assert _is_retryable(ValueError("bad value")) is False
        assert _is_retryable(RuntimeError("fail")) is False
