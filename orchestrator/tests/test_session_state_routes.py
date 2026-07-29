"""Tests for the session-state push/pull endpoints (#3278)."""

import sys
from pathlib import Path

import fakeredis
import pytest
from flask import Flask

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

import session_state_store
from routes.session_state import session_state_bp
from session_state_store import SessionStateStore


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(session_state_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(autouse=True)
def _fakeredis_store():
    session_state_store.set_session_state_store(SessionStateStore(fakeredis.FakeRedis()))
    yield
    session_state_store.reset_session_state_store()


_URL = "/api/v1/pipelines/issue-1/session-state"


class TestPushPullRoundTrip:
    def test_push_then_pull(self, client):
        push = client.post(
            _URL,
            json={
                "role": "coder",
                "slice_id": "slice-3",
                "session_id": "sid-abc",
                "window_occupancy": 99,
                "transcript": '{"l": 1}\n',
            },
        )
        assert push.status_code == 200
        assert push.get_json()["stored"] is True

        pull = client.get(_URL, query_string={"role": "coder", "slice_id": "slice-3"})
        assert pull.status_code == 200
        body = pull.get_json()
        assert body["found"] is True
        assert body["data"]["session_id"] == "sid-abc"
        assert body["data"]["window_occupancy"] == 99
        assert body["data"]["transcript"] == '{"l": 1}\n'

    def test_pull_miss_returns_found_false_not_404(self, client):
        pull = client.get(_URL, query_string={"role": "coder", "slice_id": "slice-9"})
        assert pull.status_code == 200
        assert pull.get_json()["found"] is False

    def test_pull_scopes_by_slice_and_role(self, client):
        client.post(_URL, json={"role": "coder", "slice_id": "slice-3", "session_id": "a"})
        # Wrong slice / role → miss.
        assert (
            client.get(_URL, query_string={"role": "coder", "slice_id": "slice-4"}).get_json()[
                "found"
            ]
            is False
        )
        assert (
            client.get(
                _URL, query_string={"role": "reviewer_code", "slice_id": "slice-3"}
            ).get_json()["found"]
            is False
        )

    def test_pipeline_level_pull_omits_slice(self, client):
        client.post(_URL, json={"role": "coder", "session_id": "pl"})
        body = client.get(_URL, query_string={"role": "coder"}).get_json()
        assert body["found"] is True
        assert body["data"]["session_id"] == "pl"


class TestIndex:
    """Operator-facing index of stored records (#3547)."""

    _INDEX_URL = f"{_URL}/index"

    def test_empty_index(self, client):
        r = client.get(self._INDEX_URL)
        assert r.status_code == 200
        assert r.get_json() == {"success": True, "records": []}

    def test_index_lists_metadata_without_transcripts(self, client):
        client.post(
            _URL,
            json={
                "role": "coder",
                "slice_id": "slice-3",
                "session_id": "sid-a",
                "window_occupancy": 42,
                "transcript": '{"l": 1}\n',
            },
        )
        client.post(_URL, json={"role": "reviewer_code", "session_id": "sid-b"})

        records = client.get(self._INDEX_URL).get_json()["records"]
        assert len(records) == 2
        by_role = {r["role"]: r for r in records}
        coder = by_role["coder"]
        assert coder["slice_id"] == "slice-3"
        assert coder["session_id"] == "sid-a"
        assert coder["window_occupancy"] == 42
        assert coder["transcript_bytes"] == len('{"l": 1}\n')
        assert "transcript" not in coder
        reviewer = by_role["reviewer_code"]
        assert reviewer["slice_id"] is None
        assert reviewer["transcript_bytes"] == 0

    def test_index_scoped_to_pipeline(self, client):
        client.post(_URL, json={"role": "coder", "session_id": "a"})
        other = client.get("/api/v1/pipelines/issue-2/session-state/index").get_json()
        assert other["records"] == []


class TestValidation:
    def test_push_requires_role(self, client):
        r = client.post(_URL, json={"session_id": "a"})
        assert r.status_code == 400

    def test_push_requires_session_id(self, client):
        r = client.post(_URL, json={"role": "coder"})
        assert r.status_code == 400

    def test_push_rejects_non_object_body(self, client):
        r = client.post(_URL, json=["not", "an", "object"])
        assert r.status_code == 400

    def test_push_rejects_bad_occupancy(self, client):
        r = client.post(_URL, json={"role": "coder", "session_id": "a", "window_occupancy": "big"})
        assert r.status_code == 400

    def test_pull_requires_role(self, client):
        r = client.get(_URL)
        assert r.status_code == 400


class TestTranscriptProvenance:
    """The advisory block describing which transcript a push carried (#3692).

    Advisory means it never gates the push. It does NOT mean the values reach
    the structured log line unchecked — every other field on this route is
    type-checked, and these are the only client-supplied ones that end up in a
    log stream."""

    def test_a_well_formed_block_is_accepted(self, client):
        r = client.post(
            _URL,
            json={
                "role": "coder",
                "session_id": "a",
                "transcript": '{"l": 1}\n',
                "transcript_provenance": {
                    "tail_timestamp": "2026-07-29T04:21:34.335Z",
                    "entries": 3472,
                    "assistant_turns": 1837,
                    "bytes": 4788605,
                },
            },
        )
        assert r.status_code == 200
        assert r.get_json()["stored"] is True

    def test_a_malformed_block_does_not_fail_the_push(self, client):
        # The transcript is the thing worth keeping; a bad description of it
        # must not be able to cost us the record.
        r = client.post(
            _URL,
            json={
                "role": "coder",
                "session_id": "a",
                "transcript": '{"l": 1}\n',
                "transcript_provenance": "not an object",
            },
        )
        assert r.status_code == 200
        assert r.get_json()["stored"] is True

    def test_wrong_typed_members_are_dropped_not_forwarded(self):
        from routes.session_state import _advisory_int, _advisory_str

        block = {
            "tail_timestamp": {"nested": "object"},
            "entries": "3472",
            "assistant_turns": True,
            "bytes": 12,
        }
        assert _advisory_str(block, "tail_timestamp") is None
        assert _advisory_int(block, "entries") is None
        # `isinstance(True, int)` is True, and `assistant_turns: true` is not
        # a count.
        assert _advisory_int(block, "assistant_turns") is None
        assert _advisory_int(block, "bytes") == 12
        assert _advisory_str({}, "tail_timestamp") is None

    def test_an_unbounded_string_is_capped(self):
        from routes.session_state import _MAX_PROVENANCE_STR, _advisory_str

        value = _advisory_str({"tail_timestamp": "x" * 10_000}, "tail_timestamp")
        assert value is not None and len(value) == _MAX_PROVENANCE_STR


class TestEvict:
    """#3537 DELETE - operator-facing eviction of a poisoned warm-resume record."""

    def test_evict_removes_record(self, client):
        client.post(_URL, json={"role": "coder", "slice_id": "slice-3", "session_id": "sid"})
        resp = client.delete(_URL, query_string={"role": "coder", "slice_id": "slice-3"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["deleted"] is True
        pull = client.get(_URL, query_string={"role": "coder", "slice_id": "slice-3"})
        assert pull.get_json()["found"] is False

    def test_evict_miss_is_success_with_deleted_false(self, client):
        resp = client.delete(_URL, query_string={"role": "coder", "slice_id": "slice-9"})
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, "deleted": False}

    def test_evict_requires_role(self, client):
        resp = client.delete(_URL)
        assert resp.status_code == 400

    def test_evict_is_scoped(self, client):
        client.post(_URL, json={"role": "coder", "slice_id": "slice-3", "session_id": "a"})
        client.post(_URL, json={"role": "tester", "slice_id": "slice-3", "session_id": "b"})
        client.delete(_URL, query_string={"role": "coder", "slice_id": "slice-3"})
        keep = client.get(_URL, query_string={"role": "tester", "slice_id": "slice-3"})
        assert keep.get_json()["found"] is True

    def test_evict_pipeline_level_omits_slice(self, client):
        client.post(_URL, json={"role": "coder", "session_id": "pipeline-level"})
        resp = client.delete(_URL, query_string={"role": "coder"})
        assert resp.get_json()["deleted"] is True
        assert client.get(_URL, query_string={"role": "coder"}).get_json()["found"] is False
