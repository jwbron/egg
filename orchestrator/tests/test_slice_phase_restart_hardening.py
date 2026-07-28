"""Slice/phase restart hardening tests (#2777, bundles #2409 / #2928).

Covers the restart- and recovery-hardening surface that keeps a sliced
implement phase consistent across operator restarts and orchestrator-pod
recycles:

* **Slice-aware ``restart_phase`` consensus-clear** — in addition to the
  pipeline-level ``tracker.clear()`` in ``orchestrator/routes/pipelines.py``,
  the handler loads the contract, iterates ``contract.slices`` and calls
  ``get_peer_consensus_tracker(pipeline_id, slice_id=s.id).clear()`` for each
  slice (mirroring the slice-aware ``restart_agent`` path). Empty
  ``contract.slices`` still clears the pipeline-level tracker, and a contract
  load failure falls back to pipeline-level-only clearing rather than blocking
  the restart.

* **Eager-persist ``parent_branch_at_creation`` + ``PENDING → IN_PROGRESS``
  flip** — the parent-branch persist site flips the slice status in the SAME
  contract-locked write, so a crash between the status flip and branch
  creation cannot leave the field empty. The flip is idempotent: only PENDING
  is flipped; COMPLETE / BLOCKED / IN_PROGRESS are left untouched.

* **Parent-existence gate in ``_resolve_slice_base_branch`` (#2928)** — an
  optional ``parent_branch_exists(parent_branch) -> bool`` callback decides the
  base for a non-root slice with no recorded parent. ``True`` → the
  dependency-derived parent (correct for fresh AND legacy slices). ``False`` →
  ``pipeline_branch`` (the parent PR merged into ``work`` and its branch was
  cascade-deleted, so ``work`` already contains its commits). A raised probe is
  treated conservatively as ``True`` so a flaky gateway never silently swaps a
  real slice onto ``work``. This replaced an earlier merge-base probe that
  probed the slice's OWN not-yet-created integration branch and so mis-based
  every fresh non-root slice onto ``work`` whenever ``work`` had advanced ahead
  of the parent.

* **Bootstrap reconciliation 5-way classification** — module-level
  ``_classify_non_complete_slice`` returns one of five labels for every
  non-COMPLETE slice:

  - ``"fresh"`` — IN_PROGRESS/PENDING + no commits on origin → no Layer-C
    action; scheduler re-yields READY.
  - ``"resume"``: IN_PROGRESS + commits + no consensus → no scheduler
    action, so the slice re-yields READY and the run loop re-drives it
    (#3685: re-driving is what starts the slice's BRC event loop, and
    ``_run_one_slice`` is the only caller that can complete it; the old
    ``scheduler.mark_spawned`` parked the slice in RUNNING with neither).
  - ``"consensus_complete"`` — IN_PROGRESS + commits + consensus REACHED →
    mark COMPLETE so the slice-PR opener fires.
  - ``"blocked"`` — BLOCKED → preserve status; caller escalates via
    ``_escalate_blocked_slice_to_hitl`` (writes a new HITL ``Decision`` to the
    contract) when no pending decision is found.
  - ``"corrupt"`` — impossible status enum or contradictory state combination
    → caller escalates via ``_escalate_corrupt_slice_to_hitl``. PENDING +
    commits-on-origin is one such impossibility (the status flip above lands
    before any commits could), so the classifier wakes the operator instead of
    re-yielding READY.

* **Per-slice consensus tracker reconstruction (#2409)** —
  ``startup_reconciliation.py`` iterates ``contract.slices`` via
  ``_enumerate_contract_slices`` and calls
  ``reconstruct_tracker_from_messages(pipeline_id, graph, slice_id=s.id)`` for
  each slice in addition to the pipeline-level call.
  ``handle_consensus_confirmed_signal`` reconstructs even when ``slice_id`` is
  supplied — the metadata filter in ``message_store.py`` (#2725) is the
  canonical scope mechanism. Two concurrent slices reconstruct via the
  slice-id filter without one slice's ack/propose records bleeding into the
  other's tracker.

The classes below also cover ``_reap_orphaned_slice_jobs`` (#3685), the
``_escalate_layer_c_hitl`` HITL persistence helpers, and
``GatewayClient.merge_base`` strict SHA-shape + session-auth bootstrapping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# sys.path setup matches other orchestrator/tests files.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing routes.pipelines.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from egg_contracts.models import (  # noqa: E402
    Contract,
    IssueInfo,
    Slice,
    SliceStatus,
    Task,
    TaskStatus,
)
from egg_contracts.models import (  # noqa: E402
    PipelinePhase as ContractPhase,
)
from message_store import Message, MessageType  # noqa: E402
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from peer_consensus import (  # noqa: E402
    _tracker_key,
    _trackers,
    _trackers_lock,
    get_peer_consensus_tracker,
    reconstruct_tracker_from_messages,
)
from review_graph import get_review_graph_for_phase  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pipeline(pipeline_id: str = "issue-2777-slice-4") -> Pipeline:
    config = PipelineConfig(
        concurrent_execution=True,
        max_concurrent_agents=6,
        consensus_timeout_minutes=30,
    )
    return Pipeline(
        id=pipeline_id,
        issue_number=2777,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}/work",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_slice(
    slice_id: str,
    *,
    deps: list[str] | None = None,
    parent_branch_at_creation: str | None = None,
    status: SliceStatus = SliceStatus.PENDING,
    task_idx: int = 1,
) -> Slice:
    """Build a contract Slice.

    Task IDs must match ``^task-[0-9]+(-[0-9]+)?$`` per
    ``shared/egg_contracts/models.py`` — we derive ``task-N`` from the
    caller-supplied ``task_idx`` rather than embedding the slice id
    (which contains dashes and would fail the pydantic regex).
    """
    return Slice(
        id=slice_id,
        name=f"Slice {slice_id}",
        status=status,
        dependencies=deps or [],
        parent_branch_at_creation=parent_branch_at_creation,
        tasks=[
            Task(
                id=f"task-{task_idx}",
                description="t",
                status=TaskStatus.PENDING,
                files_affected=[],
            )
        ],
    )


def _make_contract(
    pipeline_id: str = "issue-2777-slice-4",
    issue_number: int = 2777,
    slices: list[Slice] | None = None,
) -> Contract:
    return Contract(
        schemaVersion="1.2",
        issue=IssueInfo(number=issue_number, title=f"#{issue_number}", url=""),
        pipeline_id=pipeline_id,
        current_phase=ContractPhase.IMPLEMENT,
        slices=slices or [],
    )


def _purge_tracker_keys(pipeline_id: str, slice_ids: list[str | None]) -> None:
    """Drop every named tracker key from the global registry.

    Used by setup_method / teardown_method so a leaking tracker from
    one test does not pollute the next test's registry.
    """
    with _trackers_lock:
        for sid in slice_ids:
            _trackers.pop(_tracker_key(pipeline_id, sid), None)


def _propose_message(
    pipeline_id: str,
    *,
    from_role: str,
    slice_id: str | None,
    commit_sha: str = "abc1234",
) -> Message:
    """Build a CONSENSUS_PROPOSE Message whose payload metadata passes
    the ProposalPayload validator at peer_consensus.py:~2090-2102.

    Used by the per-slice reconstruction tests so the replay path
    actually populates the tracker's proposal slot rather than skipping
    the message as invalid.
    """
    payload = {
        "summary": f"{from_role} slice {slice_id} proposal",
        "artifacts": ["a.py"],
        "commit_sha": commit_sha,
    }
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=MessageType.CONSENSUS_PROPOSE,
        subject=f"propose by {from_role}",
        body=json.dumps(payload),
        phase="implement",
        metadata={
            "slice_id": slice_id,
            "payload": payload,
            "version": 1,
        },
    )


def _confirmed_message(
    pipeline_id: str,
    *,
    from_role: str,
    slice_id: str | None,
) -> Message:
    """Build a CONSENSUS_CONFIRMED Message tagged with ``slice_id`` metadata."""
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=MessageType.CONSENSUS_CONFIRMED,
        subject=f"{from_role} confirmed",
        body="",
        phase="implement",
        metadata={"slice_id": slice_id, "consensus_reached": True},
    )


# ---------------------------------------------------------------------------
# Slice-aware restart_phase consensus-clear
# ---------------------------------------------------------------------------


class TestRestartPhaseClearsPerSliceTrackers:
    """``restart_phase`` clears the pipeline-level tracker AND every
    per-slice tracker registered for ``contract.slices``.

    Mirrors the existing ``restart_agent`` pattern at
    ``pipelines.py:~2859`` which already calls
    ``get_peer_consensus_tracker(pipeline_id, slice_id)``.
    """

    PIPELINE_ID = "issue-2777-restart"

    def setup_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1", "slice-2", "slice-3"])

    def teardown_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1", "slice-2", "slice-3"])

    def _flask_client(self):
        from flask import Flask
        from routes.pipelines import pipelines_bp

        app = Flask(__name__)
        app.register_blueprint(pipelines_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def _make_pipeline_for_restart(self) -> Pipeline:
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            AgentRole,
            ContainerInfo,
            ContainerStatus,
            PhaseExecution,
        )

        pipeline = _make_pipeline(self.PIPELINE_ID)
        pipeline.phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                review_cycles=1,
                containers=[
                    ContainerInfo(
                        container_id="coder-c",
                        container_name="egg-coder",
                        agent_role=AgentRole.CODER,
                        status=ContainerStatus.RUNNING,
                    )
                ],
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.RUNNING,
                        container_id="coder-c",
                    )
                ],
            )
        }
        return pipeline

    def test_restart_phase_clears_pipeline_level_tracker(self) -> None:
        """Pre-slice-4 behaviour MUST still hold: the bare
        pipeline-level tracker is cleared on phase restart.

        A future refactor that switched every ``tracker.clear()`` to
        per-slice-only would silently break single-slice and legacy
        pipelines — this test guards against that.
        """
        from unittest.mock import patch

        graph = get_review_graph_for_phase("implement", repo="owner/repo")
        from peer_consensus import create_peer_consensus_tracker

        tracker = create_peer_consensus_tracker(self.PIPELINE_ID, graph)
        tracker.register_agent("coder")

        pipeline = self._make_pipeline_for_restart()
        contract = _make_contract(pipeline_id=self.PIPELINE_ID, slices=[])
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        client = self._flask_client()
        with (
            patch("routes.pipelines.threading.Thread"),
            patch("routes.pipelines.get_container_spawner", return_value=MagicMock()),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_repo_path", return_value="/repo"),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            response = client.post(
                f"/api/v1/pipelines/{self.PIPELINE_ID}/phases/implement/restart",
                json={"reason": "task-4-1 pipeline tracker"},
            )

        assert response.status_code == 200, (
            f"restart_phase failed: {response.status_code} {response.get_data()!r}"
        )
        # Pipeline-level tracker still registered, but cleared.
        # ``PeerConsensusTracker.clear()`` resets the producer / reviewer
        # phase dicts but does not deregister agents — probe the private
        # state that ``clear()`` actually empties (``_producer_phases``,
        # ``_confirmed``) so this test catches a regression that drops
        # the ``clear()`` call entirely.
        cleared = get_peer_consensus_tracker(self.PIPELINE_ID)
        if cleared is not None:
            assert dict(cleared._producer_phases) == {}, (
                "pipeline-level tracker._producer_phases was NOT cleared on phase restart"
            )
            assert dict(cleared._confirmed) == {}, (
                "pipeline-level tracker._confirmed was NOT cleared on phase restart"
            )

    def test_restart_phase_clears_each_per_slice_tracker(self) -> None:
        """Slice-aware behaviour (TASK-4-1): every per-slice tracker
        ``_tracker_key(pipeline_id, slice_id)`` is cleared in
        addition to the pipeline-level one.

        Set up three pre-populated per-slice trackers, fire the
        restart, assert each one's state is empty afterwards.
        """
        from unittest.mock import patch

        from peer_consensus import create_peer_consensus_tracker

        graph = get_review_graph_for_phase("implement", repo="owner/repo")
        # Pipeline-level tracker plus three per-slice ones, each with
        # a recorded PROPOSE so ``_producer_phases`` is non-empty
        # (the assertion below checks ``_producer_phases`` is reset
        # to ``{}`` by ``clear()``).
        pl = create_peer_consensus_tracker(self.PIPELINE_ID, graph)
        pl.register_agent("coder")
        pl.handle_propose(
            "coder",
            {"summary": "s", "artifacts": ["a.py"], "commit_sha": "deadbeef"},
        )
        for sid in ("slice-1", "slice-2", "slice-3"):
            t = create_peer_consensus_tracker(self.PIPELINE_ID, graph, slice_id=sid)
            t.register_agent("coder")
            t.register_agent("tester")
            t.handle_propose(
                "coder",
                {
                    "summary": "s",
                    "artifacts": ["a.py"],
                    "commit_sha": f"sha_{sid.replace('-', '_')}",
                },
            )

        slices = [
            _make_slice(sid, task_idx=i + 1)
            for i, sid in enumerate(["slice-1", "slice-2", "slice-3"])
        ]
        contract = _make_contract(pipeline_id=self.PIPELINE_ID, slices=slices)

        pipeline = self._make_pipeline_for_restart()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        client = self._flask_client()
        with (
            patch("routes.pipelines.threading.Thread"),
            patch("routes.pipelines.get_container_spawner", return_value=MagicMock()),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_repo_path", return_value="/repo"),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            response = client.post(
                f"/api/v1/pipelines/{self.PIPELINE_ID}/phases/implement/restart",
                json={"reason": "task-4-1 per-slice tracker"},
            )

        assert response.status_code == 200
        for sid in ("slice-1", "slice-2", "slice-3"):
            t = get_peer_consensus_tracker(self.PIPELINE_ID, slice_id=sid)
            assert t is not None, (
                f"slice tracker for {sid} disappeared (clear should not deregister)"
            )
            assert dict(t._producer_phases) == {}, (
                f"per-slice tracker for {sid} was not cleared on phase restart "
                f"(slice-4 TASK-4-1) — _producer_phases still: {dict(t._producer_phases)}"
            )
        # Pipeline-level tracker also cleared (the existing pre-slice-4
        # behaviour must keep firing).
        pl_after = get_peer_consensus_tracker(self.PIPELINE_ID)
        assert pl_after is not None
        assert dict(pl_after._producer_phases) == {}, (
            "pipeline-level tracker was not cleared on phase restart"
        )

    def test_restart_phase_handles_empty_contract_slices(self) -> None:
        """A non-sliced (or fresh) pipeline has ``contract.slices == []``.
        The slice-aware iteration must NOT crash on that — it must
        gracefully no-op the per-slice clear and fall through to the
        pipeline-level clear.
        """
        from unittest.mock import patch

        from peer_consensus import create_peer_consensus_tracker

        graph = get_review_graph_for_phase("implement", repo="owner/repo")
        create_peer_consensus_tracker(self.PIPELINE_ID, graph).register_agent("coder")

        pipeline = self._make_pipeline_for_restart()
        contract = _make_contract(pipeline_id=self.PIPELINE_ID, slices=[])
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        client = self._flask_client()
        with (
            patch("routes.pipelines.threading.Thread"),
            patch("routes.pipelines.get_container_spawner", return_value=MagicMock()),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_repo_path", return_value="/repo"),
            patch("egg_contracts.loader.load_contract", return_value=contract),
        ):
            response = client.post(
                f"/api/v1/pipelines/{self.PIPELINE_ID}/phases/implement/restart",
                json={"reason": "empty contract.slices"},
            )
        assert response.status_code == 200, (
            "empty contract.slices must not crash the slice-aware iteration"
        )

    def test_restart_phase_survives_contract_load_failure(self) -> None:
        """Adversarial probe: if the contract is corrupt / missing,
        ``restart_phase`` must STILL succeed — the pipeline-level
        clear above already ran, and per-slice clear is best-effort.
        """
        from unittest.mock import patch

        from peer_consensus import create_peer_consensus_tracker

        graph = get_review_graph_for_phase("implement", repo="owner/repo")
        create_peer_consensus_tracker(self.PIPELINE_ID, graph).register_agent("coder")

        pipeline = self._make_pipeline_for_restart()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        client = self._flask_client()
        with (
            patch("routes.pipelines.threading.Thread"),
            patch("routes.pipelines.get_container_spawner", return_value=MagicMock()),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_repo_path", return_value="/repo"),
            patch(
                "egg_contracts.loader.load_contract",
                side_effect=RuntimeError("corrupt contract on disk"),
            ),
        ):
            response = client.post(
                f"/api/v1/pipelines/{self.PIPELINE_ID}/phases/implement/restart",
                json={"reason": "load_contract raises"},
            )
        assert response.status_code == 200, (
            "contract load failure during restart_phase must not block the restart"
        )


# ---------------------------------------------------------------------------
# Eager-persist parent_branch_at_creation + PENDING → IN_PROGRESS flip
# ---------------------------------------------------------------------------


class TestEagerPersistParentBranchAtCreationAndStatusFlip:
    """The slice's ``parent_branch_at_creation`` field AND the PENDING
    → IN_PROGRESS status flip are written in the SAME contract write,
    under the per-pipeline state lock (cq-9 / crash recovery)."""

    def test_pending_flips_to_in_progress_in_same_save_as_parent_branch(self) -> None:
        """The save that records ``parent_branch_at_creation`` MUST
        also flip the slice status to IN_PROGRESS in the same
        contract mutation."""
        from unittest.mock import patch

        from routes.pipelines import _run_implement_phase_slices

        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", status=SliceStatus.PENDING)
        contract = _make_contract(slices=[slice_obj])

        save_snapshots: list[tuple[SliceStatus, str | None]] = []

        def _capture_save(contract_arg, *_args, **_kwargs):
            for s in contract_arg.slices:
                if s.id == "slice-1":
                    save_snapshots.append((s.status, s.parent_branch_at_creation))

        spawner = MagicMock()
        spawner.gateway = MagicMock()
        spawner.gateway.create_slice_pr.return_value = "https://example/pr/1"
        spawner.gateway.is_slice_branch_merged_into_parent.return_value = False
        spawner.gateway.get_remote_branch_sha.return_value = None

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=_capture_save),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
            patch(
                "routes.pipelines._resolve_slice_base_branch",
                return_value="egg/issue-2777-slice-4/work",
            ),
            patch("routes.pipelines._open_context_pr_at_implement_start"),
        ):
            import threading as _threading

            mock_recon.return_value = (MagicMock(), _threading.Event())
            _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        # At least one save snapshot must have IN_PROGRESS *and*
        # parent_branch_at_creation populated together.
        atomic_writes = [
            (status, pb)
            for status, pb in save_snapshots
            if status == SliceStatus.IN_PROGRESS and pb == "egg/issue-2777-slice-4/work"
        ]
        assert atomic_writes, (
            f"PENDING → IN_PROGRESS flip + parent_branch_at_creation persist "
            f"never observed in the same save; saves: {save_snapshots}"
        )

    def test_status_flip_is_idempotent_for_non_pending(self) -> None:
        """Idempotency probe: re-entering the persist site with a slice
        already in IN_PROGRESS (or BLOCKED, or COMPLETE) MUST NOT
        forcibly flip the status. Slice-3 already ships the parent-
        branch persist; slice-4 layers the status flip on top, and
        the flip must be gated on ``if s.status == SliceStatus.PENDING``.
        """
        from unittest.mock import patch

        from routes.pipelines import _run_implement_phase_slices

        pipeline = _make_pipeline()
        # Slice enters in IN_PROGRESS already (e.g. orphan reconciler
        # re-entry).
        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        contract = _make_contract(slices=[slice_obj])

        save_snapshots: list[SliceStatus] = []

        def _capture_save(contract_arg, *_args, **_kwargs):
            for s in contract_arg.slices:
                if s.id == "slice-1":
                    save_snapshots.append(s.status)

        spawner = MagicMock()
        spawner.gateway = MagicMock()
        spawner.gateway.create_slice_pr.return_value = "https://example/pr/1"
        spawner.gateway.is_slice_branch_merged_into_parent.return_value = False
        spawner.gateway.get_remote_branch_sha.return_value = None

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=_capture_save),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
            patch("routes.pipelines._commit_slice_brc_history_to_integration_branch"),
            patch(
                "routes.pipelines._resolve_slice_base_branch",
                return_value="egg/issue-2777-slice-4/work",
            ),
            patch("routes.pipelines._open_context_pr_at_implement_start"),
        ):
            import threading as _threading

            mock_recon.return_value = (MagicMock(), _threading.Event())
            _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )

        # IN_PROGRESS must NEVER be flipped back to PENDING by the
        # eager-persist site. ``_persist_slice_status_complete`` may
        # later flip the slice to COMPLETE after consensus reaches —
        # that's the normal terminal path and is allowed; only the
        # invalid PENDING regression matters here.
        assert SliceStatus.PENDING not in save_snapshots, (
            f"IN_PROGRESS slice was forcibly demoted to PENDING by the "
            f"persist site (TASK-4-2 idempotency regression); "
            f"saves: {save_snapshots}"
        )
        # And the FIRST save (the eager-persist write) MUST preserve
        # IN_PROGRESS — that's the slice-4 idempotency contract.
        assert save_snapshots[0] == SliceStatus.IN_PROGRESS, (
            f"first save after slice entry mutated IN_PROGRESS to "
            f"{save_snapshots[0]!r}; expected IN_PROGRESS unchanged"
        )


# ---------------------------------------------------------------------------
# ``branch_has_origin_commits`` fallback in _resolve_slice_base_branch
# ---------------------------------------------------------------------------


class TestResolveSliceBaseBranchPreservesExistingBehaviour:
    """Smoke / regression tests pinning the *pre-slice-4* behaviour of
    ``_resolve_slice_base_branch``. These MUST keep passing after
    slice-4 lands its ``branch_has_origin_commits`` fallback — the
    new arm only kicks in when ``parent_branch_at_creation`` is
    empty AND the probe is supplied.
    """

    def test_populated_parent_branch_short_circuits(self) -> None:
        """A populated ``parent_branch_at_creation`` short-circuits at
        the first check. The eager-persisted value is authoritative
        and faster — no merge-base call is made.
        """
        from routes.pipelines import _resolve_slice_base_branch

        contract = _make_contract(
            slices=[
                _make_slice(
                    "slice-1",
                    parent_branch_at_creation="egg/issue-2777-slice-4/recorded-parent",
                )
            ]
        )
        probe = MagicMock(name="parent_branch_exists")
        result = _resolve_slice_base_branch(
            contract,
            "slice-1",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            parent_branch_exists=probe,
        )
        assert result == "egg/issue-2777-slice-4/recorded-parent"
        # Probe NOT consulted (short-circuit).
        probe.assert_not_called()

    def test_falls_back_to_pipeline_branch_when_no_parent_recorded_root(self) -> None:
        """An orphaned root slice (no deps, no recorded parent) falls
        back to the pipeline branch — the canonical context-PR head
        under the post-slice-3 topology.
        """
        from routes.pipelines import _resolve_slice_base_branch

        contract = _make_contract(
            slices=[
                _make_slice("slice-1", parent_branch_at_creation=None),
            ]
        )
        result = _resolve_slice_base_branch(
            contract,
            "slice-1",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
        )
        assert result == "egg/issue-2777-slice-4/work"

    def test_non_root_slice_derives_from_dependency_without_probe(self) -> None:
        """A non-root slice with no recorded parent AND no probe still
        derives the parent branch from ``slice.dependencies[0]`` —
        preserves the current ``f"{issue_branch}/{parent_slice_id}"``
        convention.
        """
        from routes.pipelines import _resolve_slice_base_branch

        contract = _make_contract(
            slices=[
                _make_slice("slice-1", parent_branch_at_creation=None),
                _make_slice(
                    "slice-2",
                    deps=["slice-1"],
                    parent_branch_at_creation=None,
                    task_idx=2,
                ),
            ]
        )
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
        )
        assert result == "egg/issue-2777-slice-4/slice-1"


class TestResolveSliceBaseBranchParentExistenceGate:
    """The ``parent_branch_exists`` arm (#2928): a non-root slice with
    no recorded parent invokes ``parent_branch_exists(parent_branch)``
    to decide between the dependency-derived parent and
    ``pipeline_branch``.

    * ``True`` → dependency-derived parent (correct for fresh AND
      legacy slices).
    * ``False`` → ``pipeline_branch`` (the parent PR merged into
      ``work`` and its branch was cascade-deleted).
    * Probe raises → conservative default (treated as ``True``) → the
      derived parent, so a flaky gateway never silently swaps the
      parent.
    """

    def test_fresh_slice_with_existing_parent_uses_derived_parent(self) -> None:
        """#2928 headline regression: a FRESH non-root slice (its own
        integration branch does not exist yet) whose dependency parent
        branch IS on origin must stack on the derived parent — NOT on
        ``pipeline_branch``. This is the case the old merge-base probe
        mis-routed onto ``work`` (the slice's own branch had no fork
        point because it had not been created yet).
        """
        from routes.pipelines import _resolve_slice_base_branch

        contract = _make_contract(
            slices=[
                _make_slice("slice-1", parent_branch_at_creation=None),
                _make_slice(
                    "slice-2",
                    deps=["slice-1"],
                    parent_branch_at_creation=None,
                    task_idx=2,
                ),
            ]
        )
        probe = MagicMock(name="parent_branch_exists", return_value=True)
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            parent_branch_exists=probe,
        )
        # Probe is asked about the DEPENDENCY PARENT branch (bare
        # name), never the slice's own integration branch.
        probe.assert_called_once_with("egg/issue-2777-slice-4/slice-1")
        assert result == "egg/issue-2777-slice-4/slice-1"

    def test_absent_parent_falls_back_to_pipeline_branch(self) -> None:
        """Parent branch gone from origin (merged into ``work`` and
        cascade-deleted) ⇒ resolver returns ``pipeline_branch``, which
        already contains the parent's commits."""
        from routes.pipelines import _resolve_slice_base_branch

        contract = _make_contract(
            slices=[
                _make_slice("slice-1", parent_branch_at_creation=None),
                _make_slice(
                    "slice-2",
                    deps=["slice-1"],
                    parent_branch_at_creation=None,
                    task_idx=2,
                ),
            ]
        )
        probe = MagicMock(name="parent_branch_exists", return_value=False)
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            parent_branch_exists=probe,
        )
        probe.assert_called_once_with("egg/issue-2777-slice-4/slice-1")
        assert result == "egg/issue-2777-slice-4/work", (
            "non-root slice whose dependency parent branch is absent "
            "must fall back to pipeline_branch (#2928)"
        )

    def test_probe_failure_falls_through_to_derived_parent(self) -> None:
        """Adversarial probe: a flaky gateway raises mid-probe. The
        resolver MUST treat the parent as existing and return the
        derived-parent path rather than silently swap to
        ``pipeline_branch`` (which would re-stack a real slice onto the
        wrong base).
        """
        from routes.pipelines import _resolve_slice_base_branch

        contract = _make_contract(
            slices=[
                _make_slice("slice-1", parent_branch_at_creation=None),
                _make_slice(
                    "slice-2",
                    deps=["slice-1"],
                    parent_branch_at_creation=None,
                    task_idx=2,
                ),
            ]
        )

        def _flaky(_parent_branch: str) -> bool:
            raise RuntimeError("gateway down")

        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            parent_branch_exists=_flaky,
        )
        # Conservative default ("parent exists") preserves the derived
        # parent — never silently swap on a flaky probe.
        assert result == "egg/issue-2777-slice-4/slice-1"


