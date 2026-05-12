"""Module-level helpers for BRC regression tests (issue #2635).

Lives next to ``conftest.py`` because pytest's conftest discovery does
NOT make conftest's module-level symbols importable from sibling test
modules — only fixtures (declared with ``@pytest.fixture``) are.
Splitting plain helpers into their own module keeps both available
without contorting them into fixtures.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Mirror conftest.py's path setup so this module can be imported
# stand-alone (pytest collects conftest before test modules, but
# IDEs / type-checkers don't).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (
    _PROJECT_ROOT / "orchestrator",
    _PROJECT_ROOT / "shared",
    _PROJECT_ROOT,
):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from events import Event, EventType  # noqa: E402
from peer_consensus import (  # noqa: E402
    PeerConsensusTracker,
    create_peer_consensus_tracker,
    remove_peer_consensus_tracker,
)
from review_graph import ReviewGraph  # noqa: E402


def make_tracker(
    pipeline_id: str,
    graph: ReviewGraph,
    *,
    cooldown_seconds: int = 0,
) -> PeerConsensusTracker:
    """Build a tracker with all agents registered (cooldown=0 for tests).

    Registers the tracker in the module-level registry via
    ``create_peer_consensus_tracker`` so ``get_peer_consensus_tracker``
    — the API consumed by ``_handle_brc_consensus_timeout`` and the
    SSE bridge — finds it.  A direct ``PeerConsensusTracker(...)``
    constructor call would be a registry-bypass and break the
    timeout-handler test scenarios silently (the handler's
    ``_brc_tracker = get_peer_consensus_tracker(...)`` returns
    ``None`` and the alert paths short-circuit).
    """
    # Reset any tracker the previous test left under the same id —
    # ``create_peer_consensus_tracker`` overwrites silently, but
    # explicit removal also clears any per-agent state that survives
    # across re-creation.
    remove_peer_consensus_tracker(pipeline_id)
    tracker = create_peer_consensus_tracker(pipeline_id, graph, cooldown_seconds=cooldown_seconds)
    for role in graph.all_roles():
        tracker.register_agent(role)
    return tracker


def propose_payload(
    *,
    artifacts: list[str] | None = None,
    commit_sha: str = "abc1234",
    summary: str = "test proposal",
) -> dict[str, Any]:
    """Minimal valid ``ProposalPayload`` dict (no attestation)."""
    return {
        "summary": summary,
        "artifacts": list(artifacts or ["a.py"]),
        "commit_sha": commit_sha,
    }


def ack_payload(*, artifacts: list[str] | None = None) -> dict[str, Any]:
    """Minimal valid ``ReviewPayload`` ACK dict."""
    return {"artifact_references": list(artifacts or ["a.py"])}


def nack_payload(
    *,
    artifacts: list[str] | None = None,
    reason: str = "regression in a.py:42",
) -> dict[str, Any]:
    """Minimal valid ``ReviewPayload`` NACK dict (reason is required)."""
    return {
        "artifact_references": list(artifacts or ["a.py"]),
        "reason": reason,
    }


def filter_events(
    events: list[Event],
    *,
    pipeline_id: str,
    event_type: EventType | None = None,
) -> list[Event]:
    """Return events for ``pipeline_id`` (and optional ``event_type``)."""
    out = [e for e in events if e.pipeline_id == pipeline_id]
    if event_type is not None:
        out = [e for e in out if e.event_type == event_type]
    return out


EventFilter = Callable[..., list[Event]]
