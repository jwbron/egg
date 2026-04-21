"""Tests for the POST /<pipeline_id>/phase/complete endpoint.

Regressions for #1755:
- Empty body must be treated as an empty object, not 400.
- Non-dict artifacts must be rejected at the boundary, not written to
  disk (PhaseExecution.artifacts is typed as dict[str, str] but does
  not validate on assignment, so a poisoned value would break every
  subsequent read).

Regressions for #1788:
- Phase advance must not silently abandon HITL decisions the human has
  not yet answered. The endpoint returns 409 with the blocking decision
  ids unless the caller passes ``force=true``.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import DecisionStatus, HITLDecision, Pipeline, PipelinePhase, PipelineStatus
from routes.phases import phases_bp


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_pipeline(phase=PipelinePhase.IMPLEMENT):
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
    )
    pipeline.current_phase = phase
    return pipeline


class TestCompletePhaseEndpoint:
    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_empty_body_returns_200(self, mock_get_store, _mock_clear, client):
        """POST with Content-Type: application/json and empty body must not 400.

        Regression: Flask's default get_json() raises BadRequest for an
        empty body with a JSON content type, which previously made the
        optional `artifacts` field effectively required.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["phase"] == "implement"
        assert data["data"]["next_phase"] == "pr"

        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert phase_exec.status == PipelineStatus.COMPLETE
        assert phase_exec.artifacts == {}

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_no_body_returns_200(self, mock_get_store, _mock_clear, client):
        """POST with no body at all also succeeds."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_string_artifacts_returns_400(self, mock_get_store, _mock_clear, client):
        """A stringified-JSON artifacts value is rejected without mutating state.

        Previously the string was assigned to PhaseExecution.artifacts
        (typed dict[str, str]) without validation, persisted to disk,
        and then broke every subsequent read with a 500 when pydantic
        re-validated the stored pipeline.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": "{}"},
        )

        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "artifacts" in data["message"]

        # The early return must prevent pipeline state from being loaded.
        mock_get_store.assert_not_called()
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_list_artifacts_returns_400(self, mock_get_store, _mock_clear, client):
        """Non-dict artifacts of any kind are rejected."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": ["a", "b"]},
        )

        assert resp.status_code == 400
        mock_get_store.assert_not_called()
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_non_string_values_artifacts_returns_400(self, mock_get_store, _mock_clear, client):
        """A dict with non-string values is rejected at the boundary.

        PhaseExecution.artifacts is typed dict[str, str], so values like
        lists or nested dicts would persist without pydantic catching them
        and then break on the next read.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": {"key": ["not", "a", "string"]}},
        )

        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "string values" in data["message"]
        mock_get_store.assert_not_called()
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_dict_artifacts_stored(self, mock_get_store, _mock_clear, client):
        """Valid dict artifacts are stored on the phase execution."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": {"commit_sha": "abc123"}},
        )

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert phase_exec.artifacts == {"commit_sha": "abc123"}


def _make_pipeline_without_contract(phase=PipelinePhase.REFINE):
    """Pipeline with has_contract=False so tests don't need the contract file."""
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
        has_contract=False,
    )
    pipeline.current_phase = phase
    return pipeline