# ---------------------------------------------------------------------------
# Bootstrap reconciliation 5-way classification
# ---------------------------------------------------------------------------


class TestClassifyNonCompleteSlice:
    """Direct unit tests against ``_classify_non_complete_slice`` —
    the pure classifier exposed as a module-level helper so the five
    cells of the R5 matrix are exhaustively covered without driving
    the full slice loop."""

    def _make_gateway(self, *, has_commits: bool, raise_exc: bool = False) -> MagicMock:
        gateway = MagicMock(name="gateway")
        if raise_exc:
            gateway.get_remote_branch_sha.side_effect = RuntimeError("probe blew up")
        else:
            # ``get_remote_branch_sha`` returns SHA string or None.
            gateway.get_remote_branch_sha.return_value = "abcd1234" if has_commits else None
        return gateway

    def test_d1_in_progress_no_commits_classifies_fresh(self) -> None:
        """(d1) IN_PROGRESS + no commits pushed → ``"fresh"``: the
        scheduler re-yields READY and the run loop spawns fresh
        agents."""
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        gateway = self._make_gateway(has_commits=False)
        tracker_lookup = MagicMock(return_value=None)

        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=tracker_lookup,
        )
        assert result == "fresh"
        # Tracker lookup not consulted when has_commits=False.
        tracker_lookup.assert_not_called()

    def test_d2_in_progress_commits_no_consensus_classifies_resume(self) -> None:
        """(d2) IN_PROGRESS + commits pushed + consensus NOT
        reached → ``"resume"``: caller takes no scheduler action, so
        the run loop re-drives the slice onto the commits already on
        its integration branch (#3685)."""
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        gateway = self._make_gateway(has_commits=True)
        # Tracker exists but consensus has NOT reached.
        tracker = MagicMock()
        tracker.evaluate.return_value = {"is_complete": False}
        tracker_lookup = MagicMock(return_value=tracker)

        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=tracker_lookup,
        )
        assert result == "resume"
        tracker_lookup.assert_called_with("issue-2777-slice-4", "slice-1")

    def test_d2_in_progress_commits_no_tracker_classifies_resume(self) -> None:
        """Edge case for (d2): no tracker exists (e.g. message bus
        empty after a long pod restart). Classifier defaults to
        ``"resume"`` rather than ``"consensus_complete"`` — the
        conservative call is to keep the slice running, not to
        silently advance it.
        """
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        gateway = self._make_gateway(has_commits=True)
        tracker_lookup = MagicMock(return_value=None)
        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=tracker_lookup,
        )
        assert result == "resume"

    def test_d3_in_progress_consensus_reached_classifies_consensus_complete(self) -> None:
        """(d3) IN_PROGRESS + commits + consensus REACHED → caller
        marks the slice COMPLETE so the slice-PR opener fires."""
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        gateway = self._make_gateway(has_commits=True)
        tracker = MagicMock()
        tracker.evaluate.return_value = {"is_complete": True}
        tracker_lookup = MagicMock(return_value=tracker)

        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=tracker_lookup,
        )
        assert result == "consensus_complete"

    def test_d4_blocked_classifies_blocked(self) -> None:
        """(d4) BLOCKED → ``"blocked"``: caller preserves status;
        the classifier itself does not validate the pending HITL
        (callers do via ``_slice_has_pending_decision``).
        """
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.BLOCKED)
        gateway = self._make_gateway(has_commits=False)
        tracker_lookup = MagicMock()
        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=tracker_lookup,
        )
        assert result == "blocked"

    def test_d5_pending_with_commits_classifies_corrupt(self) -> None:
        """(d5) PENDING with commits on origin = state-machine
        impossibility (TASK-4-2 flips PENDING → IN_PROGRESS in the
        SAME write that records the parent branch BEFORE any commits
        could land). Must classify as ``"corrupt"`` so the operator
        is woken.
        """
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.PENDING)
        gateway = self._make_gateway(has_commits=True)
        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=MagicMock(),
        )
        assert result == "corrupt"

    def test_repoless_pipeline_classifies_fresh(self) -> None:
        """A pipeline with ``pipeline_repo is None`` (test
        scaffolds) must NOT crash on the gateway probe — the
        classifier treats it as ``has_commits=False`` → ``"fresh"``.
        """
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        gateway = MagicMock()
        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo=None,
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=MagicMock(),
        )
        assert result == "fresh"
        # Probe NOT consulted when pipeline_repo is None.
        gateway.get_remote_branch_sha.assert_not_called()

    def test_probe_failure_classifies_fresh(self) -> None:
        """Adversarial probe: the gateway probe raises mid-call.
        The classifier MUST default to ``"fresh"`` rather than
        ``"resume"`` so a slice whose true origin state is unknown
        takes the plainest recovery path.
        """
        from routes.pipelines import _classify_non_complete_slice

        slice_obj = _make_slice("slice-1", status=SliceStatus.IN_PROGRESS)
        gateway = self._make_gateway(has_commits=False, raise_exc=True)
        result = _classify_non_complete_slice(
            pipeline_id="issue-2777-slice-4",
            slice_obj=slice_obj,
            issue_branch="egg/issue-2777-slice-4",
            pipeline_repo="owner/repo",
            worktree_repo_path=Path("/tmp/x"),
            gateway=gateway,
            gateway_mode="public",
            consensus_tracker_lookup=MagicMock(),
        )
        assert result == "fresh"


