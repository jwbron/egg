"""Regression for #2535 Bug B — slice-2 false consensus at elapsed=0s.

When a fresh slice-N tracker exists but is empty (the steady-state
right after the slice-N spawn, before any agent has proposed), the
``check_consensus`` fallbacks must NOT scan the pipeline-wide message
bus. Doing so picks up CONSENSUS_CONFIRMED messages from a prior slice
whose roster matches slice-N's (coder/tester/documenter + reviewers
re-spawn per slice), aggregates them into ``confirmed_roles`` without
slice scope, and falsely declares ``is_complete=True`` at the very
first poll iteration.

The fix gates both the reconstruction fallback and the message-bus
fallback inside :meth:`ConcurrentPhaseExecutor.check_consensus` on
``self._slice_id is None``. The in-memory per-slice tracker is the
authoritative source for slice work; an empty fresh tracker correctly
returns ``is_complete=False`` and the polling loop keeps going.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# sys.path setup — orchestrator + shared.
_orchestrator_path = Path(__file__).parent.parent
_shared_path = _orchestrator_path.parent / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from concurrent_executor import ConcurrentPhaseExecutor  # noqa: E402
from message_store import Message, MessageType  # noqa: E402
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


def _make_pipeline(pipeline_id: str = "issue-2535-test") -> Pipeline:
    config = PipelineConfig()
    try:
        config.concurrent_execution = True  # type: ignore[attr-defined]
    except AttributeError, ValueError:
        config.__dict__["concurrent_execution"] = True
    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=2535,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _slice_one_confirmed_messages(pipeline_id: str) -> list[Message]:
    """Mimic the message-bus state after slice-1 reaches consensus.

    All implement-phase roles emit CONSENSUS_CONFIRMED. The persisted
    Message records key by bare ``pipeline_id`` (the message store does
    not yet carry ``slice_id``), which is the contamination surface the
    fix has to cordon off.
    """
    graph = get_review_graph_for_phase("implement", repo="test/repo")
    msgs: list[Message] = []
    for role in graph.all_roles():
        msgs.append(
            Message(
                pipeline_id=pipeline_id,
                from_role=role,
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject=f"Confirmed by {role}",
                body="",
                phase="implement",
                metadata={"consensus_reached": True},
            )
        )
    return msgs


class TestSliceTwoFreshTrackerDoesNotInheritSliceOneConsensus:
    """A freshly-spawned slice-2 must not borrow slice-1's CONFIRMs."""

    def test_check_consensus_returns_incomplete_for_empty_slice_tracker(self) -> None:
        pipeline_id = "issue-2535-fresh-slice"
        pipeline = _make_pipeline(pipeline_id)
        graph = get_review_graph_for_phase("implement", repo="test/repo")

        # Fresh slice-2 tracker — registered, empty, no proposals yet.
        # Mirrors the state right after ``spawn_all`` registers the
        # tracker but before any agent has reached the message bus.
        create_peer_consensus_tracker(pipeline_id, graph, slice_id="slice-2")

        # Mimic the bug-A state: slice-1's CONFIRMED messages are
        # still in the bus under the bare pipeline_id.
        fake_store = MagicMock()
        fake_store.get_messages.return_value = _slice_one_confirmed_messages(pipeline_id)

        try:
            executor = ConcurrentPhaseExecutor(
                pipeline,
                spawn_fn=MagicMock(),
                review_graph=graph,
                slice_id="slice-2",
            )
            with patch("message_store.get_message_store", return_value=fake_store):
                result = executor.check_consensus()
        finally:
            remove_peer_consensus_tracker(pipeline_id, "slice-2")

        assert result["is_complete"] is False, (
            "slice-2 with empty tracker must NOT inherit slice-1 CONFIRMs "
            "via the pipeline-wide message-bus fallback (#2535)"
        )
        # blocking_agents must still surface the slice-2 roster so the
        # caller can see the work is in flight.
        assert set(result.get("blocking_agents", [])) == graph.all_roles()

    def test_pipeline_scoped_executor_still_uses_message_bus_fallback(self) -> None:
        """The fix must not break the legacy pipeline-scoped behaviour.

        For executors with ``slice_id=None`` the message-bus fallback is
        the long-standing recovery path for tracker loss after restart
        (#1471/#1615). It MUST keep firing when all roles have CONFIRMED
        messages — only slice-scoped executors are gated.
        """
        pipeline_id = "issue-2535-legacy-scope"
        pipeline = _make_pipeline(pipeline_id)
        graph = get_review_graph_for_phase("implement", repo="test/repo")

        # Pipeline-scoped tracker, empty.
        create_peer_consensus_tracker(pipeline_id, graph)

        fake_store = MagicMock()
        fake_store.get_messages.return_value = _slice_one_confirmed_messages(pipeline_id)

        try:
            executor = ConcurrentPhaseExecutor(
                pipeline,
                spawn_fn=MagicMock(),
                review_graph=graph,
                slice_id=None,
            )
            with patch("message_store.get_message_store", return_value=fake_store):
                result = executor.check_consensus()
        finally:
            remove_peer_consensus_tracker(pipeline_id)

        assert result["is_complete"] is True
        assert result.get("fallback") == "message_bus"
