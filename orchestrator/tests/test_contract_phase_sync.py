"""Tests for #3521: orchestrator-driven phase transitions must advance
``contract.current_phase`` in lockstep with ``pipeline.current_phase``.

Before this fix the contract mutation was owned by whichever agent happened
to call the gateway phase API after a transition; when none did (agents are
one-shot pods), the contract silently stayed on the previous phase and the
gateway commit gate (which keys off the CONTRACT phase) rejected the next
phase's producers ("Phase 'refine' cannot modify"), wedging consensus.

Covers:
- the ``_sync_contract_phase_to_pipeline`` helper (forward-only advance,
  audit entry, best-effort failure modes),
- the ``advance_phase`` route wiring,
- structural assertions that the ``_run_pipeline`` auto-advance block and
  the ``start_pipeline`` HITL-recovery advance call the helper.
"""

import inspect
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing models
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from egg_contracts.loader import create_contract, load_contract
from egg_contracts.models import AuditAction
from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

try:
    from flask import Flask
    from routes.phases import phases_bp
    from routes.pipelines import _sync_contract_phase_to_pipeline

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


def _make_pipeline(
    pipeline_id="issue-3521",
    phase=PipelinePhase.PLAN,
    issue_number=3521,
):
    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch=f"egg/issue-{issue_number}",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestSyncContractPhaseToPipeline:
    """Unit tests for the forward-only contract phase sync helper."""

    def test_advances_contract_and_appends_audit(self, tmp_path):
        """refine→plan (the #3521 incident shape) advances the contract with an audit entry."""
        create_contract(
            issue_number=3521,
            title="test",
            pipeline_id="issue-3521",
            repo_root=tmp_path,
            initial_phase=PipelinePhase.REFINE,
        )
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        advanced = _sync_contract_phase_to_pipeline(pipeline, tmp_path, source="auto_advance")

        assert advanced is True
        contract = load_contract("issue-3521", tmp_path)
        assert contract.current_phase == PipelinePhase.PLAN
        entry = contract.audit_log[-1]
        assert entry.action == AuditAction.TRANSITION
        assert entry.actor == "orchestrator"
        assert entry.old_value == "refine"
        assert entry.new_value == "plan"
        assert "auto_advance" in (entry.reason or "")
        assert "#3521" in (entry.reason or "")

    def test_noop_when_already_in_sync(self, tmp_path):
        """No write and no audit entry when the contract already matches."""
        create_contract(
            issue_number=3521,
            title="test",
            pipeline_id="issue-3521",
            repo_root=tmp_path,
            initial_phase=PipelinePhase.PLAN,
        )
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        advanced = _sync_contract_phase_to_pipeline(pipeline, tmp_path, source="advance_phase")

        assert advanced is False
        contract = load_contract("issue-3521", tmp_path)
        assert contract.current_phase == PipelinePhase.PLAN
        assert not any(e.action == AuditAction.TRANSITION for e in contract.audit_log)

    def test_never_demotes_contract(self, tmp_path):
        """A stale caller (contract ahead of the pipeline record) never rolls the contract back."""
        create_contract(
            issue_number=3521,
            title="test",
            pipeline_id="issue-3521",
            repo_root=tmp_path,
            initial_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        advanced = _sync_contract_phase_to_pipeline(pipeline, tmp_path, source="advance_phase")

        assert advanced is False
        contract = load_contract("issue-3521", tmp_path)
        assert contract.current_phase == PipelinePhase.IMPLEMENT

    def test_epic_apply_transition_advances(self, tmp_path):
        """plan→apply (epic pipelines, #1557) is a forward transition."""
        create_contract(
            issue_number=3521,
            title="test",
            pipeline_id="issue-3521",
            repo_root=tmp_path,
            initial_phase=PipelinePhase.PLAN,
        )
        pipeline = _make_pipeline(phase=PipelinePhase.APPLY)

        advanced = _sync_contract_phase_to_pipeline(pipeline, tmp_path, source="auto_advance")

        assert advanced is True
        contract = load_contract("issue-3521", tmp_path)
        assert contract.current_phase == PipelinePhase.APPLY

    def test_missing_contract_returns_false(self, tmp_path):
        """No contract on disk is a logged no-op, never a raise."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        advanced = _sync_contract_phase_to_pipeline(pipeline, tmp_path, source="auto_advance")

        assert advanced is False

    def test_none_phase_returns_false(self, tmp_path):
        """A pipeline without a current phase is a no-op."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        pipeline.current_phase = None

        advanced = _sync_contract_phase_to_pipeline(pipeline, tmp_path, source="auto_advance")

        assert advanced is False


@pytest.fixture
def app():
    if not _HAS_FLASK:
        pytest.skip("Flask not available")
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestAdvancePhaseSyncsContract:
    """advance_phase must invoke the contract phase sync after the mutation."""

    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines._sync_contract_phase_to_pipeline")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_refine_to_plan_calls_sync(
        self,
        mock_get_store,
        mock_get_lock,
        mock_resolve_wt,
        mock_sync,
        mock_thread,
        mock_persist,
        client,
    ):
        """A refine→plan advance (the #3521 incident transition) routes through the sync helper."""
        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        phase_exec.status = PipelineStatus.COMPLETE
        phase_exec.completed_at = datetime.now(UTC)

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_resolve_wt.return_value = Path("/tmp/wt")

        resp = client.post(
            "/api/v1/pipelines/issue-3521/phase",
            json={"target_phase": "plan"},
        )

        assert resp.status_code == 200
        mock_sync.assert_called_once()
        args, kwargs = mock_sync.call_args
        assert args[0] is pipeline
        assert args[0].current_phase == PipelinePhase.PLAN
        assert args[1] == Path("/tmp/wt")
        assert kwargs.get("source") == "advance_phase"


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestTransitionSitesCallSync:
    """Structural assertions that the other orchestrator-driven transition
    sites call the sync helper (mirrors TestAutoAdvanceRespawnsThread's
    source-level technique; full behavioral coverage of ``_run_pipeline``
    would need a heavy mock harness for the entire phase loop).
    """

    def test_auto_advance_block_calls_sync(self):
        from routes import pipelines

        source = inspect.getsource(pipelines._run_pipeline).replace("_pkg.", "")
        marker = "TEST_MARKER: auto_advance_block"
        assert marker in source
        block = source[source.index(marker) : source.index(marker) + 6000]
        assert "_sync_contract_phase_to_pipeline(" in block, (
            "The auto-advance block must sync contract.current_phase to the "
            "pipeline record (#3521)."
        )

    def test_hitl_recovery_advance_calls_sync(self):
        from routes.pipelines import _routes_lifecycle

        source = inspect.getsource(_routes_lifecycle._start_pipeline_body)
        assert "_sync_contract_phase_to_pipeline(" in source, (
            "The start_pipeline HITL-recovery advance must sync "
            "contract.current_phase to the pipeline record (#3521)."
        )
