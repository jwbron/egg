"""Retry wrapper provider for the egg harness.

Adds automatic retry with exponential backoff and a circuit breaker around
any :class:`Provider` implementation.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from egg_harness.providers.base import Provider, StreamEvent

logger = logging.getLogger(__name__)

# HTTP status codes that should trigger a retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Number of consecutive non-retryable failures before the circuit opens.
_CIRCUIT_BREAKER_THRESHOLD: int = 3


class RetryProvider(Provider):
    """Retry wrapper that adds resilience around another :class:`Provider`.

    Retries on rate-limit (429), server errors (5xx), and connection errors.
    Does **not** retry on client errors (4xx except 429).

    Includes a simple circuit breaker: after ``_CIRCUIT_BREAKER_THRESHOLD``
    consecutive non-retryable failures, subsequent calls raise immediately
    until a successful call resets the counter.

    Args:
        inner: The underlying provider to wrap.
        max_retries: Maximum number of retry attempts (default 3).
        base_delay: Base delay in seconds for exponential backoff (default 1.0).
    """

    def __init__(
        self,
        inner: Provider,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._consecutive_non_retryable_failures: int = 0

    @property
    def name(self) -> str:
        """Delegate to the wrapped provider's name."""
        return self._inner.name

    async def send_message(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16384,
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response with automatic retry on transient failures.

        Yields:
            :data:`StreamEvent` instances from the underlying provider.

        Raises:
            The original exception after all retries are exhausted or when
            the circuit breaker is open.
        """
        # Circuit breaker: if too many consecutive non-retryable failures,
        # fail fast.
        if self._consecutive_non_retryable_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            raise CircuitOpenError(
                f"Circuit breaker open after {self._consecutive_non_retryable_failures} "
                f"consecutive non-retryable failures on provider {self.name!r}."
            )

        last_exc: BaseException | None = None

        for attempt in range(self._max_retries + 1):
            try:
                async for event in self._inner.send_message(
                    messages=messages,
                    tools=tools,
                    system=system,
                    model=model,
                    max_tokens=max_tokens,
                    extra_headers=extra_headers,
                ):
                    yield event

                # Success -- reset the circuit breaker and return.
                self._consecutive_non_retryable_failures = 0
                return

            except Exception as exc:
                last_exc = exc

                if not _is_retryable(exc):
                    self._consecutive_non_retryable_failures += 1
                    logger.warning(
                        "Non-retryable error on provider %r (attempt %d): %s",
                        self.name,
                        attempt + 1,
                        exc,
                    )
                    raise

                # Retryable error -- back off and try again (unless exhausted).
                if attempt < self._max_retries:
                    delay = self._base_delay * (2**attempt) + random.uniform(0, 1)
                    logger.info(
                        "Retryable error on provider %r (attempt %d/%d), retrying in %.2fs: %s",
                        self.name,
                        attempt + 1,
                        self._max_retries + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "All %d retries exhausted on provider %r: %s",
                        self._max_retries + 1,
                        self.name,
                        exc,
                    )

        # All retries exhausted -- propagate the last exception.
        if last_exc is not None:
            raise last_exc  # noqa: TRY201


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open and calls are rejected."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception represents a transient/retryable failure."""
    # httpx HTTP status errors.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES

    # Connection-level errors from httpx.
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True

    # Broad network / transport errors.
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return True

    # Anthropic SDK wraps HTTP errors; check for retryable status codes.
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in _RETRYABLE_STATUS_CODES

    # Connection-related exceptions from other libraries (e.g. aiohttp).
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    return False