# ---------------------------------------------------------------------------
# #3685: _reap_orphaned_slice_jobs(), the foreground teardown of Jobs whose
# BRC event loop died with the previous orchestrator process
#
# Layer-C case 2 re-drives every non-COMPLETE slice through the run loop
# (that is the only path that starts a slice's event loop, and the only
# one that can ever complete it). A Job that survived the restart holds
# the role's worktree and would race the fresh cohort's Job on the same
# checkout (#3337), and the spawner's live-key adoption cannot collapse
# the duplicate: the orphan's dedupe key was derived against a tracker
# this process no longer has. So the re-drive reaps first.
#
# The helper must be defensive:
# - Removes only live pods (Pending / Creating / Running)
# - Leaves terminal pods alone (they are already gone)
# - Filters by both pipeline_id AND slice_id labels (not just pipeline)
# - Swallows a failed label query and a failed removal: a reap failure
#   must never block recovery of a slice that is ready to re-drive
# ---------------------------------------------------------------------------


class TestReapOrphanedSliceJobs:
    """Exercise ``_reap_orphaned_slice_jobs`` against a stubbed spawner.

    The helper takes ``spawner`` as a parameter (paralleling how
    ``_classify_non_complete_slice`` takes ``gateway``) so tests inject
    a stub directly without patching ``routes.pipelines._get_spawner``.
    """

    @staticmethod
    def _make_container_info(container_id: str, status):
        from models import ContainerInfo

        return ContainerInfo(
            container_id=container_id,
            container_name=f"egg-{container_id}",
            status=status,
        )

    def _make_spawner(self, returned_pods):
        """Build a spawner stub whose backend.list_containers yields
        the given pods."""
        backend = MagicMock()
        backend.list_containers.return_value = returned_pods
        spawner = MagicMock()
        spawner.backend = backend
        return spawner

    def test_reaps_running_pod(self):
        """A RUNNING pod is an orphan of the dead loop: force-remove it."""
        from models import ContainerStatus
        from routes.pipelines import _reap_orphaned_slice_jobs

        spawner = self._make_spawner([self._make_container_info("p1", ContainerStatus.RUNNING)])

        assert _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-1") == ["p1"]
        spawner.remove_agent_container.assert_called_once_with(
            "p1", force=True, cleanup_session=True
        )

    def test_reaps_pending_and_creating_pods(self):
        """PENDING / CREATING pods are live too (``LIVE_POD_STATUSES``):
        a Job mid-spawn when the orchestrator went down is just as
        orphaned as a Running one, and will attach to the same worktree
        the moment its pod starts."""
        from models import ContainerStatus
        from routes.pipelines import _reap_orphaned_slice_jobs

        spawner = self._make_spawner(
            [
                self._make_container_info("p1", ContainerStatus.PENDING),
                self._make_container_info("p2", ContainerStatus.CREATING),
            ]
        )

        assert _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-1") == ["p1", "p2"]
        assert spawner.remove_agent_container.call_count == 2

    def test_skips_terminal_pods(self):
        """EXITED / FAILED pods have already gone; removing them would
        be a wasted API round-trip per bootstrap."""
        from models import ContainerStatus
        from routes.pipelines import _reap_orphaned_slice_jobs

        spawner = self._make_spawner(
            [
                self._make_container_info("p1", ContainerStatus.EXITED),
                self._make_container_info("p2", ContainerStatus.FAILED),
            ]
        )

        assert _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-1") == []
        spawner.remove_agent_container.assert_not_called()

    def test_no_pods_is_a_noop(self):
        """The common case after a full pod recycle: nothing to reap."""
        from routes.pipelines import _reap_orphaned_slice_jobs

        spawner = self._make_spawner([])

        assert _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-1") == []
        spawner.remove_agent_container.assert_not_called()

    def test_swallows_list_error(self):
        """Defensive: a k8s API error must not abort the re-drive."""
        from routes.pipelines import _reap_orphaned_slice_jobs

        backend = MagicMock()
        backend.list_containers.side_effect = RuntimeError("k8s unreachable")
        spawner = MagicMock()
        spawner.backend = backend

        assert _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-1") == []

    def test_swallows_removal_error_and_continues(self):
        """One failed removal must not skip the remaining orphans, and
        must not propagate: the slice still needs to be re-driven."""
        from models import ContainerStatus
        from routes.pipelines import _reap_orphaned_slice_jobs

        spawner = self._make_spawner(
            [
                self._make_container_info("p1", ContainerStatus.RUNNING),
                self._make_container_info("p2", ContainerStatus.RUNNING),
            ]
        )
        spawner.remove_agent_container.side_effect = [RuntimeError("boom"), None]

        assert _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-1") == ["p2"]
        assert spawner.remove_agent_container.call_count == 2

    def test_filters_by_pipeline_and_slice_labels(self):
        """The query must carry both labels so a sibling slice's live
        cohort is never reaped by this slice's bootstrap."""
        from models import ContainerStatus
        from routes.pipelines import _reap_orphaned_slice_jobs

        backend = MagicMock()
        backend.list_containers.return_value = [
            self._make_container_info("p1", ContainerStatus.RUNNING),
        ]
        spawner = MagicMock()
        spawner.backend = backend

        _reap_orphaned_slice_jobs(spawner, "pipeline-x", "slice-2")

        labels = backend.list_containers.call_args.kwargs["labels"]
        assert labels["egg.pipeline.id"] == "pipeline-x"
        assert labels["egg.slice.id"] == "slice-2"


