"""#1939 — phase lifecycle error responses must carry a stable `reason` code.

Two of the advance/complete 409 gates (health-check FAIL_PIPELINE vs.
unresolved HITL) previously collapsed into an indistinguishable "the server
said 409" on the caller's side. These tests pin the reason-code contract so
future callers can switch on `reason` without parsing the human message.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from flask import Flask
from models import DecisionStatus, HITLDecision, Pipeline, PipelinePhase, PipelineStatus
from routes.phases import phases_bp
from state_store import InvalidPipelineIdError, PipelineNotFoundError, VersionConflictError


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_pipeline(
    phase=PipelinePhase.PLAN,
    phase_status=PipelineStatus.COMPLETE,
    has_contract=False,
):
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
        has_contract=has_contract,
    )
    pipeline.current_phase = phase
    pipeline.get_phase_execution(phase).status = phase_status
    return pipeline


def _body(resp):
    return json.loads(resp.data)


class TestAdvancePhaseReasonCodes:
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_missing_target_phase(self, mock_get_store, client):
        # Reached before pipeline load — no store needed.
        resp = client.post("/api/v1/pipelines/issue-42/phase", json={})
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "missing_target_phase"
        mock_get_store.assert_not_called()

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_phase(self, mock_get_store, client):
        resp = client.post(
            "/api/v1/pipelines/issue-42/phase",
            json={"target_phase": "bogus"},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_phase"
        mock_get_store.assert_not_called()

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_phase_transition(self, mock_get_store, client):
        # REFINE -> PR is not a valid transition (REFINE can go to PLAN or IMPLEMENT)
        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        mock_get_store.return_value = (MagicMock(repo_path=Path("/tmp/repo")), pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase",
            json={"target_phase": "pr"},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_phase_transition"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_previous_phase_not_complete(self, mock_get_store, client):
        # Current phase is RUNNING — advance must be rejected.
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN, phase_status=PipelineStatus.RUNNING)
        mock_get_store.return_value = (MagicMock(repo_path=Path("/tmp/repo")), pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase",
            json={"target_phase": "implement"},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "previous_phase_not_complete"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_health_checks_failed_409(self, mock_get_store, app, client):
        """FAIL_PIPELINE health result surfaces as reason=health_checks_failed."""
        from health_checks.types import HealthAction, HealthResult, HealthStatus, HealthTier

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_get_store.return_value = (MagicMock(repo_path=Path("/tmp/repo")), pipeline)

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="check-1",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="broken",
                action=HealthAction.FAIL_PIPELINE,
            ),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase",
            json={"target_phase": "implement"},
        )
        assert resp.status_code == 409
        body = _body(resp)
        assert body["reason"] == "health_checks_failed"
        # Backward-compat: details.health_results must still be present.
        assert "health_results" in body["details"]

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_pipeline_id(self, mock_get_store, client):
        mock_get_store.side_effect = InvalidPipelineIdError("bad id")
        resp = client.post(
            "/api/v1/pipelines/bad-id/phase",
            json={"target_phase": "implement"},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_pipeline_id"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_not_found(self, mock_get_store, client):
        mock_get_store.side_effect = PipelineNotFoundError("issue-99")
        resp = client.post(
            "/api/v1/pipelines/issue-99/phase",
            json={"target_phase": "implement"},
        )
        assert resp.status_code == 404
        assert _body(resp)["reason"] == "pipeline_not_found"


class TestStartPhaseReasonCodes:
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_phase_already_running(self, mock_get_store, client):
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT, phase_status=PipelineStatus.RUNNING
        )
        mock_get_store.return_value = (MagicMock(), pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/start")
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "phase_already_running"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_version_conflict(self, mock_get_store, client):
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT, phase_status=PipelineStatus.PENDING
        )
        mock_store = MagicMock()
        mock_store.save_pipeline.side_effect = VersionConflictError("boom")
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/start")
        assert resp.status_code == 409
        assert _body(resp)["reason"] == "version_conflict"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_pipeline_id(self, mock_get_store, client):
        mock_get_store.side_effect = InvalidPipelineIdError("bad id")
        resp = client.post("/api/v1/pipelines/bad-id/phase/start")
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_pipeline_id"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_not_found(self, mock_get_store, client):
        mock_get_store.side_effect = PipelineNotFoundError("issue-99")
        resp = client.post("/api/v1/pipelines/issue-99/phase/start")
        assert resp.status_code == 404
        assert _body(resp)["reason"] == "pipeline_not_found"


class TestCompletePhaseReasonCodes:
    """The core motivation for #1939 — the `unresolved_hitl_decisions` 409 must
    be machine-distinguishable from `health_checks_failed` (which fires from a
    different endpoint, but shares the 409 status)."""

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_unresolved_hitl_decisions_409(self, mock_get_store, _mock_clear, client):
        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        pipeline.decisions.append(
            HITLDecision(
                id="decision-1",
                question="q",
                phase=PipelinePhase.REFINE,
                status=DecisionStatus.PENDING,
            )
        )
        mock_get_store.return_value = (MagicMock(repo_path=Path("/tmp/repo")), pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")
        assert resp.status_code == 409
        body = _body(resp)
        assert body["reason"] == "unresolved_hitl_decisions"
        # Backward-compat: details.unresolved_decision_ids must still be present.
        assert body["details"]["unresolved_decision_ids"] == ["decision-1"]

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_artifacts_non_dict(self, mock_get_store, client):
        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": "not-a-dict"},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_artifacts"
        mock_get_store.assert_not_called()

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_artifacts_non_string_values(self, mock_get_store, client):
        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": {"k": 123}},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_artifacts"
        mock_get_store.assert_not_called()

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_force_reason(self, mock_get_store, client):
        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"force_reason": 42},
        )
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_force_reason"
        mock_get_store.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_version_conflict(self, mock_get_store, _mock_clear, client):
        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.save_pipeline.side_effect = VersionConflictError("boom")
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")
        assert resp.status_code == 409
        assert _body(resp)["reason"] == "version_conflict"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_pipeline_id(self, mock_get_store, client):
        mock_get_store.side_effect = InvalidPipelineIdError("bad id")
        resp = client.post("/api/v1/pipelines/bad-id/phase/complete")
        assert resp.status_code == 400
        assert _body(resp)["reason"] == "invalid_pipeline_id"

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_not_found(self, mock_get_store, client):
        mock_get_store.side_effect = PipelineNotFoundError("issue-99")
        resp = client.post("/api/v1/pipelines/issue-99/phase/complete")
        assert resp.status_code == 404
        assert _body(resp)["reason"] == "pipeline_not_found"


class TestTwo409sAreDistinguishable:
    """The whole point: advance_phase 409 vs. complete_phase 409 must carry
    distinct reason codes a caller can switch on.
    """

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_409_reasons_are_distinct(self, mock_get_store, _mock_clear, app, client):
        from health_checks.types import HealthAction, HealthResult, HealthStatus, HealthTier

        # --- Scenario A: advance_phase 409 from failing health checks ---
        pipeline_a = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_get_store.return_value = (MagicMock(repo_path=Path("/tmp/repo")), pipeline_a)

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="check-1",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="broken",
                action=HealthAction.FAIL_PIPELINE,
            ),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp_a = client.post(
            "/api/v1/pipelines/issue-42/phase",
            json={"target_phase": "implement"},
        )

        # --- Scenario B: complete_phase 409 from unresolved HITL decisions ---
        pipeline_b = _make_pipeline(phase=PipelinePhase.REFINE)
        pipeline_b.decisions.append(
            HITLDecision(
                id="decision-1",
                question="q",
                phase=PipelinePhase.REFINE,
                status=DecisionStatus.PENDING,
            )
        )
        mock_get_store.return_value = (MagicMock(repo_path=Path("/tmp/repo")), pipeline_b)

        resp_b = client.post("/api/v1/pipelines/issue-42/phase/complete")

        # Both 409, but distinguishable.
        assert resp_a.status_code == 409
        assert resp_b.status_code == 409
        assert _body(resp_a)["reason"] == "health_checks_failed"
        assert _body(resp_b)["reason"] == "unresolved_hitl_decisions"
        assert _body(resp_a)["reason"] != _body(resp_b)["reason"]
