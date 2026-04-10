"""Tests for egg_harness.providers.anthropic — AnthropicProvider."""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.providers.anthropic")

import os
from unittest.mock import AsyncMock, MagicMock, patch

from egg_harness.providers.anthropic import AnthropicProvider
from egg_harness.providers.base import (
    MessageStart,
    Provider,
    TextDelta,
)


class TestAnthropicProviderInit:
    """Verify AnthropicProvider construction and gateway URL validation."""

    def test_inherits_from_provider(self):
        """AnthropicProvider must be a subclass of Provider."""
        assert issubclass(AnthropicProvider, Provider)

    def test_valid_gateway_url(self):
        """Provider should accept a well-formed gateway URL."""
        provider = AnthropicProvider(gateway_url="http://egg-gateway:9848")
        assert provider is not None

    def test_valid_gateway_url_with_path(self):
        """Provider should accept a gateway URL that includes a path."""
        provider = AnthropicProvider(gateway_url="http://egg-gateway:9848/v1")
        assert provider is not None

    def test_valid_https_gateway_url(self):
        """Provider should accept HTTPS gateway URLs."""
        provider = AnthropicProvider(gateway_url="https://gateway.example.com")
        assert provider is not None

    def test_invalid_gateway_url_no_scheme(self):
        """Gateway URL without a scheme should be rejected."""
        with pytest.raises((ValueError, TypeError)):
            AnthropicProvider(gateway_url="egg-gateway:9848")

    def test_invalid_gateway_url_empty(self):
        """Empty gateway URL should be rejected."""
        with pytest.raises((ValueError, TypeError)):
            AnthropicProvider(gateway_url="")

    def test_anthropic_api_key_not_in_env(self):
        """ANTHROPIC_API_KEY must NOT be present in the environment at init.

        The provider routes through the gateway sidecar which injects
        credentials. Having the key in the env risks leaking it or
        bypassing the gateway.
        """
        env_with_key = {**os.environ, "ANTHROPIC_API_KEY": "sk-ant-test-key"}
        with patch.dict(os.environ, env_with_key, clear=True):
            with pytest.raises((ValueError, AssertionError, RuntimeError)):
                AnthropicProvider(gateway_url="http://egg-gateway:9848")


class TestAnthropicProviderStreaming:
    """Verify streaming behaviour with a mocked Anthropic SDK client."""

    @pytest.fixture
    def provider(self):
        return AnthropicProvider(gateway_url="http://egg-gateway:9848")

    def _make_sse_event(self, event_type: str, **kwargs):
        """Helper to build a mock SSE event object."""
        event = MagicMock()
        event.type = event_type
        for k, v in kwargs.items():
            setattr(event, k, v)
        return event

    @pytest.mark.asyncio
    async def test_stream_yields_text_delta(self, provider):
        """A content_block_delta with text type should yield TextDelta."""
        delta = MagicMock()
        delta.type = "text_delta"
        delta.text = "Hello"
        event = self._make_sse_event("content_block_delta", index=0, delta=delta)

        mock_stream = AsyncMock()
        mock_stream.__aiter__ = lambda self: self
        mock_stream.__anext__ = AsyncMock(side_effect=[event, StopAsyncIteration])

        with patch.object(provider, "_create_stream", return_value=mock_stream):
            events = []
            async for e in provider.send_message(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
                system="You are helpful.",
                model="claude-sonnet-4-20250514",
            ):
                events.append(e)

            text_events = [e for e in events if isinstance(e, TextDelta)]
            assert len(text_events) >= 1
            assert text_events[0].text == "Hello"

    @pytest.mark.asyncio
    async def test_stream_yields_message_start(self, provider):
        """message_start SSE should yield a MessageStart event."""
        message = MagicMock()
        message.id = "msg_123"
        message.model = "claude-sonnet-4-20250514"
        message.role = "assistant"
        event = self._make_sse_event("message_start", message=message)

        mock_stream = AsyncMock()
        mock_stream.__aiter__ = lambda self: self
        mock_stream.__anext__ = AsyncMock(side_effect=[event, StopAsyncIteration])

        with patch.object(provider, "_create_stream", return_value=mock_stream):
            events = []
            async for e in provider.send_message(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
                system="sys",
                model="claude-sonnet-4-20250514",
            ):
                events.append(e)

            msg_starts = [e for e in events if isinstance(e, MessageStart)]
            assert len(msg_starts) >= 1
            assert msg_starts[0].message_id == "msg_123"
            assert msg_starts[0].model == "claude-sonnet-4-20250514"
            assert msg_starts[0].role == "assistant"


