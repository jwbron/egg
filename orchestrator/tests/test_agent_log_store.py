"""Tests for the post-reap agent-log store (#3547)."""

import sys
from pathlib import Path

import fakeredis
import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from agent_log_store import (
    AGENT_LOG_TTL_SECONDS,
    MAX_LOG_BYTES,
    AgentLogStore,
)


@pytest.fixture
def store():
    return AgentLogStore(fakeredis.FakeRedis())


class TestRoundTrip:
    def test_put_then_get(self, store):
        assert (
            store.put(
                "issue-1",
                "egg-agent-issue-1-coder-abc",
                logs="hello\nworld\n",
                agent_role="coder",
                slice_id="slice-3",
                exit_code=137,
            )
            is True
        )
        rec = store.get("issue-1", "egg-agent-issue-1-coder-abc")
        assert rec["logs"] == "hello\nworld\n"
        assert rec["agent_role"] == "coder"
        assert rec["slice_id"] == "slice-3"
        assert rec["exit_code"] == 137
        assert rec["truncated"] is False
        assert rec["captured_at"]

    def test_miss_returns_none(self, store):
        assert store.get("issue-1", "nope") is None

    def test_requires_pipeline_and_job(self, store):
        assert store.put("", "job", logs="x") is False
        assert store.put("issue-1", "", logs="x") is False

    def test_ttl_applied(self, store):
        store.put("issue-1", "job-1", logs="x")
        ttl = store._redis.ttl(AgentLogStore._key("issue-1", "job-1"))
        assert 0 < ttl <= AGENT_LOG_TTL_SECONDS

    def test_oversized_log_tail_truncated(self, store):
        logs = "a" * MAX_LOG_BYTES + "TAIL"
        store.put("issue-1", "job-1", logs=logs)
        rec = store.get("issue-1", "job-1")
        assert rec["truncated"] is True
        assert rec["logs"].endswith("TAIL")
        assert len(rec["logs"].encode()) == MAX_LOG_BYTES


class TestListRecords:
    def test_index_newest_first_without_bodies(self, store):
        store.put(
            "issue-1",
            "job-old",
            logs="old",
            agent_role="coder",
            captured_at="2026-07-07T01:00:00+00:00",
        )
        store.put(
            "issue-1",
            "job-new",
            logs="newer!",
            agent_role="reviewer_code",
            captured_at="2026-07-07T02:00:00+00:00",
        )
        store.put("issue-2", "job-other", logs="other")

        records = store.list_records("issue-1")
        assert [r["job_name"] for r in records] == ["job-new", "job-old"]
        assert all("logs" not in r for r in records)
        assert records[0]["log_bytes"] == len("newer!")

    def test_include_logs_keeps_bodies(self, store):
        store.put("issue-1", "job-1", logs="body")
        records = store.list_records("issue-1", include_logs=True)
        assert records[0]["logs"] == "body"

    def test_scan_failure_returns_empty(self):
        class _Boom:
            def scan_iter(self, *_a, **_k):
                raise RuntimeError("redis down")

        assert AgentLogStore(_Boom()).list_records("issue-1") == []


class TestDefensiveContract:
    def test_write_failure_returns_false(self):
        class _Boom:
            def setex(self, *_a, **_k):
                raise RuntimeError("redis down")

        assert AgentLogStore(_Boom()).put("issue-1", "job", logs="x") is False

    def test_read_failure_returns_none(self):
        class _Boom:
            def get(self, *_a, **_k):
                raise RuntimeError("redis down")

        assert AgentLogStore(_Boom()).get("issue-1", "job") is None

    def test_malformed_payload_returns_none(self, store):
        store._redis.set(AgentLogStore._key("issue-1", "job"), b"not json")
        assert store.get("issue-1", "job") is None
