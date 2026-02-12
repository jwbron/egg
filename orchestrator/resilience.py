"""
Retry logic and circuit breaker for resilient operations.

Provides retry with exponential backoff and circuit breaker pattern
to prevent cascade failures.
"""

import sys
import threading
import time
from datetime import datetime, timedelta
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.resilience")

T = TypeVar("T")


class CircuitState(StrEnum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open."""

    pass


class RetryExhaustedError(Exception):
    """Raised when all retries are exhausted."""

    def __init__(self, message: str, last_error: Exception | None = None):
        super().__init__(message)
        self.last_error = last_error


class CircuitBreaker:
    """Circuit breaker for preventing cascade failures.

    Opens after threshold failures, then allows test requests
    after recovery timeout.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2,
    ):
        """Initialize circuit breaker.

        Args:
            name: Circuit breaker name (for logging)
            failure_threshold: Failures before opening
            recovery_timeout: Seconds before trying again
            success_threshold: Successes in half-open to close
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: datetime | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Get current state, transitioning if needed."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time:
                    elapsed = (datetime.utcnow() - self._last_failure_time).total_seconds()
                    if elapsed >= self.recovery_timeout:
                        self._state = CircuitState.HALF_OPEN
                        self._success_count = 0
                        logger.info(
                            "Circuit half-open",
                            circuit=self.name,
                        )
            return self._state

    def record_success(self) -> None:
        """Record a successful operation."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info("Circuit closed", circuit=self.name)
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = datetime.utcnow()

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open goes back to open
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit reopened",
                    circuit=self.name,
                )
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "Circuit opened",
                        circuit=self.name,
                        failures=self._failure_count,
                    )

    def is_open(self) -> bool:
        """Check if circuit is open."""
        return self.state == CircuitState.OPEN

    def allow_request(self) -> bool:
        """Check if a request should be allowed.

        Returns:
            True if request should proceed
        """
        state = self.state
        if state == CircuitState.OPEN:
            return False
        return True

    def reset(self) -> None:
        """Reset circuit to closed state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            logger.info("Circuit reset", circuit=self.name)


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: float = 0.1,
        retry_exceptions: tuple[type[Exception], ...] | None = None,
    ):
        """Initialize retry config.

        Args:
            max_retries: Maximum retry attempts
            base_delay: Initial delay in seconds
            max_delay: Maximum delay in seconds
            exponential_base: Base for exponential backoff
            jitter: Random jitter factor (0-1)
            retry_exceptions: Exceptions to retry on (default: all)
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self.retry_exceptions = retry_exceptions or (Exception,)

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for attempt number.

        Args:
            attempt: Attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        delay = self.base_delay * (self.exponential_base ** attempt)
        delay = min(delay, self.max_delay)

        # Add jitter
        if self.jitter:
            import random

            jitter_amount = delay * self.jitter * random.uniform(-1, 1)
            delay += jitter_amount

        return max(0, delay)


def with_retry(
    config: RetryConfig | None = None,
    circuit_breaker: CircuitBreaker | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for retry with optional circuit breaker.

    Args:
        config: Retry configuration
        circuit_breaker: Optional circuit breaker

    Returns:
        Decorator function
    """
    config = config or RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            # Check circuit breaker
            if circuit_breaker and not circuit_breaker.allow_request():
                raise CircuitBreakerError(
                    f"Circuit breaker {circuit_breaker.name} is open"
                )

            last_error: Exception | None = None

            for attempt in range(config.max_retries + 1):
                try:
                    result = func(*args, **kwargs)

                    # Record success
                    if circuit_breaker:
                        circuit_breaker.record_success()

                    return result

                except config.retry_exceptions as e:
                    last_error = e

                    # Record failure
                    if circuit_breaker:
                        circuit_breaker.record_failure()

                    if attempt < config.max_retries:
                        delay = config.get_delay(attempt)
                        logger.warning(
                            "Retry scheduled",
                            function=func.__name__,
                            attempt=attempt + 1,
                            max_retries=config.max_retries,
                            delay=delay,
                            error=str(e),
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "Retries exhausted",
                            function=func.__name__,
                            attempts=attempt + 1,
                            error=str(e),
                        )

            raise RetryExhaustedError(
                f"All {config.max_retries + 1} attempts failed for {func.__name__}",
                last_error=last_error,
            )

        return wrapper

    return decorator


def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    circuit_breaker: CircuitBreaker | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Simplified retry decorator.

    Args:
        max_retries: Maximum retries
        base_delay: Base delay in seconds
        circuit_breaker: Optional circuit breaker

    Returns:
        Decorator function
    """
    config = RetryConfig(max_retries=max_retries, base_delay=base_delay)
    return with_retry(config, circuit_breaker)


# Global circuit breakers for common operations
_container_circuit = CircuitBreaker(
    name="container_spawn",
    failure_threshold=3,
    recovery_timeout=30,
)

_gateway_circuit = CircuitBreaker(
    name="gateway_api",
    failure_threshold=5,
    recovery_timeout=60,
)


def get_container_circuit() -> CircuitBreaker:
    """Get circuit breaker for container operations."""
    return _container_circuit


def get_gateway_circuit() -> CircuitBreaker:
    """Get circuit breaker for gateway API calls."""
    return _gateway_circuit


async def retry_async(
    func: Callable[..., Any],
    *args: Any,
    config: RetryConfig | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    **kwargs: Any,
) -> Any:
    """Async retry helper.

    Args:
        func: Async function to retry
        *args: Function arguments
        config: Retry configuration
        circuit_breaker: Optional circuit breaker
        **kwargs: Function keyword arguments

    Returns:
        Function result
    """
    import asyncio

    config = config or RetryConfig()

    # Check circuit breaker
    if circuit_breaker and not circuit_breaker.allow_request():
        raise CircuitBreakerError(
            f"Circuit breaker {circuit_breaker.name} is open"
        )

    last_error: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            result = await func(*args, **kwargs)

            if circuit_breaker:
                circuit_breaker.record_success()

            return result

        except config.retry_exceptions as e:
            last_error = e

            if circuit_breaker:
                circuit_breaker.record_failure()

            if attempt < config.max_retries:
                delay = config.get_delay(attempt)
                await asyncio.sleep(delay)
            else:
                break

    raise RetryExhaustedError(
        f"All {config.max_retries + 1} attempts failed",
        last_error=last_error,
    )
