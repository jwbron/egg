"""Regression tests for routes/pipelines.py helpers.

Covers issue #1783: the BRC/consensus timeout path used bare relative
imports that crashed under k3s's top-level-module layout, and silently
swallowed add_decision failures so the stall had no visible HITL decision.

Also covers issue #2243: the BRC progress gate must defer the auto
consensus-failure HITL decision while the bus or container heartbeats
have fired within the gate window.
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from events import EventType
from models import Pipeline, PipelinePhase
from routes.pipelines import _check_brc_progress_gate, _handle_brc_consensus_timeout


@pytest.fixture
def pipeline():
    p = Pipeline(id="issue-1783-test", issue_number=1783, repo="owner/repo")
    p.current_phase = PipelinePhase.REFINE
    return p


def _make_store(pipeline):
    """MagicMock store whose load/save round-trip the in-memory pipeline.

    `_persist_hitl_decision` does load → mutate → save. Returning the
    same pipeline from `load_pipeline` keeps the test assertions on
    `pipeline.decisions` working without inventing a separate disk-side
    Pipeline (issue #2208 review asked HITL writes go through the store).
    """
    store = MagicMock()
    store.load_pipeline.side_effect = lambda _pid: pipeline
    store.save_pipeline.side_effect = lambda _p: None
    return store


class TestHandleBrcConsensusTimeout:
    """The timeout handler must reach add_decision under k3s's module layout.

    The test runner imports routes.pipelines via absolute imports (the same
    layout k3s uses), so a regression that re-introduces a bare relative
    import would surface here as an ImportError before add_decision runs.
    """

    @patch("routes.pipelines._emit_event")
    def test_no_brc_tracker_queues_hitl_decision(self, mock_emit, pipeline):
        store = _make_store(pipeline)
        with patch("peer_consensus.get_peer_consensus_tracker", return_value=None):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["reviewer_code"],
                store=store,
            )

        assert len(pipeline.decisions) == 1
        assert "Consensus not reached after 30 minutes" in pipeline.decisions[0].question
        mock_emit.assert_called_once_with(
            EventType.CONSENSUS_TIMEOUT,
            pipeline.id,
            data={
                "timeout_minutes": 30.0,
                "blocking_agents": ["reviewer_code"],
            },
        )

    @patch("routes.pipelines._emit_event")
    def test_brc_escalate_queues_hitl_decision(self, mock_emit, pipeline):
        store = _make_store(pipeline)
        tracker = MagicMock()
        tracker.handle_timeout.return_value = {"action": "escalate"}
        tracker.is_timeout_handled.return_value = True

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=[],
                store=store,
            )

        assert len(pipeline.decisions) == 1
        assert "BRC consensus failure" in pipeline.decisions[0].question
        mock_emit.assert_not_called()

    @patch("routes.pipelines._emit_event")
    def test_brc_handled_without_escalate_no_decision(self, mock_emit, pipeline):
        store = _make_store(pipeline)
        tracker = MagicMock()
        tracker.handle_timeout.return_value = {"action": "proceed"}
        tracker.is_timeout_handled.return_value = True

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=[],
                store=store,
            )

        assert len(pipeline.decisions) == 0
        mock_emit.assert_not_called()

    @patch("routes.pipelines.logger")
    @patch("routes.pipelines._emit_event")
    def test_tracker_import_error_falls_back_to_hitl(self, mock_emit, mock_logger, pipeline):
        # Regression test for issue #1783: bare relative import raised
        # ImportError under k3s, and the outer except caught it but the
        # HITL decision must still be created via the fallback path.
        store = _make_store(pipeline)
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            side_effect=ImportError("simulated k3s import failure"),
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["reviewer_code"],
                store=store,
            )

        # The warning log records the import failure
        mock_logger.warning.assert_called_once()
        _, warn_kwargs = mock_logger.warning.call_args
        assert "simulated k3s import failure" in warn_kwargs.get("error", "")

        # Fallback HITL decision is still queued
        assert len(pipeline.decisions) == 1
        assert "Consensus not reached after 30 minutes" in pipeline.decisions[0].question
        mock_emit.assert_called_once_with(
            EventType.CONSENSUS_TIMEOUT,
            pipeline.id,
            data={
                "timeout_minutes": 30.0,
                "blocking_agents": ["reviewer_code"],
            },
        )

    @patch("routes.pipelines.logger")
    @patch("routes.pipelines._emit_event")
    def test_add_decision_failure_is_logged_not_swallowed(self, mock_emit, mock_logger, pipeline):
        # Covers the issue #1783 second-order bug: the except Exception: pass
        # at the old decision-queue call sites hid stall-causing failures.
        # Post-#2208 review: persistence routes through `_persist_hitl_decision`,
        # which catches and logs at WARNING with exc_info so the original
        # traceback survives — still not swallowed.
        store = _make_store(pipeline)
        with (
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            patch.object(Pipeline, "add_decision", side_effect=RuntimeError("boom")),
        ):
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=[],
                store=store,
            )

        # `_persist_hitl_decision` logs the failure with exc_info so the
        # traceback is preserved rather than swallowed.
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if call.args and "Failed to persist HITL decision" in call.args[0]
        ]
        assert warning_calls, "add_decision failure must be logged with exc_info"
        _, kwargs = warning_calls[0]
        assert kwargs.get("pipeline_id") == pipeline.id
        assert kwargs.get("exc_info") is True


class TestBrcProgressGate:
    """Issue #2243 — defer the auto consensus-failure HITL decision while
    BRC bus or container heartbeats have advanced within the gate window.

    The gate is the operator-friendly half of the fix: previously, at
    ``consensus_timeout_minutes`` the orchestrator opened a `choice`
    decision unconditionally, even when producers were minutes away
    from their first commit (decision-15 / decision-17 on
    ``issue-1557-v2``).  The gate keeps the polling loop polling while
    signals are alive, and only opens the decision once the bus and
    containers have both gone quiet for ``gate_seconds``.
    """

    PIPELINE_ID = "issue-2243-test"

    def _patch_tracker(self, latest_progress: datetime | None):
        tracker = MagicMock()
        tracker.get_latest_progress_timestamp.return_value = latest_progress
        return patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker)

    def _patch_health_monitor(self, last_heartbeats: dict[str, float] | None):
        if last_heartbeats is None:
            return patch("health_monitor.get_health_monitor", return_value=None)
        hm = MagicMock()
        hm._lock = MagicMock()
        hm._lock.__enter__ = MagicMock(return_value=hm._lock)
        hm._lock.__exit__ = MagicMock(return_value=False)
        hm._last_heartbeat = dict(last_heartbeats)
        return patch("health_monitor.get_health_monitor", return_value=hm)

    def test_disabled_gate_returns_no_defer(self):
        defer, reason = _check_brc_progress_gate(self.PIPELINE_ID, None, ["coder"], 0)
        assert defer is False
        assert reason is None

    def test_recent_proposal_defers(self):
        recent = datetime.now(UTC) - timedelta(seconds=30)
        with (
            self._patch_tracker(recent),
            self._patch_health_monitor(None),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is True
        assert reason is not None and "BRC bus" in reason

    def test_stale_proposal_does_not_defer(self):
        stale = datetime.now(UTC) - timedelta(seconds=600)
        with (
            self._patch_tracker(stale),
            self._patch_health_monitor(None),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_recent_heartbeat_defers_when_bus_silent(self):
        # Bus completely silent (decision-17 shape: coder mid-merge-conflict
        # before its first CONSENSUS_PROPOSE), but the container is still
        # emitting heartbeats.  The gate should still defer.
        recent_hb = time.time() - 30
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"coder": recent_hb}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder", "tester"], gate_seconds=300
            )
        assert defer is True
        assert reason is not None and "heartbeat" in reason

    def test_stale_heartbeat_does_not_defer(self):
        stale_hb = time.time() - 600
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"coder": stale_hb}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_heartbeat_for_inactive_role_is_ignored(self):
        # Cross-phase pollution: the singleton HealthMonitor's
        # ``_last_heartbeat`` may carry a stale entry for a role that
        # isn't part of the current phase.  The gate must filter so a
        # ghost heartbeat from a finished phase doesn't keep the gate
        # deferring forever.
        recent_hb = time.time() - 30
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"refiner": recent_hb}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder", "tester"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_empty_active_roles_does_not_defer_on_heartbeat(self):
        # Contract: an empty ``active_role_names`` means the caller has
        # no live containers to gate on, so match nothing rather than
        # accept every stale heartbeat in the singleton HealthMonitor.
        # ``_run_concurrent_phase`` exits before reaching the gate when
        # there are no live containers, but the contract is explicit so
        # a future caller can't accidentally widen the gate.
        recent_hb = time.time() - 30
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({"coder": recent_hb, "refiner": recent_hb}),
        ):
            defer, reason = _check_brc_progress_gate(self.PIPELINE_ID, None, [], gate_seconds=300)
        assert defer is False
        assert reason is None

    def test_no_signals_returns_no_defer(self):
        with (
            self._patch_tracker(None),
            self._patch_health_monitor({}),
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None

    def test_tracker_failure_logged_and_not_treated_as_defer(self):
        # If the tracker collector raises, treat it as "no signal" rather
        # than as a defer — a crashed signal source must never silently
        # keep us off the HITL surface.
        with (
            patch(
                "peer_consensus.get_peer_consensus_tracker",
                side_effect=RuntimeError("simulated tracker failure"),
            ),
            self._patch_health_monitor({}),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None
        assert any(
            "tracker check failed" in (call.args[0] if call.args else "")
            for call in mock_logger.warning.call_args_list
        )

    def test_heartbeat_failure_logged_and_not_treated_as_defer(self):
        # Same as above for the heartbeat collector.
        bad_hm = MagicMock()
        bad_hm._lock = MagicMock()
        bad_hm._lock.__enter__ = MagicMock(side_effect=RuntimeError("hm boom"))
        bad_hm._lock.__exit__ = MagicMock(return_value=False)
        with (
            self._patch_tracker(None),
            patch("health_monitor.get_health_monitor", return_value=bad_hm),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            defer, reason = _check_brc_progress_gate(
                self.PIPELINE_ID, None, ["coder"], gate_seconds=300
            )
        assert defer is False
        assert reason is None
        assert any(
            "heartbeat check failed" in (call.args[0] if call.args else "")
            for call in mock_logger.warning.call_args_list
        )