class TestSliceHasPendingDecision:
    """``_slice_has_pending_decision`` — the helper that detects a
    BLOCKED slice without any pending HITL on the contract (case-4
    deserves an overseer alert).
    """

    def test_no_decisions_returns_false(self) -> None:
        from routes.pipelines import _slice_has_pending_decision

        assert _slice_has_pending_decision("slice-1", []) is False

    def test_unresolved_decision_returns_true(self) -> None:
        from routes.pipelines import _slice_has_pending_decision

        unresolved = MagicMock()
        unresolved.resolved = False
        assert _slice_has_pending_decision("slice-1", [unresolved]) is True

    def test_all_resolved_returns_false(self) -> None:
        from routes.pipelines import _slice_has_pending_decision

        resolved = MagicMock()
        resolved.resolved = True
        assert _slice_has_pending_decision("slice-1", [resolved, resolved]) is False


# ---------------------------------------------------------------------------
# HITL escalation helpers — Layer-C operator-pause persistence
#
# ``_escalate_layer_c_hitl`` writes the HITL ``Decision`` directly via
# ``load_contract(pipeline_id, worktree_repo_path)`` /
# ``save_contract(contract, worktree_repo_path)`` (no state-store
# indirection), so a corrupt / unclassifiable slice reliably pauses for the
# operator instead of silently failing open. The tests pin: the Decision is
# actually persisted to disk; the ``next_cq_id`` allocator skips legacy
# ``decision-N`` ids and allocates from the ``cq-N`` namespace; the
# caller-threaded ``current_phase`` is recorded (falling back to IMPLEMENT
# when None); and the signature does not accept a ``context_prefix`` kwarg
# (the routing discriminator lives in the ``question`` text).
# ---------------------------------------------------------------------------


import subprocess  # noqa: E402
import tempfile  # noqa: E402

from egg_contracts.loader import load_contract, save_contract  # noqa: E402
from egg_contracts.models import Decision, DecisionType  # noqa: E402


