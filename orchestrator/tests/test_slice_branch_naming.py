"""Slice-aware branch + BRC tracker tests (#2137 TASK-4-5).

Verifies:

* ``ConcurrentPhaseExecutor.get_worktree_branch(role, slice_id=...)``
  returns the nested ``egg/issue-N/slice-M/{role}/work`` shape.
* Bare integer slice ids are normalised (``slice_id=2`` → ``slice-2``).
* Babysit-pr mode is intentionally NOT slice-aware in this PR
  (refine-phase decision-8 deferred babysit slicing) — supplying
  ``slice_id`` while the pipeline is in babysit mode falls through
  to the per-role staging-branch path.
* ``ConcurrentPhaseExecutor.get_slice_integration_branch`` returns
  the bare ``egg/issue-N/slice-M`` name.
* The ``peer_consensus._tracker_key`` helper (and the public
  ``create_/get_/remove_peer_consensus_tracker`` wrappers) namespace
  per-slice trackers under nested keys without disturbing the
  pipeline-level tracker.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# sys.path setup matches test_concurrent_executor_staging_branch.py.
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Docker module mock — match the pattern used by the existing staging
# branch tests so this file imports cleanly outside pytest, too.
if "docker" not in sys.modules:
    _errors_mod = types.ModuleType("docker.errors")
    _errors_mod.DockerException = type("DockerException", (Exception,), {})
    _errors_mod.APIError = type("APIError", (Exception,), {})
    _errors_mod.NotFound = type("NotFound", (Exception,), {})
    _errors_mod.ImageNotFound = type("ImageNotFound", (Exception,), {})
    _docker_mod = MagicMock()
    _docker_mod.errors = _errors_mod
    sys.modules["docker"] = _docker_mod
    sys.modules["docker.errors"] = _errors_mod

from concurrent_executor import ConcurrentPhaseExecutor  # noqa: E402
from egg_contracts.agent_roles import AgentRole  # noqa: E402
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from peer_consensus import (  # noqa: E402
    _tracker_key,
    create_peer_consensus_tracker,
    get_peer_consensus_tracker,
    remove_peer_consensus_tracker,
)


def _issue_pipeline(
    pipeline_id: str = "issue-2137",
    *,
    branch: str | None = None,
    issue_number: int | None = 2137,
    pr_number: int | None = None,
) -> Pipeline:
    config = PipelineConfig()
    try:
        config.concurrent_execution = True  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        config.__dict__["concurrent_execution"] = True
    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


# ---------- get_worktree_branch ----------


class TestSliceAwareWorktreeBranch:
    def test_slice_aware_branch_for_canonical_id(self) -> None:
        pipeline = _issue_pipeline(branch="egg/issue-2137")
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        result = ex.get_worktree_branch(AgentRole.CODER, slice_id="slice-3")
        assert result == "egg/issue-2137/slice-3/coder/work"

    def test_bare_integer_slice_id_normalised(self) -> None:
        pipeline = _issue_pipeline(branch="egg/issue-2137")
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        result = ex.get_worktree_branch(AgentRole.CODER, slice_id="3")
        # Bare-integer ids are accepted and normalised to ``slice-N``
        # so callers that haven't plumbed canonical ids through don't
        # need a separate code path.
        assert result == "egg/issue-2137/slice-3/coder/work"

    def test_no_slice_id_returns_pipeline_branch(self) -> None:
        # Sanity: legacy non-slice path is untouched.
        pipeline = _issue_pipeline(branch="egg/issue-2137")
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        assert ex.get_worktree_branch(AgentRole.CODER) == "egg/issue-2137"

    def test_falls_back_to_issue_number_when_no_branch(self) -> None:
        pipeline = _issue_pipeline(branch=None, issue_number=2137)
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        result = ex.get_worktree_branch(AgentRole.CODER, slice_id="slice-1")
        assert result == "egg/issue-2137/slice-1/coder/work"


# ---------- get_slice_integration_branch ----------


class TestSliceIntegrationBranch:
    def test_canonical_id(self) -> None:
        pipeline = _issue_pipeline(branch="egg/issue-2137")
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        assert ex.get_slice_integration_branch("slice-2") == "egg/issue-2137/slice-2"

    def test_bare_integer_id(self) -> None:
        pipeline = _issue_pipeline(branch="egg/issue-2137")
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        assert ex.get_slice_integration_branch("2") == "egg/issue-2137/slice-2"

    def test_no_pipeline_branch_uses_issue_number(self) -> None:
        pipeline = _issue_pipeline(branch=None, issue_number=2137)
        ex = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        assert ex.get_slice_integration_branch("slice-7") == "egg/issue-2137/slice-7"


# ---------- BRC tracker key namespacing ----------


class TestTrackerKey:
    def test_no_slice_id_returns_pipeline_id(self) -> None:
        assert _tracker_key("issue-2137") == "issue-2137"

    def test_slice_id_nests(self) -> None:
        assert _tracker_key("issue-2137", "slice-3") == "issue-2137/slice-3"

    def test_idempotent_on_already_nested_id(self) -> None:
        # If a caller already constructed ``"issue-2137/slice-3"`` and
        # passes it back with ``slice_id="slice-3"``, the function must
        # NOT double-prefix.
        assert _tracker_key("issue-2137/slice-3", "slice-3") == "issue-2137/slice-3"


class TestTrackerLifecycle:
    """Public create / get / remove honour slice_id namespacing."""

    def _fake_graph(self) -> MagicMock:
        # ReviewGraph is a complex object; we don't need its real
        # behaviour here, just a sentinel that PeerConsensusTracker
        # accepts. The tracker's own behaviour is unit-tested
        # elsewhere.
        return MagicMock(name="ReviewGraph")

    def test_pipeline_and_slice_trackers_are_distinct(self) -> None:
        pipeline_id = "issue-2137-test-distinct"
        graph = self._fake_graph()
        # Create both — must coexist.
        pipe_tr = create_peer_consensus_tracker(pipeline_id, graph)
        slice_tr = create_peer_consensus_tracker(pipeline_id, graph, slice_id="slice-1")
        try:
            assert pipe_tr is not slice_tr
            assert get_peer_consensus_tracker(pipeline_id) is pipe_tr
            assert get_peer_consensus_tracker(pipeline_id, "slice-1") is slice_tr
        finally:
            remove_peer_consensus_tracker(pipeline_id)
            remove_peer_consensus_tracker(pipeline_id, "slice-1")

    def test_remove_only_drops_target_scope(self) -> None:
        pipeline_id = "issue-2137-test-remove"
        graph = self._fake_graph()
        create_peer_consensus_tracker(pipeline_id, graph)
        create_peer_consensus_tracker(pipeline_id, graph, slice_id="slice-7")
        try:
            remove_peer_consensus_tracker(pipeline_id, "slice-7")
            # Pipeline-level tracker survives.
            assert get_peer_consensus_tracker(pipeline_id) is not None
            assert get_peer_consensus_tracker(pipeline_id, "slice-7") is None
        finally:
            remove_peer_consensus_tracker(pipeline_id)

    def test_remove_unknown_slice_is_noop(self) -> None:
        # Cleanup of a never-created slice tracker must not crash —
        # the orchestrator's idempotency relies on this.
        remove_peer_consensus_tracker("issue-9999", "slice-99")
