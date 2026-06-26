"""Tests for the ``egg-orch session-state pull|push`` CLI (#3278).

Focus on the best-effort contract (never wedge the wrapper) and the
orchestrator round-trip; the slug/file logic is covered in
``test_session_state_sync.py``.
"""

import argparse
import json
import sys
from pathlib import Path

import pytest

_sandbox_path = Path(__file__).parent.parent
if str(_sandbox_path) not in sys.path:
    sys.path.insert(0, str(_sandbox_path))

from egg_lib import cli_session_state as cli
from egg_lib import orch_cli, session_state_sync


def _args(**kw: object) -> argparse.Namespace:
    ns = argparse.Namespace(session_state_file=None, repo_path=None, config_dir=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-1")
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
    monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path / "repo"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("EGG_SESSION_STATE_FILE", str(tmp_path / "state.json"))
    yield


class TestBestEffort:
    def test_pull_skips_without_identity(self, monkeypatch):
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)
        assert cli.cmd_session_state_pull(_args()) == 0

    def test_pull_swallows_orchestrator_failure(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("orchestrator down")

        monkeypatch.setattr(orch_cli, "orch_request", _boom)
        assert cli.cmd_session_state_pull(_args()) == 0

    def test_pull_miss_is_clean(self, monkeypatch):
        monkeypatch.setattr(orch_cli, "orch_request", lambda *a, **k: {"found": False})
        assert cli.cmd_session_state_pull(_args()) == 0

    def test_push_skips_when_no_session(self, monkeypatch):
        # No pointer file written → nothing to push.
        called = []
        monkeypatch.setattr(orch_cli, "orch_request", lambda *a, **k: called.append(1))
        assert cli.cmd_session_state_push(_args()) == 0
        assert called == []


class TestRoundTrip:
    def test_pull_writes_state_and_push_sends_it(self, monkeypatch, tmp_path):
        repo = str(tmp_path / "repo")
        cfg = str(tmp_path / "cfg")
        ssf = str(tmp_path / "state.json")

        # --- pull: orchestrator returns a stored session ---
        get_result = {
            "found": True,
            "data": {
                "session_id": "sid-1",
                "window_occupancy": 42,
                "transcript": '{"l":1}\n',
            },
        }
        monkeypatch.setattr(orch_cli, "orch_request", lambda *a, **k: get_result)
        assert cli.cmd_session_state_pull(_args()) == 0

        # Pointer + transcript materialised where --resume reads.
        assert json.loads(Path(ssf).read_text())["session_id"] == "sid-1"
        tpath = session_state_sync.transcript_path(cfg, repo, "sid-1")
        assert tpath.read_text() == '{"l":1}\n'

        # --- push: ships the (unchanged) state back, scoped to slice+role ---
        sent = {}

        def _capture(endpoint, method="GET", data=None, timeout=None):
            sent["endpoint"] = endpoint
            sent["method"] = method
            sent["data"] = data
            sent["timeout"] = timeout
            return {"success": True, "stored": True}

        monkeypatch.setattr(orch_cli, "orch_request", _capture)
        assert cli.cmd_session_state_push(_args()) == 0
        assert sent["method"] == "POST"
        assert sent["endpoint"] == "/api/v1/pipelines/issue-1/session-state"
        assert sent["data"]["session_id"] == "sid-1"
        assert sent["data"]["role"] == "coder"
        assert sent["data"]["slice_id"] == "slice-3"
        assert sent["data"]["transcript"] == '{"l":1}\n'
        # Transcript round-trip gets a payload-sized HTTP timeout, not the 15s default.
        assert sent["timeout"] == cli._SESSION_STATE_HTTP_TIMEOUT