class TestEscalateLayerCHITLPersistence:
    """``_escalate_layer_c_hitl`` writes a real HITL Decision to the
    contract at ``worktree_repo_path``.

    Adversarial probes for the v2→v3 transition. The v2 helper used a
    ``_state_store_or_none()`` indirection that silently no-op'd via
    a swallowed ``TypeError``; v3 must actually persist the Decision
    so ``/sdlc`` reads it and the slice pauses for the operator.
    """

    PIPELINE_ID = "issue-2777-escalation"

    def _seed_contract(self, repo_root: Path, *, decisions=None) -> None:
        """Write a base contract to the temp ``repo_root`` so the
        helper's ``load_contract(pipeline_id, repo_root)`` succeeds.
        """
        contract = Contract(
            schemaVersion="1.2",
            issue=IssueInfo(number=2777, title="#2777", url=""),
            pipeline_id=self.PIPELINE_ID,
            current_phase=ContractPhase.IMPLEMENT,
            slices=[],
            decisions=decisions or [],
        )
        save_contract(contract, repo_root)

    def test_decision_persisted_to_contract_on_disk(self) -> None:
        """Adversarial probe — the v3 regression test for blocker 1 of
        reviewer_code v2 + the reviewer_security v2 blocker. The v2
        helper silently no-op'd (no Decision appended). After this
        test executes, the contract file at
        ``worktree_repo_path/.egg-state/contracts/<id>.json`` MUST
        contain an unresolved HITL Decision.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
                question="[#2777 slice-4 TASK-4-4 case 5] corrupt",
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert len(reloaded.decisions) == 1, (
                "v3 helper MUST persist a Decision — v2 silently no-op'd via "
                "the swallowed TypeError on get_state_store() (reviewer_code "
                "v2 blocker 1)"
            )
            d = reloaded.decisions[0]
            assert d.type == DecisionType.HITL
            assert d.resolved is False, (
                "newly-appended HITL Decision must be unresolved so /sdlc "
                "blocks the slice for the operator"
            )
            assert "TASK-4-4 case 5" in d.question
            assert len(d.options) == 3, (
                "Layer-C HITL escalation must offer the three canonical "
                "options (mark complete / restart / cancel)"
            )

    def test_decision_id_uses_next_cq_id_allocator(self) -> None:
        """Adversarial probe for reviewer_code v2 blocker 2 — the v2
        helper used ``f"decision-{len(decisions) + 1}"`` which collides
        with the pipeline-side ``decision-N`` namespace per the
        ``Decision.id`` docstring. v3 must use ``next_cq_id`` which
        allocates from the ``cq-N`` namespace and IGNORES legacy
        ``decision-N`` entries.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        # Seed the contract with TWO legacy ``decision-N`` entries that
        # the pipeline-side bridge mirror could have written. The v2
        # ``len(decisions)+1`` allocator would produce ``decision-3``,
        # colliding with the bridge namespace. v3's ``next_cq_id`` must
        # skip these and produce ``cq-1``.
        prior = [
            Decision(
                id="decision-1",
                question="legacy",
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
            ),
            Decision(
                id="decision-2",
                question="legacy",
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root, decisions=prior)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
                question="case-5",
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            new_decisions = [d for d in reloaded.decisions if d.id.startswith("cq-")]
            assert len(new_decisions) == 1, (
                f"v3 must allocate a single cq-N id; got {[d.id for d in reloaded.decisions]}"
            )
            assert new_decisions[0].id == "cq-1", (
                "v3 next_cq_id MUST skip legacy decision-N entries and "
                f"allocate cq-1, got {new_decisions[0].id}"
            )

    def test_next_cq_id_increments_when_cq_entries_already_exist(self) -> None:
        """Adversarial probe — if the contract already has ``cq-1``
        from an earlier escalation, the next call must allocate
        ``cq-2`` (NOT collide on ``cq-1`` and NOT silently overwrite
        the existing decision).
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        prior = [
            Decision(
                id="cq-1",
                question="earlier",
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root, decisions=prior)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
                question="case-5",
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            cq_ids = sorted(d.id for d in reloaded.decisions if d.id.startswith("cq-"))
            assert cq_ids == ["cq-1", "cq-2"], f"v3 must increment cq-N; got {cq_ids}"

    def test_current_phase_is_threaded_into_decision_record(self) -> None:
        """Adversarial probe for reviewer_code v2 blocker 3 — the v2
        helper hard-coded ``PipelinePhase.IMPLEMENT`` on the Decision
        even when Layer-C fires under a different live phase. v3 must
        use the ``current_phase`` parameter the caller threads through.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.PLAN,
                question="case-5",
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert reloaded.decisions[0].phase == ContractPhase.PLAN, (
                "v3 Decision.phase MUST reflect the live pipeline phase, "
                "not the hard-coded IMPLEMENT literal of v2"
            )

    def test_current_phase_none_falls_back_to_implement(self) -> None:
        """Adversarial probe — ``current_phase=None`` (defensive: the
        Layer-C caller may have no pipeline record loaded) must fall
        back to ``PipelinePhase.IMPLEMENT`` rather than write a None
        phase that breaks the Decision validator.
        """
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=None,
                question="case-5",
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert reloaded.decisions[0].phase == ContractPhase.IMPLEMENT, (
                "v3 defensive fallback for current_phase=None must produce "
                "IMPLEMENT, not raise / write None / drop the Decision"
            )

    def _resolve_only_decision(self, repo_root: Path) -> None:
        contract = load_contract(self.PIPELINE_ID, repo_root)
        assert len(contract.decisions) == 1
        contract.decisions[0].resolved = True
        contract.decisions[0].resolution = "Restart slice from scratch"
        save_contract(contract, repo_root)

    def test_carry_forward_default_adopts_the_resolved_question(self) -> None:
        """The #3392 converge-before-advance behaviour, unchanged.

        A refine/plan re-run that re-registers an already-answered
        question must adopt the resolution rather than re-surface it,
        or the loop never reaches a fixpoint. That is what the default
        ``carry_forward=True`` preserves; the gate escalations opt out
        of it (see the two tests below and the helper's docstring).
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            for _ in range(2):
                _escalate_layer_c_hitl(
                    pipeline_id=self.PIPELINE_ID,
                    slice_id="slice-1",
                    worktree_repo_path=repo_root,
                    current_phase=PipelineModelsPhase.IMPLEMENT,
                    question="same question",
                )
                self._resolve_only_decision(repo_root)
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert len(reloaded.decisions) == 1, (
                "carry_forward=True must adopt the resolved decision so the #3392 loop converges"
            )

    def test_carry_forward_false_re_opens_after_a_resolution(self) -> None:
        """The gate-escalation posture (PR #3628 review, blocking finding 1).

        A close-path gate red that recurs *after* the operator answered
        is a discrete physical event, not a re-derivation: the answer
        had no mechanical effect (nothing dispatches on the gate
        markers) and the world is still red. Adopting the prior
        resolution there is suppression, not idempotence — the phase
        goes FAILED with nothing on ``pending_decisions``.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
                question="same question",
                carry_forward=False,
            )
            self._resolve_only_decision(repo_root)
            _escalate_layer_c_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
                question="same question",
                carry_forward=False,
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert [d.resolved for d in reloaded.decisions] == [True, False], (
                "carry_forward=False must mint a fresh unresolved decision "
                "when the identical question recurs after a resolution"
            )

    def test_carry_forward_false_still_dedupes_the_open_question(self) -> None:
        """Opting out of carry-forward must not become one-per-retry.

        The open-question half of the #3427 guard is unconditional: an
        operator who has not answered yet sees exactly one decision no
        matter how many close retries fire.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            for _ in range(3):
                _escalate_layer_c_hitl(
                    pipeline_id=self.PIPELINE_ID,
                    slice_id="slice-1",
                    worktree_repo_path=repo_root,
                    current_phase=PipelineModelsPhase.IMPLEMENT,
                    question="same question",
                    carry_forward=False,
                )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert len(reloaded.decisions) == 1

    def test_every_gate_wrapper_opts_out_of_carry_forward(self) -> None:
        """Wiring check for *every* gate wrapper, discovered by name.

        Guards against a future wrapper being added without the opt-out
        (or one of the current two being edited), which would silently
        restore the suppression the behaviour tests above pin. Listing
        the two known wrappers explicitly would not do that — a third
        ``_escalate_*_gate_to_hitl`` would sail past — so this walks the
        barrel's namespace and drives whatever it finds, building each
        call's kwargs from the wrapper's own signature.
        """
        import inspect
        from unittest.mock import patch as _patch

        import routes.pipelines as _rp
        from models import PipelinePhase as PipelineModelsPhase

        wrappers = {
            name: fn
            for name, fn in vars(_rp).items()
            if name.startswith("_escalate_")
            and name.endswith("_gate_to_hitl")
            and inspect.isfunction(fn)
        }
        # Discovery sanity: a glob that matched nothing would make the
        # assertion below vacuously true. Subset, not equality, so a
        # third wrapper is exercised rather than rejected.
        assert {
            "_escalate_evidence_gate_to_hitl",
            "_escalate_green_gate_to_hitl",
        } <= set(wrappers), f"gate-wrapper discovery found only {sorted(wrappers)}"

        common: dict[str, object] = {
            "pipeline_id": self.PIPELINE_ID,
            "slice_id": "slice-1",
            "worktree_repo_path": Path("/tmp/worktree"),
            "current_phase": PipelineModelsPhase.IMPLEMENT,
        }
        driven = sorted(wrappers)
        with _patch("routes.pipelines._escalate_layer_c_hitl") as shared:
            for name in driven:
                params = inspect.signature(wrappers[name]).parameters
                kwargs = {k: v for k, v in common.items() if k in params}
                # Whatever failure-text parameter this wrapper names
                # (``failure`` / ``failure_headline`` / a future one).
                kwargs.update(
                    {
                        p.name: f"{name} failure text"
                        for p in params.values()
                        if p.name not in kwargs and p.default is inspect.Parameter.empty
                    }
                )
                wrappers[name](**kwargs)
        assert [c.kwargs["carry_forward"] for c in shared.call_args_list] == [False] * len(
            driven
        ), f"every gate wrapper must pass carry_forward=False; drove {driven}"

    def test_signature_no_longer_accepts_context_prefix(self) -> None:
        """Adversarial probe for the v3 signature surface — the v2
        ``context_prefix`` parameter was dropped (the routing
        discriminator is now embedded in ``question`` text). A test
        that PASSES context_prefix=... must raise ``TypeError``, not
        silently accept and ignore it (which would mask future
        callers that still try to thread it).
        """
        import inspect

        from routes.pipelines import _escalate_layer_c_hitl

        sig = inspect.signature(_escalate_layer_c_hitl)
        assert "context_prefix" not in sig.parameters, (
            f"v3 must drop context_prefix; it appears in the signature as {list(sig.parameters)}"
        )

    def test_load_contract_failure_is_swallowed(self) -> None:
        """Adversarial probe — when the contract file is missing
        (e.g. bootstrap ran before the contract was persisted), the
        helper must swallow the ``ContractNotFoundError`` and return
        cleanly (per the v3 ``except Exception as escalate_err: ...
        logger.warning`` arm). It MUST NOT bubble up and crash the
        Layer-C dispatch loop.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Do NOT seed a contract — load_contract will raise.
            _escalate_layer_c_hitl(
                pipeline_id="issue-2777-missing-contract",
                slice_id="slice-1",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
                question="case-5",
            )
            # No exception propagated → the test passes by virtue of
            # reaching here. Pin the assertion so an accidental change
            # to "raise" mode trips this regression.
            assert True


class TestEscalateLayerCDedupeAndDurability:
    """#3427 hardening for ``_escalate_layer_c_hitl``.

    Two guarantees:

    * **Idempotent across bootstrap re-runs** — the Layer-C question text
      is deterministic per (case, slice, pipeline), so every
      ``restart_phase`` re-derives the identical question. The helper must
      adopt an existing open duplicate and must NOT re-surface a question
      the operator already resolved (dedupe/carry-forward parity with
      ``register_open_question``, #3374/#3392).
    * **Durable at write time** — a newly appended Decision must trigger
      ``persist_contract_statefiles`` (commit+push to the work branch), so
      the phase-(re)start ``git reset --hard`` syncs cannot revert it and
      the next ``next_cq_id`` mint cannot reuse its id (#3427).
    """

    PIPELINE_ID = "issue-3427-layer-c"
    QUESTION = (
        "[#2777 slice-4 TASK-4-4 case 5] Slice slice-4 of pipeline "
        "issue-3427-layer-c has an impossible status enum value."
    )

    def _seed_contract(self, repo_root: Path, *, decisions=None) -> None:
        contract = Contract(
            schemaVersion="1.2",
            issue=IssueInfo(number=3427, title="#3427", url=""),
            pipeline_id=self.PIPELINE_ID,
            current_phase=ContractPhase.IMPLEMENT,
            slices=[],
            decisions=decisions or [],
        )
        save_contract(contract, repo_root)

    def _escalate(self, repo_root: Path, question: str | None = None) -> None:
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_layer_c_hitl

        _escalate_layer_c_hitl(
            pipeline_id=self.PIPELINE_ID,
            slice_id="slice-4",
            worktree_repo_path=repo_root,
            current_phase=PipelineModelsPhase.IMPLEMENT,
            question=question or self.QUESTION,
        )

    def test_open_duplicate_adopted_not_reminted(self) -> None:
        """A bootstrap re-run re-deriving an identical, still-open
        question must adopt the existing ``cq-N`` — not append a second
        copy the operator has to answer twice.
        """
        prior = [
            Decision(
                id="cq-1",
                question=self.QUESTION,
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root, decisions=prior)
            self._escalate(repo_root)
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert [d.id for d in reloaded.decisions] == ["cq-1"], (
                "identical open Layer-C question must be adopted, not re-minted"
            )

    def test_resolved_duplicate_not_reasked(self) -> None:
        """A question the operator already resolved must NOT be
        re-surfaced by a later bootstrap re-run (#3392 carry-forward);
        re-asking is exactly the operator pain from #3427.
        """
        prior = [
            Decision(
                id="cq-1",
                question=self.QUESTION,
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
                resolved=True,
                resolution="opt-1",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root, decisions=prior)
            self._escalate(repo_root)
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert len(reloaded.decisions) == 1
            assert reloaded.decisions[0].resolved is True, (
                "operator resolution must survive a Layer-C re-run untouched"
            )

    def test_distinct_question_still_mints_next_id(self) -> None:
        """Dedupe must not over-match: a different slice's Layer-C
        question is a distinct decision and gets the next ``cq-N``.
        """
        prior = [
            Decision(
                id="cq-1",
                question="[#2777 slice-4 TASK-4-4 case 5] Slice slice-1 is corrupt.",
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
                resolved=True,
                resolution="opt-1",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root, decisions=prior)
            self._escalate(repo_root)
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert sorted(d.id for d in reloaded.decisions) == ["cq-1", "cq-2"]

    def test_new_decision_triggers_durable_persist(self) -> None:
        """A newly appended Decision must be committed+pushed at write
        time via ``persist_contract_statefiles`` — otherwise the next
        worktree reset reverts it and its id gets re-minted (#3427).
        """
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root)
            with patch("routes.pipelines.persist_contract_statefiles") as persist_mock:
                self._escalate(repo_root)
            persist_mock.assert_called_once()
            args, _kwargs = persist_mock.call_args
            assert args[0] == self.PIPELINE_ID
            assert args[1] == repo_root
            assert "cq-1" in args[2]

    def test_dedupe_skip_does_not_persist(self) -> None:
        """An adopted duplicate writes nothing, so nothing to persist."""
        from unittest.mock import patch

        prior = [
            Decision(
                id="cq-1",
                question=self.QUESTION,
                type=DecisionType.HITL,
                phase=ContractPhase.IMPLEMENT,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_contract(repo_root, decisions=prior)
            with patch("routes.pipelines.persist_contract_statefiles") as persist_mock:
                self._escalate(repo_root)
            persist_mock.assert_not_called()


class TestPersistContractStatefiles:
    """#3427: direct coverage of ``persist_contract_statefiles``.

    Every *caller* of this helper mocks it (the mutate route, the resolve
    route, and ``_escalate_layer_c_hitl`` above all patch it out), so its
    own body — the commit+push wiring that makes a decision survive the
    phase-restart ``git reset --hard`` — is never exercised by those
    suites. A signature drift in ``_commit_statefiles_to_worktree`` or
    ``gateway.push_worktree_branch`` would slip through as a
    warning-logged no-op — the exact failure mode this fix exists to
    prevent. This runs the real helper against a temp git repo and
    asserts a commit lands and is handed to the gateway push.
    """

    PIPELINE_ID = "issue-3427-persist"

    def _git(self, repo_root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    def _init_repo(self, repo_root: Path) -> None:
        """A real git repo with an initial commit so HEAD resolves
        (``_commit_statefiles_to_worktree`` runs ``git read-tree HEAD``).
        """
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.email", "egg@localhost")
        self._git(repo_root, "config", "user.name", "egg")
        (repo_root / "README.md").write_text("seed\n")
        self._git(repo_root, "add", "README.md")
        self._git(repo_root, "commit", "-q", "-m", "init")

    def _make_pipeline(self):
        config = PipelineConfig(concurrent_execution=True, max_concurrent_agents=6)
        return Pipeline(
            id=self.PIPELINE_ID,
            issue_number=3427,
            repo="owner/repo",
            branch=f"egg/{self.PIPELINE_ID}/work",
            base_branch="main",
            # Explicit mode so ``_compute_gateway_mode`` returns without a
            # gateway round-trip.
            network_mode="public",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )

    def _seed_contract(self, repo_root: Path) -> None:
        contract = Contract(
            schemaVersion="1.2",
            issue=IssueInfo(number=3427, title="#3427", url=""),
            pipeline_id=self.PIPELINE_ID,
            current_phase=ContractPhase.IMPLEMENT,
            slices=[],
            decisions=[
                Decision(
                    id="cq-1",
                    question="persist me durably",
                    type=DecisionType.HITL,
                    phase=ContractPhase.IMPLEMENT,
                )
            ],
        )
        save_contract(contract, repo_root)

    def test_commits_and_hands_to_gateway_push(self) -> None:
        from unittest.mock import MagicMock, patch

        from routes.pipelines import persist_contract_statefiles

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._init_repo(repo_root)
            self._seed_contract(repo_root)
            pipeline = self._make_pipeline()

            spawner = MagicMock()
            head_before = self._git(repo_root, "rev-parse", "HEAD").stdout.strip()

            with patch("routes.pipelines._get_spawner", return_value=spawner):
                result = persist_contract_statefiles(
                    self.PIPELINE_ID,
                    repo_root,
                    "Persist contract decision mutation decisions.0 (#3427)",
                    pipeline=pipeline,
                )

            assert result is True
            # A real commit landed on the branch (the reset target now
            # contains the decision, not just the worktree file).
            head_after = self._git(repo_root, "rev-parse", "HEAD").stdout.strip()
            assert head_after != head_before, "expected a new commit for the contract write"
            subject = self._git(repo_root, "log", "-1", "--pretty=%s").stdout.strip()
            assert subject == "Persist contract decision mutation decisions.0 (#3427)"
            # The contract file is what got committed.
            committed = self._git(repo_root, "show", "--stat", "--pretty=", "HEAD").stdout
            assert ".egg-state/contracts/" in committed

            # And the commit was pushed to the pipeline's work branch.
            spawner.gateway.push_worktree_branch.assert_called_once()
            push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
            assert push_kwargs["pipeline_id"] == self.PIPELINE_ID
            assert push_kwargs["branch"] == pipeline.branch
            assert push_kwargs["base_branch"] == "main"

    def test_no_branch_does_not_push_and_returns_false(self) -> None:
        """A committed decision with no work branch to push to must not
        claim durability — it returns ``False`` and skips the push (the
        commit is local-only and a reset can still discard it).
        """
        from unittest.mock import MagicMock, patch

        from routes.pipelines import persist_contract_statefiles

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._init_repo(repo_root)
            self._seed_contract(repo_root)
            pipeline = self._make_pipeline()
            pipeline.branch = None

            spawner = MagicMock()
            with patch("routes.pipelines._get_spawner", return_value=spawner):
                result = persist_contract_statefiles(
                    self.PIPELINE_ID,
                    repo_root,
                    "Persist without a branch (#3427)",
                    pipeline=pipeline,
                )

            assert result is False
            spawner.gateway.push_worktree_branch.assert_not_called()


class TestEscalateCorruptSliceToHITL:
    """``_escalate_corrupt_slice_to_hitl`` — case-5 wrapper that builds
    the question text with the ``[#2777 slice-4 TASK-4-4 case 5]``
    routing prefix.
    """

    PIPELINE_ID = "issue-2777-escalation-c5"

    def test_question_includes_case_5_routing_prefix(self) -> None:
        """Pin the literal routing prefix so a future
        ``routes/decisions.py`` dispatch handler can scan on the exact
        substring without a contract-schema change.
        """
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_corrupt_slice_to_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            contract = Contract(
                schemaVersion="1.2",
                issue=IssueInfo(number=2777, title="#2777", url=""),
                pipeline_id=self.PIPELINE_ID,
                current_phase=ContractPhase.IMPLEMENT,
                slices=[],
            )
            save_contract(contract, repo_root)
            _escalate_corrupt_slice_to_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-7",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert len(reloaded.decisions) == 1
            assert "[#2777 slice-4 TASK-4-4 case 5]" in reloaded.decisions[0].question
            assert "slice-7" in reloaded.decisions[0].question, (
                "question must name the corrupt slice id"
            )


class TestGatewayClientMergeBaseShapeCheck:
    """``GatewayClient.merge_base`` — adversarial probes for the v3
    strict 40-char hex SHA shape check (replacing the v2 lax
    ``len >= 7 + all-hex`` check). The strict regex MUST reject:

    * Truncated SHAs (e.g. 7-char short SHA) — git merge-base ALWAYS
      returns the full 40-char SHA on success, so a short SHA is
      stdout noise / corruption and must NOT propagate downstream.
    * Hex strings of the wrong length (39, 41).
    * Non-hex strings (e.g. error text bleeding through stdout).

    All tests below mock ``register_session`` / ``delete_session`` (v4
    auth-bootstrap; ``/api/v1/git/execute`` is ``@require_session_auth``
    and merge_base now self-registers a synthetic session when no
    ``bearer_token`` is supplied, per the v4 fix for reviewer_code v3's
    silent-no-op blocker). Direct shape-check coverage is preserved by
    plumbing the synthetic token through ``_make_request``.
    """

    def _client(self):
        """Build a GatewayClient with the inner ``_make_request`` mocked
        out so we drive the stdout shape directly.
        """
        from gateway_client import GatewayClient

        c = GatewayClient(
            gateway_host="gateway",
            gateway_port=8080,
            launcher_secret="dummy",
        )
        return c

    def _run_with_stdout(self, stdout: str, *, bearer_token: str | None = None) -> str | None:
        """Drive ``merge_base`` against a stubbed stdout payload.

        Mocks (v4 surface): ``register_session`` returns a synthetic
        session with token ``synthetic-token``; ``delete_session``
        is a no-op; ``_make_request`` returns ``{"data":
        {"stdout": stdout}}`` for the ``/api/v1/git/execute`` call.
        """
        from unittest.mock import MagicMock, patch

        c = self._client()
        # Stub register_session to avoid hitting the real network.
        session = MagicMock()
        session.session_token = "synthetic-token"
        with (
            patch.object(c, "register_session", return_value=session),
            patch.object(c, "delete_session"),
            patch.object(
                c,
                "_make_request",
                return_value={"data": {"stdout": stdout}},
            ),
        ):
            return c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token=bearer_token,
            )

    def test_full_40_char_lowercase_hex_sha_returned(self) -> None:
        """The canonical happy-path SHA passes the regex."""
        sha = "0" * 40
        assert self._run_with_stdout(sha + "\n") == sha

    def test_truncated_7_char_sha_rejected(self) -> None:
        """Adversarial probe — the v2 lax check would have accepted a
        7-char short SHA (``len >= 7 + all-hex``). v3 must reject it
        as "no fork point" rather than pass a truncated value
        downstream (which would break ``create_slice_integration_branch``
        which expects branch-name strings, not SHA prefixes).
        """
        assert self._run_with_stdout("abc1234\n") is None, (
            "v3 strict regex must reject 7-char truncated SHAs that v2 accepted"
        )

    def test_uppercase_hex_rejected(self) -> None:
        """git outputs lowercase hex; uppercase is gateway noise."""
        assert self._run_with_stdout("A" * 40 + "\n") is None

    def test_non_hex_garbage_rejected(self) -> None:
        """Adversarial probe — error text bleeding into stdout must
        be rejected, not parsed as a SHA.
        """
        assert self._run_with_stdout("not-a-sha-at-all\n") is None
        assert self._run_with_stdout("fatal: bad object\n") is None

    def test_extra_newline_noise_stripped_and_validated(self) -> None:
        """The parser takes the first line and strips whitespace. A
        valid SHA followed by trailing log noise is still accepted
        (only the first line matters).
        """
        sha = "1" * 40
        assert self._run_with_stdout(f"{sha}\nsome extra log\n") == sha

    def test_empty_stdout_returns_none(self) -> None:
        """Empty stdout (e.g. no shared ancestor exit) → None."""
        assert self._run_with_stdout("") is None
        assert self._run_with_stdout("\n") is None


class TestGatewayClientMergeBaseSessionAuth:
    """``GatewayClient.merge_base`` — adversarial probes for the v4
    auth-bootstrap fix (reviewer_code v3 blocker).

    The v3 helper called ``/api/v1/git/execute`` without registering a
    session, but the endpoint is ``@require_session_auth`` so the
    request 401'd, surfaced as a GatewayError with returncode=None,
    and the catch-all swallow returned None → resolver mis-routed
    the slice onto pipeline_branch. v4 mirrors the
    fetch_branch / ls_remote_branch / get_remote_branch_sha pattern:
    register_session(synthetic=True) when ``bearer_token`` is None,
    plumb the token through ``_make_request``, delete_session in a
    finally.

    Coverage handed off to the tester role by the coder in the
    slice-4 v4 commit message ("Tester ownership for the
    TestGatewayClientMergeBase coverage gap").
    """

    def _client(self):
        from gateway_client import GatewayClient

        return GatewayClient(
            gateway_host="gateway",
            gateway_port=8080,
            launcher_secret="dummy",
        )

    def test_no_bearer_token_self_bootstraps_synthetic_session(self) -> None:
        """v4 blocker fix — ``bearer_token=None`` MUST trigger a
        ``register_session(synthetic=True, ...)`` call before the
        ``/api/v1/git/execute`` request. Without this self-bootstrap
        the v3 call 401'd silently and the merge-base fallback
        defeated TASK-4-3 for legacy slices.
        """
        from unittest.mock import MagicMock, patch

        c = self._client()
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        sha = "0" * 40
        with (
            patch.object(c, "register_session", return_value=session) as mock_reg,
            patch.object(c, "delete_session"),
            patch.object(
                c,
                "_make_request",
                return_value={"data": {"stdout": sha + "\n"}},
            ),
        ):
            c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token=None,
            )
        mock_reg.assert_called_once()
        # ``synthetic=True`` MUST be set: the launcher-secret-only
        # auth path on the gateway is gated on this flag (mirroring
        # fetch_branch / ls_remote_branch).
        assert mock_reg.call_args.kwargs.get("synthetic") is True, (
            "v4 merge_base must register a SYNTHETIC session — the gateway "
            "rejects non-synthetic temporary sessions for /git/execute"
        )

    def test_synthetic_token_plumbed_to_make_request(self) -> None:
        """Adversarial probe — the token from ``register_session``
        MUST be passed as ``bearer_token`` on the inner
        ``_make_request`` call. The v3 bug was that bearer_token was
        always None at the call site; a partial v4 fix that registered
        the session but didn't thread the token would still 401.
        """
        from unittest.mock import MagicMock, patch

        c = self._client()
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        sha = "0" * 40
        with (
            patch.object(c, "register_session", return_value=session),
            patch.object(c, "delete_session"),
            patch.object(
                c,
                "_make_request",
                return_value={"data": {"stdout": sha + "\n"}},
            ) as mock_req,
        ):
            c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token=None,
            )
        assert mock_req.call_args.kwargs.get("bearer_token") == "synthetic-token-xyz", (
            "v4 must plumb the synthetic session's token through to "
            "_make_request, not pass the original bearer_token=None"
        )

    def test_caller_supplied_bearer_token_skips_register_session(self) -> None:
        """Adversarial probe — when the caller already has a session
        (e.g. an ambient slice / pipeline session) and passes it as
        ``bearer_token``, the helper MUST NOT register a redundant
        synthetic session. A v4 implementation that always registered
        would burn a register/delete round-trip per merge_base call.
        """
        from unittest.mock import patch

        c = self._client()
        sha = "0" * 40
        with (
            patch.object(c, "register_session") as mock_reg,
            patch.object(c, "delete_session") as mock_del,
            patch.object(
                c,
                "_make_request",
                return_value={"data": {"stdout": sha + "\n"}},
            ) as mock_req,
        ):
            c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token="caller-token-abc",
            )
        mock_reg.assert_not_called()
        mock_del.assert_not_called()
        # The caller's token must be used verbatim.
        assert mock_req.call_args.kwargs.get("bearer_token") == "caller-token-abc"

    def test_synthetic_session_deleted_in_finally_on_success(self) -> None:
        """Adversarial probe — when we self-register, we MUST tear
        down the session in a finally to avoid leaking gateway state.
        """
        from unittest.mock import MagicMock, patch

        c = self._client()
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        sha = "0" * 40
        with (
            patch.object(c, "register_session", return_value=session),
            patch.object(c, "delete_session") as mock_del,
            patch.object(
                c,
                "_make_request",
                return_value={"data": {"stdout": sha + "\n"}},
            ),
        ):
            c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token=None,
            )
        mock_del.assert_called_once()
        assert mock_del.call_args.args[0] == "synthetic-token-xyz", (
            "v4 must delete the synthetic session it registered, not leak gateway state"
        )

    def test_synthetic_session_deleted_in_finally_on_make_request_failure(self) -> None:
        """Adversarial probe — on ``_make_request`` failure (e.g. the
        merge_base subprocess exits non-zero → GatewayError), the
        finally arm MUST still tear down the synthetic session.
        Without this the gateway leaks one session per failed call.
        """
        from unittest.mock import MagicMock, patch

        from gateway_client import GatewayError

        c = self._client()
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        with (
            patch.object(c, "register_session", return_value=session),
            patch.object(c, "delete_session") as mock_del,
            patch.object(
                c,
                "_make_request",
                side_effect=GatewayError("merge-base exited 1", details={"returncode": 1}),
            ),
        ):
            result = c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token=None,
            )
        assert result is None, "returncode=1 (no shared ancestor) → None return is the v4 contract"
        mock_del.assert_called_once_with("synthetic-token-xyz")

    def test_mode_parameter_threaded_into_register_session(self) -> None:
        """v4 nit — the new ``mode: Literal['public', 'private']``
        parameter must be threaded through to ``register_session``.
        Default is ``"public"`` (matching fetch_branch's default); a
        private-mode pipeline must propagate to the synthetic session
        so the gateway grants the same allowlist.
        """
        from unittest.mock import MagicMock, patch

        c = self._client()
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        sha = "0" * 40
        with (
            patch.object(c, "register_session", return_value=session) as mock_reg,
            patch.object(c, "delete_session"),
            patch.object(
                c,
                "_make_request",
                return_value={"data": {"stdout": sha + "\n"}},
            ),
        ):
            c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="refs/remotes/origin/a",
                ref_b="refs/remotes/origin/b",
                bearer_token=None,
                mode="private",
            )
        assert mock_reg.call_args.kwargs.get("mode") == "private", (
            "v4 must thread the mode parameter through to register_session"
        )

    def test_default_mode_is_public(self) -> None:
        """The ``mode`` parameter defaults to ``"public"`` matching
        the rest of the auth-bootstrapped gateway surface
        (fetch_branch / ls_remote_branch / get_remote_branch_sha).
        """
        import inspect

        from gateway_client import GatewayClient

        sig = inspect.signature(GatewayClient.merge_base)
        mode_param = sig.parameters.get("mode")
        assert mode_param is not None, (
            "v4 merge_base must accept a mode parameter for private-pipeline propagation"
        )
        assert mode_param.default == "public", (
            f"v4 default mode must be 'public'; got {mode_param.default!r}"
        )

    def test_empty_ref_short_circuits_before_register_session(self) -> None:
        """Adversarial probe — the early ``if not ref_a or not ref_b``
        short-circuit MUST run BEFORE ``register_session``. Otherwise
        a caller passing an empty ref burns a register/delete
        round-trip per call.
        """
        from unittest.mock import patch

        c = self._client()
        with (
            patch.object(c, "register_session") as mock_reg,
            patch.object(c, "_make_request"),
        ):
            result = c.merge_base(
                pipeline_id="issue-2777-mb",
                repo_path="/tmp/x",
                ref_a="",
                ref_b="refs/remotes/origin/b",
            )
        assert result is None
        mock_reg.assert_not_called()


