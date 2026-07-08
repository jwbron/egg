"""Regression for #3547 pain point 3; consensus poll-tick log spam.

The run loop calls ``check_consensus`` every ~5 seconds per active slice, and
pre-fix each incomplete tick logged "Consensus incomplete — checking fallbacks"
and "Skipping pipeline-wide message-bus fallback ..." at INFO with
identical content. Two lines per tick per slice defined the INFO noise floor
for the whole service: with ``get_service_logs``'s 10,000-line scan budget the
effective window shrank to ~half an hour, and role-name pattern filters
matched the spam itself (roles appear in ``blocking_agents``).

Post-fix the observations log at INFO only when the incomplete state
(confirmed count, blocking set, unresolved-NACK flag) changes, and at DEBUG
otherwise; reaching consensus resets the memo so the next round's first
observation is INFO again.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# sys.path setup; orchestrator + shared.
_orchestrator_path = Path(__file__).parent.parent
_shared_path = _orchestrator_path.parent / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from concurrent_executor import ConcurrentPhaseExecutor  # noqa: E402
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from peer_consensus import (  # noqa: E402
    create_peer_consensus_tracker,
    remove_peer_consensus_tracker,
)
from review_graph import get_review_graph_for_phase  # noqa: E402


def _make_pipeline(pipeline_id: str) -> Pipeline:
    config = PipelineConfig(concurrent_execution=True)
    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=3547,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _messages_logged(mock_logger, level: str) -> list[str]:
    return [call.args[0] for call in getattr(mock_logger, level).call_args_list]


class TestIncompleteConsensusLogDedup:
    def _run_ticks(self, ticks: int) -> MagicMock:
        """Run ``ticks`` unchanged incomplete polls on one slice executor."""
        pipeline_id = "issue-3547-log-dedup"
        pipeline = _make_pipeline(pipeline_id)
        graph = get_review_graph_for_phase("implement", repo="test/repo")
        create_peer_consensus_tracker(pipeline_id, graph, slice_id="slice-1")
        try:
            executor = ConcurrentPhaseExecutor(
                pipeline,
                spawn_fn=MagicMock(),
                review_graph=graph,
                slice_id="slice-1",
            )
            with patch("concurrent_executor.logger") as mock_logger:
                for _ in range(ticks):
                    result = executor.check_consensus()
                    assert result["is_complete"] is False
        finally:
            remove_peer_consensus_tracker(pipeline_id, "slice-1")
        return mock_logger

    def test_first_tick_logs_info(self):
        mock_logger = self._run_ticks(1)
        infos = _messages_logged(mock_logger, "info")
        assert "Consensus incomplete — checking fallbacks" in infos
        assert any("Skipping pipeline-wide message-bus fallback" in m for m in infos)

    def test_unchanged_ticks_drop_to_debug(self):
        mock_logger = self._run_ticks(5)
        infos = _messages_logged(mock_logger, "info")
        debugs = _messages_logged(mock_logger, "debug")
        # One INFO per line for the first observation only...
        assert infos.count("Consensus incomplete — checking fallbacks") == 1
        assert sum("Skipping pipeline-wide" in m for m in infos) == 1
        # ...and the four unchanged repeats land at DEBUG.
        assert debugs.count("Consensus incomplete — checking fallbacks") == 4
        assert sum("Skipping pipeline-wide" in m for m in debugs) == 4

    def test_state_change_logs_info_again(self):
        pipeline_id = "issue-3547-log-dedup-change"
        pipeline = _make_pipeline(pipeline_id)
        graph = get_review_graph_for_phase("implement", repo="test/repo")
        create_peer_consensus_tracker(pipeline_id, graph, slice_id="slice-1")
        try:
            executor = ConcurrentPhaseExecutor(
                pipeline,
                spawn_fn=MagicMock(),
                review_graph=graph,
                slice_id="slice-1",
            )
            with patch("concurrent_executor.logger") as mock_logger:
                executor.check_consensus()
                # Simulate a prior differing observation (e.g. an agent just
                # confirmed): the next tick must log at INFO again.
                executor._last_incomplete_consensus_log = ("different",)
                executor.check_consensus()
        finally:
            remove_peer_consensus_tracker(pipeline_id, "slice-1")
        infos = _messages_logged(mock_logger, "info")
        assert infos.count("Consensus incomplete — checking fallbacks") == 2

    def test_memo_resets_when_consensus_completes(self):
        """A completed round clears the memo so the next round starts at INFO."""
        pipeline_id = "issue-3547-log-dedup-reset"
        pipeline = _make_pipeline(pipeline_id)
        graph = get_review_graph_for_phase("implement", repo="test/repo")
        create_peer_consensus_tracker(pipeline_id, graph, slice_id="slice-1")
        try:
            executor = ConcurrentPhaseExecutor(
                pipeline,
                spawn_fn=MagicMock(),
                review_graph=graph,
                slice_id="slice-1",
            )
            executor._last_incomplete_consensus_log = ("stale",)
            tracker = MagicMock()
            tracker.evaluate.return_value = {"is_complete": True}
            with patch("concurrent_executor.get_peer_consensus_tracker", return_value=tracker):
                result = executor.check_consensus()
            assert result["is_complete"] is True
            assert executor._last_incomplete_consensus_log is None
        finally:
            remove_peer_consensus_tracker(pipeline_id, "slice-1")