class TestAnthropicProviderRetry:
    """Verify retry and circuit-breaker behaviour."""

    @pytest.fixture
    def provider(self):
        return AnthropicProvider(gateway_url="http://egg-gateway:9848")

    @pytest.mark.asyncio
    async def test_retries_on_429(self, provider):
        """HTTP 429 (rate limit) should trigger a retry, not raise immediately."""
        rate_limit_exc = _make_api_status_error(429)

        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise rate_limit_exc
            return _empty_async_iter()

        with patch.object(provider, "_create_stream", side_effect=fake_stream):
            events = []
            try:
                async for e in provider.send_message(
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    system="sys",
                    model="claude-sonnet-4-20250514",
                ):
                    events.append(e)
            except Exception:
                pass  # May still raise after exhausting retries

            # Should have attempted more than once
            assert call_count >= 2, "Provider should retry on 429 rate-limit errors"

    @pytest.mark.asyncio
    async def test_retries_on_5xx(self, provider):
        """HTTP 5xx (server error) should trigger a retry."""
        server_exc = _make_api_status_error(500)

        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise server_exc
            return _empty_async_iter()

        with patch.object(provider, "_create_stream", side_effect=fake_stream):
            try:
                async for _ in provider.send_message(
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    system="sys",
                    model="claude-sonnet-4-20250514",
                ):
                    pass
            except Exception:
                pass

            assert call_count >= 2, "Provider should retry on 5xx server errors"

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx_except_429(self, provider):
        """HTTP 4xx (except 429) should NOT be retried."""
        bad_request_exc = _make_api_status_error(400)

        call_count = 0

        async def fake_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise bad_request_exc

        with patch.object(provider, "_create_stream", side_effect=fake_stream):
            with pytest.raises(RuntimeError):
                async for _ in provider.send_message(
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    system="sys",
                    model="claude-sonnet-4-20250514",
                ):
                    pass

            assert call_count == 1, "Provider must not retry 4xx errors (except 429)"

    @pytest.mark.asyncio
    async def test_circuit_breaker_after_consecutive_failures(self, provider):
        """After 3 consecutive failures the circuit breaker should open.

        Subsequent calls should fail fast without hitting the backend.
        """
        server_exc = _make_api_status_error(500)

        call_count = 0

        async def failing_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise server_exc

        with patch.object(provider, "_create_stream", side_effect=failing_stream):
            # Exhaust retries / trip the breaker with 3+ attempts
            for _ in range(3):
                try:
                    async for _ in provider.send_message(
                        messages=[{"role": "user", "content": "Hi"}],
                        tools=[],
                        system="sys",
                        model="claude-sonnet-4-20250514",
                    ):
                        pass
                except Exception:
                    pass

            before_count = call_count

            # The next call should fail fast (circuit open)
            with pytest.raises(RuntimeError):
                async for _ in provider.send_message(
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    system="sys",
                    model="claude-sonnet-4-20250514",
                ):
                    pass

            # Circuit breaker should prevent additional backend calls
            # (or at most allow 1 probe). The total new calls should be
            # significantly fewer than a full retry cycle.
            new_calls = call_count - before_count
            assert new_calls <= 1, (
                f"Circuit breaker should limit calls after 3 consecutive "
                f"failures, but {new_calls} new backend calls were made"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_api_status_error(status_code: int) -> Exception:
    """Create a mock exception that mimics anthropic.APIStatusError."""
    exc = Exception(f"HTTP {status_code}")
    exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


async def _empty_async_iter():
    """Return an empty async iterator."""
    return
    yield  # Make it an async generator  # noqa: RET504
