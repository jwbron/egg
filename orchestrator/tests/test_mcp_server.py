"""Unit tests for ``orchestrator/mcp_server.py``.

Covers the FastMCP wire-up pieces that the integration tests
(``integration_tests/test_orchestrator_mcp_contract.py``) exercise
end-to-end:

* ``_json_type_to_python`` — the JSON-Schema → Python annotation map
  that FastMCP consumes when building each tool's argument model.  A
  missing row here silently makes the affected MCP parameter
  unreachable (the FastMCP Pydantic layer rejects valid input with
  a misleading "should be a valid string" error before the tool
  handler ever runs).

* ``_apply_get_status_wait`` — the async polling-delay shim that the
  ``get_status`` MCP tool exposes via the ``wait`` argument.  Edge
  cases around ``bool`` (``True is int(1)``), zero / negative, and
  the :data:`GET_STATUS_MAX_WAIT` cap.

* :class:`RateLimiter` — sliding-window limiter shared by every
  MCP tool call.  The class doc claims "single-event-loop usage
  means no concurrent calls" but FastMCP's stateless-HTTP mode
  dispatches tool calls into ``anyio.to_thread`` workers, so the
  limiter can be hit from multiple OS threads.  A concurrency test
  guards the invariant that the worst-case overshoot is bounded.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from mcp_server import (
    GET_STATUS_MAX_WAIT,
    RateLimiter,
    _apply_get_status_wait,
    _json_type_to_python,
)

# ---------------------------------------------------------------------------
# _json_type_to_python — JSON-Schema type → Python annotation
# ---------------------------------------------------------------------------


class TestJsonTypeToPython:
    """The mapping must cover every JSON-Schema ``type`` value used in
    ``PIPELINE_TOOLS``.  A missing row falls through to ``str``, which
    makes any non-string-shaped MCP parameter unreachable — Pydantic
    rejects the dict/list at the FastMCP boundary with a confusing
    "Input should be a valid string" error before the handler runs.

    Reproduction history: ``config`` (object) and ``roles`` (array)
    were both unreachable over MCP until the ``array``/``object`` rows
    were added.  This test locks in the full mapping.
    """

    @pytest.mark.parametrize(
        ("json_type", "expected"),
        [
            ("string", str),
            ("integer", int),
            ("number", float),
            ("boolean", bool),
            ("array", list),
            ("object", dict),
        ],
    )
    def test_known_types_map_correctly(self, json_type: str, expected: type) -> None:
        assert _json_type_to_python({"type": json_type}) is expected

    def test_unknown_type_falls_through_to_str(self) -> None:
        # The fallthrough is intentional — JSON-Schema types we don't
        # know about (``null``, union-type arrays, etc.) get a string
        # annotation rather than crashing the server.  We accept the
        # less-useful annotation in exchange for keeping the registration
        # path robust against schema drift.
        assert _json_type_to_python({"type": "something-future"}) is str

    def test_missing_type_defaults_to_string(self) -> None:
        # ``prop.get("type", "string")`` — a schema entry that omits
        # ``type`` (rare but valid JSON Schema) maps to ``str``.
        assert _json_type_to_python({}) is str


# ---------------------------------------------------------------------------
# _apply_get_status_wait — the get_status ``wait`` shim
# ---------------------------------------------------------------------------


def _run_async(coro):
    return asyncio.run(coro)


class TestApplyGetStatusWait:
    """The ``wait`` shim must:

    1. Only fire for ``get_status`` (other tools must not be perturbed).
    2. Reject ``bool`` values — ``True`` is ``isinstance(int)``, so a
       naive ``> 0`` test would sleep for 1 s on every ``wait=True``.
    3. Treat zero / negative / non-numeric values as no-op.
    4. Cap the sleep at :data:`GET_STATUS_MAX_WAIT` so a buggy client
       can't park the server past the streamable-HTTP timeout.
    5. Consume the ``wait`` kwarg so the handler doesn't see it.
    """

    def test_no_op_for_non_get_status_tool(self, monkeypatch) -> None:
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        kwargs = {"wait": 5}
        _run_async(_apply_get_status_wait("submit_task", kwargs))
        sleeper.assert_not_awaited()
        # ``wait`` is left alone for non-target tools (the only tool
        # that consumes it is get_status; other tools should never have
        # been called with it in the first place).
        assert kwargs == {"wait": 5}

    def test_positive_int_sleeps_and_consumes_wait(self, monkeypatch) -> None:
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        kwargs = {"wait": 3, "task_id": "x"}
        _run_async(_apply_get_status_wait("get_status", kwargs))
        sleeper.assert_awaited_once_with(3)
        assert "wait" not in kwargs

    def test_positive_float_sleeps(self, monkeypatch) -> None:
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        kwargs = {"wait": 1.5}
        _run_async(_apply_get_status_wait("get_status", kwargs))
        sleeper.assert_awaited_once_with(1.5)

    def test_wait_capped_at_max(self, monkeypatch) -> None:
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        kwargs = {"wait": GET_STATUS_MAX_WAIT * 10}
        _run_async(_apply_get_status_wait("get_status", kwargs))
        sleeper.assert_awaited_once_with(GET_STATUS_MAX_WAIT)

    @pytest.mark.parametrize("bad_wait", [True, False])
    def test_bool_does_not_sleep(self, monkeypatch, bad_wait: bool) -> None:
        # ``True`` and ``False`` are both ``int`` subclasses (``True == 1``,
        # ``False == 0``).  Without the explicit bool guard, ``wait=True``
        # would sleep 1 s on every poll.
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        _run_async(_apply_get_status_wait("get_status", {"wait": bad_wait}))
        sleeper.assert_not_awaited()

    @pytest.mark.parametrize("bad_wait", [0, -1, -5.0, "5", None, [5]])
    def test_zero_negative_or_non_numeric_no_op(self, monkeypatch, bad_wait) -> None:
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        _run_async(_apply_get_status_wait("get_status", {"wait": bad_wait}))
        sleeper.assert_not_awaited()

    def test_wait_missing_no_op(self, monkeypatch) -> None:
        sleeper = AsyncMock()
        monkeypatch.setattr("mcp_server._async_sleep", sleeper)
        _run_async(_apply_get_status_wait("get_status", {"task_id": "x"}))
        sleeper.assert_not_awaited()


# ---------------------------------------------------------------------------
# RateLimiter — sliding window correctness + concurrency
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Locks in the limiter's documented behavior and tests the
    thread-safety invariant against the actual deployed shape
    (stateless-HTTP tool calls run in ``anyio.to_thread`` workers, so
    the limiter is hit from multiple OS threads — not the event loop
    alone).  The lock added in #2669 must keep the limiter exact under
    that contention."""

    def test_allows_up_to_max(self) -> None:
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is False

    def test_window_expires(self, monkeypatch) -> None:
        # Drive the clock forward so the sliding window expires the
        # first call without sleeping in real time.
        now = [1_000_000.0]
        monkeypatch.setattr("mcp_server.time.time", lambda: now[0])

        limiter = RateLimiter(max_requests=1, window_seconds=10)
        assert limiter.allow() is True
        assert limiter.allow() is False  # second call within window rejected
        now[0] += 11  # advance past the window
        assert limiter.allow() is True  # first call has expired

    def test_threaded_burst_is_exact(self) -> None:
        """Under stateless-HTTP mode the limiter is hit from multiple OS
        threads.  With the lock added in #2669, the limiter must be
        exact under contention: a burst of ``num_threads`` workers must
        see exactly ``max_requests`` successes and the rest rejections,
        regardless of interpreter scheduling.

        A regression that removed the lock would re-introduce the
        prune-and-len race (each thread reads the same pruned state
        before any have appended) and produce ``allowed_count > max_requests``
        — exactly what this test pins against.
        """
        max_requests = 10
        num_threads = 50
        limiter = RateLimiter(max_requests=max_requests, window_seconds=60)
        results: list[bool] = []
        results_lock = threading.Lock()
        start_barrier = threading.Barrier(num_threads)

        def worker() -> None:
            start_barrier.wait()
            allowed = limiter.allow()
            with results_lock:
                results.append(allowed)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed_count = sum(1 for r in results if r)
        assert allowed_count == max_requests, (
            f"limiter allowed {allowed_count}, expected exactly {max_requests} "
            f"(num_threads={num_threads})"
        )