class TestEscalateBlockedSliceToHITL:
    """``_escalate_blocked_slice_to_hitl`` — case-4 wrapper. Question
    text embeds the ``[#2777 slice-4 TASK-4-4 case 4]`` prefix AND the
    caller-supplied ``reason`` so the operator sees why the BLOCKED
    slice had no pending HITL.
    """

    PIPELINE_ID = "issue-2777-escalation-c4"

    def test_question_includes_case_4_prefix_and_reason(self) -> None:
        from models import PipelinePhase as PipelineModelsPhase
        from routes.pipelines import _escalate_blocked_slice_to_hitl

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            contract = Contract(
                schemaVersion="1.2",
                issue=IssueInfo(number=2777, title="#2777", url=""),
                pipeline_id=self.PIPELINE_ID,
                current_phase=ContractPhase.IMPLEMENT,
                slices=[],
            )
            save_contract(contract, repo_root)
            _escalate_blocked_slice_to_hitl(
                pipeline_id=self.PIPELINE_ID,
                slice_id="slice-9",
                reason="no pending HITL decision found on contract",
                worktree_repo_path=repo_root,
                current_phase=PipelineModelsPhase.IMPLEMENT,
            )
            reloaded = load_contract(self.PIPELINE_ID, repo_root)
            assert len(reloaded.decisions) == 1
            q = reloaded.decisions[0].question
            assert "[#2777 slice-4 TASK-4-4 case 4]" in q
            assert "slice-9" in q
            assert "no pending HITL decision found on contract" in q, (
                "case-4 wrapper must embed the caller's reason verbatim "
                "so the operator sees why the BLOCKED slice had no HITL"
            )


