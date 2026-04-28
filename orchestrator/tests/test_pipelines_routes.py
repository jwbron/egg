"""Regression tests for routes/pipelines.py helpers.

Covers issue #1783: the BRC/consensus timeout path used bare relative
imports that crashed under k3s's top-level-module layout, and silently
swallowed add_decision failures so the stall had no visible HITL decision.
"""

from unittest.mock import MagicMock, patch

import pytest
from events import EventType
from models import Pipeline, PipelinePhase
from routes.pipelines import _handle_brc_consensus_timeout


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