class TestCompletePhasePendingDecisions:
    """#1788 — `/phase/complete` must not advance while HITL decisions are pending."""

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pending_phase_scoped_decision_returns_409(self, mock_get_store, mock_clear, client):
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        pipeline.decisions.extend(
            [
                HITLDecision(
                    id="decision-1",
                    question="q1",
                    phase=PipelinePhase.REFINE,
                    status=DecisionStatus.PENDING,
                ),
                HITLDecision(
                    id="decision-2",
                    question="q2",
                    phase=PipelinePhase.REFINE,
                    status=DecisionStatus.PENDING,
                ),
            ]
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 409
        body = json.loads(resp.data)
        assert body["success"] is False
        assert body["details"]["phase"] == "refine"
        assert body["details"]["unresolved_decision_ids"] == ["decision-1", "decision-2"]
        # Phase state must not have been mutated or persisted.
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status != PipelineStatus.COMPLETE
        mock_store.save_pipeline.assert_not_called()
        mock_clear.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_resolved_decisions_do_not_block(self, mock_get_store, _mock_clear, client):
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        pipeline.decisions.append(
            HITLDecision(
                id="decision-1",
                question="q1",
                phase=PipelinePhase.REFINE,
                status=DecisionStatus.RESOLVED,
                resolution="yes",
            )
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pending_decision_on_other_phase_does_not_block(
        self, mock_get_store, _mock_clear, client
    ):
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        pipeline.decisions.append(
            HITLDecision(
                id="decision-1",
                question="q1",
                phase=PipelinePhase.PLAN,
                status=DecisionStatus.PENDING,
            )
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pending_decision_with_null_phase_does_not_block(
        self, mock_get_store, _mock_clear, client
    ):
        """Legacy decisions without a phase tag are not load-bearing.

        Persisted state from before the phase guard landed may contain
        HITLDecisions with phase=None. The guard skips them rather than
        refusing to advance on ambiguous data.
        """
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        pipeline.decisions.append(
            HITLDecision(
                id="decision-1",
                question="q1",
                phase=None,
                status=DecisionStatus.PENDING,
            )
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_force_overrides_and_audits(self, mock_get_store, mock_clear, client):
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        pipeline.decisions.extend(
            [
                HITLDecision(
                    id="decision-1",
                    question="q1",
                    phase=PipelinePhase.REFINE,
                    status=DecisionStatus.PENDING,
                ),
                HITLDecision(
                    id="decision-3",
                    question="q3",
                    phase=PipelinePhase.REFINE,
                    status=DecisionStatus.PENDING,
                ),
            ]
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"force": True, "force_reason": "abandoning refine after rebuild"},
        )

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.COMPLETE
        assert phase_exec.artifacts["force_completed_decisions"] == "decision-1,decision-3"
        assert phase_exec.artifacts["force_reason"] == "abandoning refine after rebuild"
        mock_store.save_pipeline.assert_called_once()
        mock_clear.assert_called_once()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_force_without_pending_does_nothing_extra(self, mock_get_store, _mock_clear, client):
        """force=true is a no-op when no decisions would have blocked — no
        force_* artifacts are written."""
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"force": True},
        )

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert "force_completed_decisions" not in phase_exec.artifacts

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_force_preserves_caller_supplied_artifacts(self, mock_get_store, _mock_clear, client):
        pipeline = _make_pipeline_without_contract(phase=PipelinePhase.REFINE)
        pipeline.decisions.append(
            HITLDecision(
                id="decision-1",
                question="q1",
                phase=PipelinePhase.REFINE,
                status=DecisionStatus.PENDING,
            )
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={
                "force": True,
                "artifacts": {"commit_sha": "abc123"},
            },
        )

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.artifacts["commit_sha"] == "abc123"
        assert phase_exec.artifacts["force_completed_decisions"] == "decision-1"

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_contract_decision_blocks_advance(self, mock_get_store, _mock_clear, client):
        """A contract-side HITL decision on the current phase is load-bearing.

        #1788: the reproducer had 15 refine-phase decisions written via
        egg-contract add-decision. Those live in the contract, not the
        pipeline's decision queue, and must still block advance.
        """
        from egg_contracts.models import Contract, Decision, DecisionType, IssueInfo
        from egg_contracts.models import PipelinePhase as ContractPipelinePhase

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
        )
        pipeline.current_phase = PipelinePhase.REFINE
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        fake_contract = Contract(
            issue=IssueInfo(
                number=42,
                title="t",
                description="d",
                url="https://example.com/issues/42",
            ),
            decisions=[
                Decision(
                    id="decision-5",
                    question="contract-q",
                    type=DecisionType.HITL,
                    phase=ContractPipelinePhase.REFINE,
                    resolved=False,
                ),
            ],
        )

        with (
            patch("routes.pipelines._pipeline_identifier", return_value=42),
            patch("routes.resolve_worktree_path", side_effect=lambda _pid, rp: rp),
            patch("egg_contracts.loader.load_contract", return_value=fake_contract),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 409
        body = json.loads(resp.data)
        assert body["details"]["unresolved_decision_ids"] == ["decision-5"]
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_resolved_contract_decision_does_not_block(self, mock_get_store, _mock_clear, client):
        from egg_contracts.models import Contract, Decision, DecisionType, IssueInfo
        from egg_contracts.models import PipelinePhase as ContractPipelinePhase

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
        )
        pipeline.current_phase = PipelinePhase.REFINE
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        fake_contract = Contract(
            issue=IssueInfo(
                number=42,
                title="t",
                description="d",
                url="https://example.com/issues/42",
            ),
            decisions=[
                Decision(
                    id="decision-5",
                    question="contract-q",
                    type=DecisionType.HITL,
                    phase=ContractPipelinePhase.REFINE,
                    resolved=True,
                    resolution="approved",
                ),
            ],
        )

        with (
            patch("routes.pipelines._pipeline_identifier", return_value=42),
            patch("routes.resolve_worktree_path", side_effect=lambda _pid, rp: rp),
            patch("egg_contracts.loader.load_contract", return_value=fake_contract),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_missing_contract_file_does_not_block(self, mock_get_store, _mock_clear, client):
        """An issue pipeline with has_contract=True but no contract file on
        disk yet (e.g. very early in the refine phase) must still be able
        to complete — we cannot block on data we cannot see."""
        from pathlib import Path

        from egg_contracts.loader import ContractNotFoundError

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
        )
        pipeline.current_phase = PipelinePhase.REFINE
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        with (
            patch("routes.pipelines._pipeline_identifier", return_value=42),
            patch("routes.resolve_worktree_path", side_effect=lambda _pid, rp: rp),
            patch(
                "egg_contracts.loader.load_contract",
                side_effect=ContractNotFoundError(42, Path("/nonexistent")),
            ),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200