# ---------------------------------------------------------------------------
# Per-slice consensus tracker reconstruction (#2409)
# ---------------------------------------------------------------------------


class TestCrossSliceTrackerIsolation:
    """Cross-slice tracker isolation (#2409) — two concurrent slices'
    messages live under the same bare ``pipeline_id`` in the message
    store, tagged per-slice via ``metadata['slice_id']``. After
    orchestrator restart, the reconstructed slice-N tracker MUST NOT
    contain sibling slices' messages.

    The metadata-based filter is the enforcement surface (see
    ``peer_consensus.py:~2035-2042``: ``_message_slice_id(m) ==
    slice_id``).
    """

    PIPELINE_ID = "issue-2777-ac16"

    def setup_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1", "slice-2"])

    def teardown_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1", "slice-2"])

    def test_slice_2_reconstruction_excludes_slice_1_messages(self) -> None:
        """Headline test bar: slice-1 has reached consensus
        (PROPOSE + CONFIRMED messages on the bus); slice-2 is
        mid-work (PROPOSE only). After orchestrator restart, the
        reconstructed slice-2 tracker MUST report ``is_complete=False``
        — slice-1's CONFIRMs MUST NOT bleed into the slice-2 tracker.
        """
        graph = get_review_graph_for_phase("implement", repo="owner/repo")

        slice_1_msgs: list[Message] = []
        for role in graph.all_roles():
            if role in {"coder", "tester", "documenter"}:
                slice_1_msgs.append(
                    _propose_message(self.PIPELINE_ID, from_role=role, slice_id="slice-1")
                )
        for role in graph.all_roles():
            slice_1_msgs.append(
                _confirmed_message(self.PIPELINE_ID, from_role=role, slice_id="slice-1")
            )

        slice_2_msgs = [_propose_message(self.PIPELINE_ID, from_role="coder", slice_id="slice-2")]

        fake_store = MagicMock()
        fake_store.get_messages.return_value = slice_1_msgs + slice_2_msgs

        slice_2_tracker = reconstruct_tracker_from_messages(
            self.PIPELINE_ID,
            graph,
            message_store=fake_store,
            slice_id="slice-2",
            phase="implement",
        )

        assert slice_2_tracker is not None, (
            "slice-2 tracker reconstruction returned None despite a slice-2 PROPOSE on the bus"
        )
        eval_state = slice_2_tracker.evaluate()
        assert eval_state.get("is_complete") is not True, (
            "slice-2 tracker incorrectly reports complete consensus — "
            "cross-slice isolation regression"
        )

    def test_slice_1_and_slice_2_trackers_are_distinct(self) -> None:
        """The reconstructed slice-1 and slice-2 trackers are
        registered under DISTINCT keys
        (``{pipeline_id}/slice-1`` vs ``{pipeline_id}/slice-2``).
        A future refactor that collapsed the key namespace would
        silently merge slices.
        """
        graph = get_review_graph_for_phase("implement", repo="owner/repo")

        msgs = [
            _propose_message(self.PIPELINE_ID, from_role="coder", slice_id="slice-1"),
            _propose_message(self.PIPELINE_ID, from_role="coder", slice_id="slice-2"),
        ]
        fake_store = MagicMock()
        fake_store.get_messages.return_value = msgs

        t1 = reconstruct_tracker_from_messages(
            self.PIPELINE_ID,
            graph,
            message_store=fake_store,
            slice_id="slice-1",
            phase="implement",
        )
        t2 = reconstruct_tracker_from_messages(
            self.PIPELINE_ID,
            graph,
            message_store=fake_store,
            slice_id="slice-2",
            phase="implement",
        )

        assert t1 is not None and t2 is not None
        assert t1 is not t2, "slice-1 and slice-2 trackers must be distinct"
        assert get_peer_consensus_tracker(self.PIPELINE_ID, "slice-1") is t1
        assert get_peer_consensus_tracker(self.PIPELINE_ID, "slice-2") is t2

    def test_pipeline_level_reconstruction_excludes_slice_messages(self) -> None:
        """Symmetric: the *pipeline-level* (``slice_id=None``)
        reconstruction MUST NOT pick up slice-tagged messages either —
        those belong only to their per-slice trackers. Regression
        for #2761.
        """
        graph = get_review_graph_for_phase("implement", repo="owner/repo")

        msgs = [
            _propose_message(self.PIPELINE_ID, from_role="coder", slice_id="slice-1"),
        ]
        fake_store = MagicMock()
        fake_store.get_messages.return_value = msgs

        pipeline_tracker = reconstruct_tracker_from_messages(
            self.PIPELINE_ID,
            graph,
            message_store=fake_store,
            slice_id=None,
            phase="implement",
        )
        assert pipeline_tracker is None, (
            "pipeline-level reconstruction silently absorbed a slice-tagged "
            "message — #2761 isolation regression"
        )


