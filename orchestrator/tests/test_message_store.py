"""Unit tests for the ``message_store`` module: shared types + backend creation.

The in-memory ``MessageStore`` backend this file used to cover was removed
in #3159 — Redis Streams (``redis_message_store.RedisMessageStore``) is the
only backend, and its behavioral contracts (blocking reads, filters, cursor
staleness, wipe semantics) are pinned by ``test_redis_message_store.py``
against fakeredis. What remains here:

- the shared type surface (:class:`MessageType`, ``HEARTBEAT_STATES``,
  :func:`coerce_deprecated_message_type`);
- ``_create_message_store`` fail-loud semantics: ``redis`` / unset selects
  Redis, the removed multi-backend-era values (``memory`` / ``auto``) and
  unknown values raise, and a Redis connection failure propagates instead
  of falling back;
- ``get_message_store`` / ``reset_message_store`` singleton semantics.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

import message_store  # noqa: E402
from message_store import (  # noqa: E402
    HEARTBEAT_STATES,
    MessageType,
    coerce_deprecated_message_type,
    get_message_store,
    reset_message_store,
)

# The conftest session fixture rebinds ``message_store._create_message_store``
# to a fakeredis-backed creator for the whole test session (fixtures run at
# test setup). This module-level reference is taken at collection time, so it
# is the REAL production creator — the selection tests below exercise it
# directly instead of going through the patched module attribute.
_real_create_message_store = message_store._create_message_store


class TestHeartbeatTypeExposure:
    """``HEARTBEAT`` is a first-class message type (issue #1897)."""

    def test_heartbeat_type_defined(self) -> None:
        assert MessageType.HEARTBEAT == "HEARTBEAT"

    def test_heartbeat_states_constant(self) -> None:
        assert "WORKING" in HEARTBEAT_STATES
        assert "WAITING_ON_ROLE" in HEARTBEAT_STATES
        assert "WAITING_FOR_EVENT" in HEARTBEAT_STATES
        assert "PROPOSED" in HEARTBEAT_STATES
        assert "IDLE" in HEARTBEAT_STATES
        assert len(HEARTBEAT_STATES) == 5

    def test_heartbeat_states_frozen(self) -> None:
        # Should be a frozenset so callers can't mutate.
        assert isinstance(HEARTBEAT_STATES, frozenset)


class TestCoerceDeprecatedMessageType:
    """Replay-safety coercion for removed message types (#1897 Phase 7)."""

    def test_question_coerces_to_progress(self) -> None:
        assert coerce_deprecated_message_type("QUESTION") == MessageType.PROGRESS

    def test_live_type_passes_through(self) -> None:
        assert coerce_deprecated_message_type("HEARTBEAT") == "HEARTBEAT"

    def test_unknown_type_passes_through_opaque(self) -> None:
        assert coerce_deprecated_message_type("SOME_FUTURE_TYPE") == "SOME_FUTURE_TYPE"


class TestBackendCreation:
    """``_create_message_store`` is redis-only and fails loudly (#3159).

    Each test calls the real (pre-conftest-patch) creator captured at
    module import; ``get_redis_message_store`` is mocked so no test
    touches a network socket.
    """

    def test_unset_env_selects_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset ``EGG_MESSAGE_STORE_BACKEND`` means redis — there is
        nothing else the variable could select."""
        monkeypatch.delenv("EGG_MESSAGE_STORE_BACKEND", raising=False)
        monkeypatch.setenv("REDIS_HOST", "redis.example")
        monkeypatch.setenv("REDIS_PORT", "6380")
        monkeypatch.setenv("REDIS_MESSAGE_DB", "3")
        sentinel = MagicMock(name="redis-store")
        with patch("redis_message_store.get_redis_message_store", return_value=sentinel) as mk:
            store = _real_create_message_store()
        assert store is sentinel
        mk.assert_called_once_with(host="redis.example", port=6380, db=3)

    def test_explicit_redis_selects_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EGG_MESSAGE_STORE_BACKEND", "redis")
        sentinel = MagicMock(name="redis-store")
        with patch("redis_message_store.get_redis_message_store", return_value=sentinel):
            assert _real_create_message_store() is sentinel

    @pytest.mark.parametrize("removed_value", ["memory", "auto"])
    def test_removed_backend_values_raise(
        self, monkeypatch: pytest.MonkeyPatch, removed_value: str
    ) -> None:
        """The multi-backend-era values are stale configuration: they
        must raise rather than silently mean something different than
        they used to (the auto→memory fallback / explicit in-memory)."""
        monkeypatch.setenv("EGG_MESSAGE_STORE_BACKEND", removed_value)
        with (
            patch("redis_message_store.get_redis_message_store") as mk,
            pytest.raises(RuntimeError, match=r"#3159"),
        ):
            _real_create_message_store()
        mk.assert_not_called()

    def test_unknown_backend_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EGG_MESSAGE_STORE_BACKEND", "carrier-pigeon")
        with pytest.raises(RuntimeError, match=r"carrier-pigeon"):
            _real_create_message_store()

    def test_redis_failure_propagates_no_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression lock carried over from explicit-redis mode: a
        connection failure raises — there is no backend to fall back to."""
        monkeypatch.setenv("EGG_MESSAGE_STORE_BACKEND", "redis")
        boom = ConnectionError("Cannot connect to Redis at redis.example:6379")
        with (
            patch("redis_message_store.get_redis_message_store", side_effect=boom),
            pytest.raises(ConnectionError, match="Cannot connect to Redis"),
        ):
            _real_create_message_store()


class TestSingletonAccessor:
    """``get_message_store`` caches; ``reset_message_store`` re-arms."""

    def test_get_returns_cached_instance(self) -> None:
        reset_message_store()
        try:
            first = get_message_store()
            assert get_message_store() is first
        finally:
            reset_message_store()

    def test_reset_builds_fresh_instance(self) -> None:
        reset_message_store()
        try:
            first = get_message_store()
            reset_message_store()
            assert get_message_store() is not first
        finally:
            reset_message_store()
