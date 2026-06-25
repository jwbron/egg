"""Regression tests for restart-orphaned AWAITING_HUMAN driver revival (#3233).

When the orchestrator restarts while a pipeline is parked AWAITING_HUMAN at a
phase gate, the in-memory ``_run_pipeline`` driver that polls
``wait_for_decision`` is gone.  Startup reconciliation deliberately leaves a
still-pending decision as-is, so resolving it afterwards records the
resolution with no consumer and the pipeline hangs silently.  The
decision-resolve path now revives the driver via ``start_pipeline``'s proven
AWAITING_HUMAN recovery once the queue drains, so the resolution self-heals.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from models import Pipeline, PipelineStatus  # noqa: E402


def _pipeline(pipeline_id="issue-3200", status=PipelineStatus.AWAITING_HUMAN, decisions=None):
    p = Pipeline(id=pipeline_id, issue_number=3200, repo="owner/repo", branch="egg/test")
    p.status = status
    if decisions:
        p.decisions = decisions
    return p


class TestHasLivePipelineDriver:
    """Thread-name detection underpinning the orphaned-driver check."""

    def test_detects_exact_and_suffixed_names(self):
        from routes.pipelines import has_live_pipeline_driver

        pid = "issue-9999-driver-test"
        for name in (f"pipeline-{pid}", f"pipeline-{pid}-1700000000"):
            stop = threading.Event()
            t = threading.Thread(target=stop.wait, name=name, daemon=True)
            t.start()
            try:
                assert has_live_pipeline_driver(pid) is True
            finally:
                stop.set()
                t.join(timeout=2)

    def test_no_thread_returns_false(self):
        from routes.pipelines import has_live_pipeline_driver

        # No driver thread exists for this id — the post-restart condition.
        assert has_live_pipeline_driver("issue-no-such-driver-3233") is False

    def test_prefix_collision_is_safe(self):
        """A driver for ``issue-32`` must not satisfy the check for ``issue-3``."""
        from routes.pipelines import has_live_pipeline_driver

        stop = threading.Event()
        t = threading.Thread(target=stop.wait, name="pipeline-issue-32", daemon=True)
        t.start()
        try:
            assert has_live_pipeline_driver("issue-32") is True
            assert has_live_pipeline_driver("issue-3") is False
        finally:
            stop.set()
            t.join(timeout=2)


class TestMaybeReviveOrphanedDriver:
    """``maybe_revive_orphaned_awaiting_human_driver`` decision logic."""

    @patch("routes.pipelines.start_pipeline")
    @patch("routes.pipelines._broadcast_orphaned_driver_alert")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    @patch("routes.pipelines.get_state_store")
    def test_revives_when_orphaned_and_resolved(
        self, mock_get_store, _mock_has_driver, _mock_alert, mock_start, tmp_path
    ):
        from routes.pipelines import maybe_revive_orphaned_awaiting_human_driver

        store = MagicMock()
        store.load_pipeline.return_value = _pipeline()  # AWAITING_HUMAN, 0 pending
        mock_get_store.return_value = store
        mock_start.return_value = (MagicMock(), 200)

        assert maybe_revive_orphaned_awaiting_human_driver("issue-3200", tmp_path) is True
        mock_start.assert_called_once_with("issue-3200")

    @patch("routes.pipelines.start_pipeline")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=True)
    @patch("routes.pipelines.get_state_store")
    def test_skips_when_live_driver_present(
        self, mock_get_store, _mock_has_driver, mock_start, tmp_path
    ):
        from routes.pipelines import maybe_revive_orphaned_awaiting_human_driver

        store = MagicMock()
        store.load_pipeline.return_value = _pipeline()
        mock_get_store.return_value = store

        assert maybe_revive_orphaned_awaiting_human_driver("issue-3200", tmp_path) is False
        mock_start.assert_not_called()

    @patch("routes.pipelines.start_pipeline")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    @patch("routes.pipelines.get_state_store")
    def test_skips_when_decisions_still_pending(
        self, mock_get_store, _mock_has_driver, mock_start, tmp_path
    ):
        from models import DecisionStatus, HITLDecision
        from routes.pipelines import maybe_revive_orphaned_awaiting_human_driver

        pending = HITLDecision(id="decision-4", question="q?", status=DecisionStatus.PENDING)
        store = MagicMock()
        store.load_pipeline.return_value = _pipeline(decisions=[pending])
        mock_get_store.return_value = store

        assert maybe_revive_orphaned_awaiting_human_driver("issue-3200", tmp_path) is False
        mock_start.assert_not_called()

    @patch("routes.pipelines.start_pipeline")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    @patch("routes.pipelines.get_state_store")
    def test_skips_when_not_awaiting_human(
        self, mock_get_store, _mock_has_driver, mock_start, tmp_path
    ):
        from routes.pipelines import maybe_revive_orphaned_awaiting_human_driver

        store = MagicMock()
        store.load_pipeline.return_value = _pipeline(status=PipelineStatus.RUNNING)
        mock_get_store.return_value = store

        assert maybe_revive_orphaned_awaiting_human_driver("issue-3200", tmp_path) is False
        mock_start.assert_not_called()

    @patch("routes.pipelines.start_pipeline")
    @patch("routes.pipelines._broadcast_orphaned_driver_alert")
    @patch("routes.pipelines.has_live_pipeline_driver", return_value=False)
    @patch("routes.pipelines.get_state_store")
    def test_returns_false_when_start_pipeline_errors(
        self, mock_get_store, _mock_has_driver, _mock_alert, mock_start, tmp_path
    ):
        from routes.pipelines import maybe_revive_orphaned_awaiting_human_driver

        store = MagicMock()
        store.load_pipeline.return_value = _pipeline()
        mock_get_store.return_value = store
        mock_start.return_value = (MagicMock(), 409)

        assert maybe_revive_orphaned_awaiting_human_driver("issue-3200", tmp_path) is False


class TestResolveRouteTriggersRevival:
    """The resolve-decision route wires the revival into the queue path."""

    @pytest.fixture
    def client(self):
        from flask import Flask
        from routes.decisions import decisions_bp

        app = Flask(__name__)
        app.register_blueprint(decisions_bp)
        app.config["TESTING"] = True
        return app.test_client()

    @patch("routes.pipelines.maybe_revive_orphaned_awaiting_human_driver")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_invokes_revival(
        self, mock_get_queue, mock_get_store, mock_revive, client, tmp_path
    ):
        from models import DecisionStatus, HITLDecision

        mock_get_store.return_value = (MagicMock(repo_path=tmp_path), MagicMock())
        queue = MagicMock()
        queue.resolve_decision.return_value = HITLDecision(
            id="decision-3",
            question="q?",
            decision_type="phase_gate",
            status=DecisionStatus.RESOLVED,
            resolution='{"action": "request_changes", "feedback": "redo"}',
        )
        mock_get_queue.return_value = queue

        resp = client.post(
            "/api/v1/pipelines/issue-3200/decisions/decision-3/resolve",
            json={"resolution": '{"action": "request_changes", "feedback": "redo"}'},
        )

        assert resp.status_code == 200
        mock_revive.assert_called_once()
        assert mock_revive.call_args[0][0] == "issue-3200"

    @patch("routes.pipelines.maybe_revive_orphaned_awaiting_human_driver")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_skips_revival_for_non_phase_gate_decision(
        self, mock_get_queue, mock_get_store, mock_revive, client, tmp_path
    ):
        """A resolved non-phase-gate decision (e.g. the worktree-reconcile
        ``choice`` HITL, #2979) must NOT trigger the AWAITING_HUMAN revival.

        ``start_pipeline``'s recovery assumes a phase-gate park and would
        spuriously force-advance the phase; only a phase_gate resolution is
        meant to drive the phase forward (#3233 review fix).
        """
        from models import DecisionStatus, HITLDecision

        mock_get_store.return_value = (MagicMock(repo_path=tmp_path), MagicMock())
        queue = MagicMock()
        queue.resolve_decision.return_value = HITLDecision(
            id="decision-9",
            question="Reconcile the worktree, then re-run populate_contract",
            decision_type="choice",
            status=DecisionStatus.RESOLVED,
            resolution="Acknowledged",
        )
        mock_get_queue.return_value = queue

        resp = client.post(
            "/api/v1/pipelines/issue-3200/decisions/decision-9/resolve",
            json={"resolution": "Acknowledged"},
        )

        assert resp.status_code == 200
        mock_revive.assert_not_called()


class TestBroadcastOrphanedDriverAlert:
    """``_broadcast_orphaned_driver_alert`` constructs and sends a real
    OVERSEER_ALERT (#3233 review: close the untested-broadcast gap)."""

    @patch("routes.pipelines._get_message_store")
    def test_emits_overseer_alert_with_expected_fields(self, mock_get_store_fn):
        from message_store import MessageType
        from routes.pipelines import _broadcast_orphaned_driver_alert

        msg_store = MagicMock()
        mock_get_store_fn.return_value = lambda: msg_store

        pipeline = _pipeline()
        _broadcast_orphaned_driver_alert("issue-3200", pipeline)

        msg_store.add_message.assert_called_once()
        sent = msg_store.add_message.call_args[0][0]
        assert sent.pipeline_id == "issue-3200"
        assert sent.from_role == "orchestrator"
        assert sent.to_role == "all"
        assert sent.message_type == MessageType.OVERSEER_ALERT
        assert "[medium]" in sent.subject
        assert sent.metadata == {"reason": "restart_orphaned_awaiting_human"}

    @patch("routes.pipelines._get_message_store", return_value=None)
    def test_no_message_store_is_a_noop(self, _mock_get_store_fn):
        from routes.pipelines import _broadcast_orphaned_driver_alert

        # Must not raise when the message store factory is unavailable.
        _broadcast_orphaned_driver_alert("issue-3200", _pipeline())
