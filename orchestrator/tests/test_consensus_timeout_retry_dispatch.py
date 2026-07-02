"""Regression tests for the consensus-timeout HITL retry dispatch (#3421).

The incomplete-consensus HITL (``consensus_timeout_incomplete`` context) is
persisted moments before the driver marks the pipeline FAILED and exits, so
nothing ever waited on it: resolving it with "Retry phase" was a silent
no-op — the decision flipped to RESOLVED, the pipeline stayed ``failed``, and
no agents respawned.  The resolve path now dispatches "Retry phase" through
the ``restart_phase`` route in-process (the documented manual workaround),
and the Accept/Abort options resolve with an explicit note instead of
silently.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from models import DecisionStatus, HITLDecision, PipelinePhase  # noqa: E402
from routes.pipelines import _CONSENSUS_TIMEOUT_HITL_CONTEXT  # noqa: E402

# Import the production constant rather than duplicating the literal so the
# coupling is explicit: if the context discriminator is ever renamed, these
# tests build decisions with the new value instead of drifting to a stale one.
CONSENSUS_TIMEOUT_CONTEXT = _CONSENSUS_TIMEOUT_HITL_CONTEXT


def _decision(
    resolution: str | None = "Retry phase",
    context: str = CONSENSUS_TIMEOUT_CONTEXT,
    phase: PipelinePhase | None = PipelinePhase.IMPLEMENT,
) -> HITLDecision:
    return HITLDecision(
        id="decision-7",
        question="Consensus timed out; consensus incomplete. How to proceed?",
        context=context,
        options=["Retry phase", "Accept current state", "Abort phase"],
        phase=phase,
        status=DecisionStatus.RESOLVED,
        resolution=resolution,
    )


class TestDispatchConsensusTimeoutResolution:
    """Handler-level dispatch logic."""

    @patch("routes.pipelines.restart_phase")
    def test_retry_phase_calls_restart_phase(self, mock_restart):
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        mock_restart.return_value = (MagicMock(), 200)

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(), "Retry phase"
        )

        mock_restart.assert_called_once_with("issue-3393", "implement")
        assert result == {"action": "restart_phase", "phase": "implement", "success": True}

    @patch("routes.pipelines.restart_phase")
    def test_retry_surfaces_restart_failure(self, mock_restart):
        """A restart that cannot act must say so, not resolve silently."""
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        resp = MagicMock()
        resp.get_json.return_value = {
            "success": False,
            "message": "Pipeline issue-3393 is not in a restartable state (status: complete)",
        }
        mock_restart.return_value = (resp, 409)

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(), "Retry phase"
        )

        assert result["action"] == "restart_phase"
        assert result["success"] is False
        assert "not in a restartable state" in result["error"]

    @patch("routes.pipelines.restart_phase")
    def test_retry_surfaces_restart_exception(self, mock_restart):
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        mock_restart.side_effect = RuntimeError("spawner unavailable")

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(), "Retry phase"
        )

        assert result["success"] is False
        assert "spawner unavailable" in result["error"]

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.pipelines.restart_phase")
    def test_retry_falls_back_to_current_phase_when_unpinned(self, mock_restart, mock_get_store):
        """A decision without a pinned phase restarts the pipeline's current phase."""
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        pipeline = MagicMock()
        pipeline.current_phase = PipelinePhase.PLAN
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (store, MagicMock())
        mock_restart.return_value = (MagicMock(), 200)

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(phase=None), "Retry phase"
        )

        mock_restart.assert_called_once_with("issue-3393", "plan")
        assert result["success"] is True

    @patch("routes.pipelines.restart_phase")
    def test_abort_phase_resolves_with_explicit_note(self, mock_restart):
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(), "Abort phase"
        )

        mock_restart.assert_not_called()
        assert result["action"] == "consensus_timeout_abort"
        assert result["success"] is True
        assert "note" in result

    @patch("routes.pipelines.restart_phase")
    def test_accept_current_state_resolves_with_explicit_note(self, mock_restart):
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(), "Accept current state"
        )

        mock_restart.assert_not_called()
        assert result["action"] == "consensus_timeout_accept"
        assert "advance_phase" in result["note"]

    @patch("routes.pipelines.restart_phase")
    def test_non_consensus_timeout_decision_is_ignored(self, mock_restart):
        """Only the consensus-timeout context triggers the dispatch — other
        decisions sharing the option label (e.g. the #1691 failed-reviewer
        HITL) keep their existing record-only semantics."""
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(context=""), "Retry phase"
        )

        mock_restart.assert_not_called()
        assert result is None

    @patch("routes.pipelines.restart_phase")
    def test_free_form_resolution_is_ignored(self, mock_restart):
        from routes.decisions import _maybe_dispatch_consensus_timeout_resolution

        result = _maybe_dispatch_consensus_timeout_resolution(
            "issue-3393", _decision(), "let me think about this"
        )

        mock_restart.assert_not_called()
        assert result is None


class TestResolveRouteDispatchesConsensusTimeout:
    """The resolve-decision route wires the dispatch into the queue path."""

    @pytest.fixture
    def client(self):
        from flask import Flask
        from routes.decisions import decisions_bp

        app = Flask(__name__)
        app.register_blueprint(decisions_bp)
        app.config["TESTING"] = True
        return app.test_client()

    @patch("routes.pipelines.restart_phase")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_retry_phase_executes_restart(
        self, mock_get_queue, mock_get_store, mock_restart, client, tmp_path
    ):
        """The concrete #3421 instance: a ``{"action": "select", "selected":
        "Retry phase"}`` envelope on the consensus-timeout decision of a
        failed pipeline must restart the phase, not resolve silently."""
        mock_get_store.return_value = (MagicMock(repo_path=tmp_path), MagicMock())
        queue = MagicMock()
        queue.resolve_decision.return_value = _decision(
            resolution='{"action": "select", "selected": "Retry phase"}'
        )
        mock_get_queue.return_value = queue
        mock_restart.return_value = (MagicMock(), 200)

        resp = client.post(
            "/api/v1/pipelines/issue-3393/decisions/decision-7/resolve",
            json={"resolution": {"action": "select", "selected": "Retry phase"}},
        )

        assert resp.status_code == 200
        mock_restart.assert_called_once_with("issue-3393", "implement")
        body = resp.get_json()
        assert body["data"]["executed_action"] == {
            "action": "restart_phase",
            "phase": "implement",
            "success": True,
        }

    @patch("routes.pipelines.restart_phase")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_retry_failure_is_surfaced_in_response(
        self, mock_get_queue, mock_get_store, mock_restart, client, tmp_path
    ):
        mock_get_store.return_value = (MagicMock(repo_path=tmp_path), MagicMock())
        queue = MagicMock()
        queue.resolve_decision.return_value = _decision(resolution="Retry phase")
        mock_get_queue.return_value = queue
        failure_resp = MagicMock()
        failure_resp.get_json.return_value = {"success": False, "message": "no agents to restart"}
        mock_restart.return_value = (failure_resp, 400)

        resp = client.post(
            "/api/v1/pipelines/issue-3393/decisions/decision-7/resolve",
            json={"resolution": "Retry phase"},
        )

        assert resp.status_code == 200  # decision itself still resolved
        executed = resp.get_json()["data"]["executed_action"]
        assert executed["success"] is False
        assert "no agents to restart" in executed["error"]
