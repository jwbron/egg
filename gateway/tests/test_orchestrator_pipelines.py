"""Tests for gateway/orchestrator_pipelines.py (#3070).

The client's one hard invariant: failure is ``None``, never an empty
set, so worktree cleanup can tell "verified nothing active" apart from
"could not verify" and fail safe.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import URLError

from orchestrator_pipelines import (
    fetch_active_pipeline_ids,
    wait_for_active_pipeline_ids,
)


def _response(payload: dict) -> io.BytesIO:
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    body.__enter__ = lambda *a: body  # type: ignore[method-assign]
    body.__exit__ = lambda *a: False  # type: ignore[method-assign]
    return body


class TestFetchActivePipelineIds:
    def test_returns_ids_on_success(self):
        payload = {
            "success": True,
            "data": {
                "pipelines": [
                    {"id": "pipeline-c978dac3", "status": "awaiting_human"},
                    {"id": "issue-3023", "status": "running"},
                ]
            },
        }
        with patch("orchestrator_pipelines.urlopen", return_value=_response(payload)) as mock:
            ids = fetch_active_pipeline_ids()

        assert ids == {"pipeline-c978dac3", "issue-3023"}
        url = mock.call_args[0][0].full_url
        assert url.endswith("/api/v1/pipelines?active_only=true")

    def test_returns_empty_set_when_no_active_pipelines(self):
        payload = {"success": True, "data": {"pipelines": []}}
        with patch("orchestrator_pipelines.urlopen", return_value=_response(payload)):
            assert fetch_active_pipeline_ids() == set()

    def test_returns_none_on_network_error(self):
        with patch("orchestrator_pipelines.urlopen", side_effect=URLError("refused")):
            assert fetch_active_pipeline_ids() is None

    def test_returns_none_on_malformed_body(self):
        body = io.BytesIO(b"not json")
        body.__enter__ = lambda *a: body  # type: ignore[method-assign]
        body.__exit__ = lambda *a: False  # type: ignore[method-assign]
        with patch("orchestrator_pipelines.urlopen", return_value=body):
            assert fetch_active_pipeline_ids() is None

    def test_returns_none_on_missing_data_key(self):
        with patch(
            "orchestrator_pipelines.urlopen",
            return_value=_response({"success": True, "data": {}}),
        ):
            assert fetch_active_pipeline_ids() is None

    def test_url_from_env(self, monkeypatch):
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orch.test:9849/")
        payload = {"success": True, "data": {"pipelines": []}}
        with patch("orchestrator_pipelines.urlopen", return_value=_response(payload)) as mock:
            fetch_active_pipeline_ids()
        assert mock.call_args[0][0].full_url.startswith("http://orch.test:9849/api")


class TestWaitForActivePipelineIds:
    def test_returns_immediately_on_success(self):
        with patch(
            "orchestrator_pipelines.fetch_active_pipeline_ids",
            return_value={"issue-1"},
        ) as mock:
            assert wait_for_active_pipeline_ids(max_wait_seconds=60) == {"issue-1"}
        assert mock.call_count == 1

    def test_retries_until_success(self):
        with (
            patch(
                "orchestrator_pipelines.fetch_active_pipeline_ids",
                side_effect=[None, None, set()],
            ) as mock,
            patch("orchestrator_pipelines.time.sleep") as sleep,
        ):
            assert wait_for_active_pipeline_ids(max_wait_seconds=60) == set()
        assert mock.call_count == 3
        assert sleep.call_count == 2

    def test_returns_none_at_deadline(self):
        with (
            patch("orchestrator_pipelines.fetch_active_pipeline_ids", return_value=None),
            patch(
                "orchestrator_pipelines.time.monotonic",
                side_effect=[0.0, 100.0],
            ),
        ):
            assert wait_for_active_pipeline_ids(max_wait_seconds=50) is None

    def test_deadline_from_env(self, monkeypatch):
        monkeypatch.setenv("EGG_CLEANUP_ORCHESTRATOR_WAIT_SECONDS", "0")
        with patch("orchestrator_pipelines.fetch_active_pipeline_ids", return_value=None) as mock:
            assert wait_for_active_pipeline_ids() is None
        assert mock.call_count == 1
