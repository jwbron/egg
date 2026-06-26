"""Tests for the Redis-backed cross-pod session-state store (#3278)."""

import sys
from pathlib import Path

import fakeredis
import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from session_state_store import (
    MAX_TRANSCRIPT_BYTES,
    SESSION_STATE_TTL_SECONDS,
    SessionStateStore,
)


@pytest.fixture
def store():
    return SessionStateStore(fakeredis.FakeRedis())


class TestRoundTrip:
    def test_put_then_get_returns_record(self, store):
        store.put(
            "issue-1",
            "slice-3",
            "coder",
            session_id="sid-abc",
            window_occupancy=123456,
            transcript='{"line": 1}\n',
        )
        rec = store.get("issue-1", "slice-3", "coder")
        assert rec is not None
        assert rec.session_id == "sid-abc"
        assert rec.window_occupancy == 123456
        assert rec.transcript == '{"line": 1}\n'

    def test_get_missing_returns_none(self, store):
        assert store.get("issue-1", "slice-3", "coder") is None

    def test_key_is_scoped_by_pipeline_slice_role(self, store):
        store.put("issue-1", "slice-3", "coder", session_id="a")
        # Any of pipeline / slice / role differing is a distinct record.
        assert store.get("issue-2", "slice-3", "coder") is None
        assert store.get("issue-1", "slice-4", "coder") is None
        assert store.get("issue-1", "slice-3", "reviewer_code") is None
        assert store.get("issue-1", "slice-3", "coder").session_id == "a"

    def test_none_slice_is_pipeline_level_and_distinct(self, store):
        store.put("issue-1", None, "coder", session_id="pipeline-level")
        store.put("issue-1", "slice-3", "coder", session_id="sliced")
        assert store.get("issue-1", None, "coder").session_id == "pipeline-level"
        assert store.get("issue-1", "slice-3", "coder").session_id == "sliced"

    def test_put_overwrites(self, store):
        store.put("issue-1", "slice-3", "coder", session_id="old", window_occupancy=1)
        store.put("issue-1", "slice-3", "coder", session_id="new", window_occupancy=2)
        rec = store.get("issue-1", "slice-3", "coder")
        assert rec.session_id == "new"
        assert rec.window_occupancy == 2


class TestTtl:
    def test_put_sets_ttl(self, store):
        store.put("issue-1", "slice-3", "coder", session_id="a")
        # fakeredis honours TTL; assert a positive ttl near the configured value.
        ttl = store._redis.ttl(SessionStateStore._key("issue-1", "slice-3", "coder"))
        assert 0 < ttl <= SESSION_STATE_TTL_SECONDS


class TestDefensiveContract:
    def test_empty_session_id_not_stored(self, store):
        assert store.put("issue-1", "slice-3", "coder", session_id="") is False
        assert store.get("issue-1", "slice-3", "coder") is None

    def test_pointer_only_record_round_trips(self, store):
        assert store.put("issue-1", "slice-3", "coder", session_id="a") is True
        rec = store.get("issue-1", "slice-3", "coder")
        assert rec.session_id == "a"
        assert rec.transcript is None
        assert rec.window_occupancy is None

    def test_oversized_transcript_dropped_but_pointer_kept(self, store):
        big = "x" * (MAX_TRANSCRIPT_BYTES + 1)
        assert store.put("issue-1", "slice-3", "coder", session_id="a", transcript=big) is True
        rec = store.get("issue-1", "slice-3", "coder")
        # Pointer survives; the oversized transcript is dropped (→ reseed).
        assert rec.session_id == "a"
        assert rec.transcript is None

    def test_malformed_payload_returns_none(self, store):
        store._redis.set(SessionStateStore._key("issue-1", "slice-3", "coder"), b"not json")
        assert store.get("issue-1", "slice-3", "coder") is None

    def test_occupancy_bool_coerced_to_none(self, store):
        store.put("issue-1", "slice-3", "coder", session_id="a", window_occupancy=True)
        assert store.get("issue-1", "slice-3", "coder").window_occupancy is None

    def test_read_failure_returns_none(self):
        class _Boom:
            def get(self, *_a, **_k):
                raise RuntimeError("redis down")

        store = SessionStateStore(_Boom())
        assert store.get("issue-1", "slice-3", "coder") is None

    def test_write_failure_returns_false(self):
        class _Boom:
            def setex(self, *_a, **_k):
                raise RuntimeError("redis down")

        store = SessionStateStore(_Boom())
        assert store.put("issue-1", "slice-3", "coder", session_id="a") is False