class TestStartupReconciliationEnumeratesContractSlices:
    """``startup_reconciliation._enumerate_contract_slices`` returns
    every slice ID on the contract so the reconstruction loop can
    iterate them (slice-4 TASK-4-5).
    """

    def test_returns_slice_ids_when_contract_has_slices(self) -> None:
        from unittest.mock import patch

        from startup_reconciliation import _enumerate_contract_slices

        contract = _make_contract(
            slices=[
                _make_slice("slice-1", task_idx=1),
                _make_slice("slice-2", task_idx=2),
            ]
        )
        store = MagicMock()
        store.repo_path = Path("/repo")
        pipeline = _make_pipeline("issue-2777-test-enum")

        with patch("egg_contracts.loader.load_contract", return_value=contract):
            ids = _enumerate_contract_slices(pipeline, store)
        assert ids == ["slice-1", "slice-2"]

    def test_returns_empty_when_contract_load_fails(self) -> None:
        from unittest.mock import patch

        from startup_reconciliation import _enumerate_contract_slices

        store = MagicMock()
        store.repo_path = Path("/repo")
        pipeline = _make_pipeline("issue-2777-test-enum")

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=RuntimeError("contract missing"),
        ):
            ids = _enumerate_contract_slices(pipeline, store)
        assert ids == []

    def test_returns_empty_when_store_has_no_repo_path(self) -> None:
        from startup_reconciliation import _enumerate_contract_slices

        store = MagicMock()
        store.repo_path = None
        pipeline = _make_pipeline("issue-2777-test-enum")
        assert _enumerate_contract_slices(pipeline, store) == []


class TestHandleConsensusConfirmedSliceScopedReconstruction:
    """``handle_consensus_confirmed_signal`` no longer skips
    reconstruction when ``slice_id`` is supplied (#2409). The fix
    removes the ``if slice_id is None`` gate around the reconstruction
    call — the metadata filter is the canonical scope mechanism.
    """

    PIPELINE_ID = "issue-2777-confirmed-slice"

    def setup_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1"])

    def teardown_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1"])

    def test_handler_calls_reconstruct_with_slice_id_when_supplied(self) -> None:
        """Slice-4 TASK-4-5 contract: when no in-memory tracker
        exists for the per-slice key AND a ``slice_id`` is supplied
        on the signal, the handler invokes
        ``reconstruct_tracker_from_messages`` with that ``slice_id``.
        Pre-slice-4 the handler skipped reconstruction entirely
        (``if slice_id is None`` gate around the call).

        Call the handler directly with a Flask app context — the
        ``/<pipeline_id>/signal`` route is exercised in
        ``orchestrator/tests/test_signals*.py``; here we want a
        focused probe of the reconstruction call shape.
        """
        from unittest.mock import patch

        from flask import Flask

        reconstruct_calls: list[dict] = []

        def _fake_reconstruct(pipeline_id, graph, **kwargs):
            reconstruct_calls.append({"pipeline_id": pipeline_id, **kwargs})
            return None  # mimic empty message bus → handler falls through to 404

        app = Flask(__name__)
        # The handler reads ``request.json`` indirectly via ``data``
        # the route layer passes in, so we synthesise the same call
        # shape but go through the handler directly.

        with (
            app.test_request_context(
                f"/api/v1/pipelines/{self.PIPELINE_ID}/signal",
                json={"agent_role": "coder", "slice_id": "slice-1"},
            ),
            patch("routes.signals.get_state_store") as mock_store_fn,
            patch(
                "peer_consensus.reconstruct_tracker_from_messages",
                side_effect=_fake_reconstruct,
            ),
        ):
            mock_pipeline = MagicMock()
            mock_pipeline.current_phase.value = "implement"
            mock_pipeline.repo = "owner/repo"
            store = MagicMock()
            store.load_pipeline.return_value = mock_pipeline
            mock_store_fn.return_value = store

            from routes.signals import handle_consensus_confirmed_signal

            response, status = handle_consensus_confirmed_signal(
                self.PIPELINE_ID,
                {"agent_role": "coder", "slice_id": "slice-1"},
                Path("/repo"),
            )
            del response, status  # we care about the reconstruct call shape

        slice_calls = [c for c in reconstruct_calls if c.get("slice_id") == "slice-1"]
        assert slice_calls, (
            f"handle_consensus_confirmed_signal did not invoke reconstruct "
            f"with slice_id='slice-1'; calls: {reconstruct_calls}"
        )
