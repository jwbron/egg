"""Slice/phase restart hardening tests (#2777 slice-4, TASK-4-6).

Covers the five sub-tasks of slice-4 (#2777):

* **TASK-4-1** — Slice-aware ``restart_phase`` consensus-clear: in
  addition to the pipeline-level ``tracker.clear()`` at
  ``orchestrator/routes/pipelines.py:3270-3289``, the handler now
  loads the contract, iterates ``contract.slices`` and calls
  ``get_peer_consensus_tracker(pipeline_id, slice_id=s.id).clear()``
  for each slice. Mirrors the slice-aware ``restart_agent`` path at
  ``pipelines.py:~2859``.

* **TASK-4-2** — Eager-persist ``parent_branch_at_creation`` + IN_PROGRESS
  flip: the existing parent-branch persist site at ``pipelines.py:~15703``
  now also flips ``SliceStatus.PENDING → IN_PROGRESS`` in the SAME
  contract-locked write. A crash between the status flip and the branch
  creation cannot leave the field empty (cq-9 / crash recovery).
  Idempotent on re-entry: only PENDING is flipped; COMPLETE / BLOCKED
  / IN_PROGRESS are left untouched.

* **TASK-4-3** — Merge-base fallback in
  ``_resolve_slice_base_branch``: an optional
  ``merge_base_lookup(integration_ref, derived_parent_ref) -> str | None``
  callback validates the legacy ancestor of orphaned non-root slices.
  A non-None SHA confirms a real fork point → fall through to the
  dependency-derived parent. A ``None`` return → no fork point → fall
  back to ``pipeline_branch`` (safe root-stack target for a slice
  that was never created OR whose parent has been deleted). Probe
  failure raises inside the callback and is caught by the resolver,
  defaulting to the derived-parent path (conservative: never silently
  swap on a flaky gateway).

* **TASK-4-4** — Bootstrap reconciliation 5-way classification.
  Module-level ``_classify_non_complete_slice`` returns one of five
  labels for every non-COMPLETE slice:

  - ``"fresh"`` — case (1) IN_PROGRESS/PENDING + no commits on origin
    → no Layer-C action; scheduler re-yields READY.
  - ``"resume"`` — case (2) IN_PROGRESS + commits + no consensus →
    ``scheduler.mark_spawned``, no respawn.
  - ``"consensus_complete"`` — case (3) IN_PROGRESS + commits +
    consensus REACHED → mark COMPLETE so the slice-PR opener fires.
  - ``"blocked"`` — case (4) BLOCKED → preserve status; caller
    escalates via ``_escalate_blocked_slice_to_hitl`` (writes a
    new HITL ``Decision`` to the contract) when no pending decision
    is found.
  - ``"corrupt"`` — case (5) impossible status enum or contradictory
    state combination → caller escalates via
    ``_escalate_corrupt_slice_to_hitl`` (writes a new HITL
    ``Decision`` to the contract).

* **TASK-4-5** — Per-slice consensus tracker reconstruction
  (#2409): ``startup_reconciliation.py`` iterates ``contract.slices``
  via ``_enumerate_contract_slices`` and calls
  ``reconstruct_tracker_from_messages(pipeline_id, graph,
  slice_id=s.id)`` for each slice in addition to the existing
  pipeline-level call. ``handle_consensus_confirmed_signal`` in
  ``orchestrator/routes/signals.py`` no longer skips reconstruction
  when ``slice_id`` is supplied — the metadata filter at
  ``message_store.py:407-418`` (#2725) is the canonical scope
  mechanism.

Adversarial probes baked into this file (each was on my "what could
the coder have missed?" list):

* Slice-aware ``restart_phase`` calls ``get_peer_consensus_tracker``
  with the per-slice key shape, not a bare inline format that drifts
  from ``_tracker_key``.
* Empty ``contract.slices`` (non-sliced pipeline) ⇒ still clears the
  pipeline-level tracker; the per-slice iteration no-ops cleanly.
* Contract load failure under ``restart_phase`` falls back to the
  pre-slice-4 pipeline-level-only behaviour rather than blocking the
  restart.
* TASK-4-2 PENDING → IN_PROGRESS flip is idempotent: COMPLETE /
  BLOCKED / IN_PROGRESS slices keep their status across a re-entry.
* TASK-4-3 fallback only fires when ``merge_base_lookup`` explicitly
  returns ``None``; a probe exception falls through to the
  derived-parent path (never silently swaps to ``pipeline_branch``
  on a flaky gateway).
* TASK-4-4 case 5: PENDING + commits-on-origin is impossible (TASK-4-2
  flips before commits could land) — the classifier must return
  ``"corrupt"`` so the operator is woken instead of the scheduler
  re-yielding READY.
* AC-16: two slices reconstruct via the slice-id filter and the
  slice-2 tracker has NO slice-1 ack/propose records.
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

    Used by AC-16 reconstruction tests so the replay path actually
    populates the tracker's proposal slot rather than skipping the
    message as invalid.
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
# TASK-4-1: Slice-aware restart_phase consensus-clear
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
                {"summary": "s", "artifacts": ["a.py"], "commit_sha": f"sha{sid}"},
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
# TASK-4-2: Eager-persist parent_branch_at_creation + PENDING → IN_PROGRESS flip
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
# TASK-4-3: ``branch_has_origin_commits`` fallback in _resolve_slice_base_branch
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
        probe = MagicMock(name="merge_base_lookup")
        result = _resolve_slice_base_branch(
            contract,
            "slice-1",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            merge_base_lookup=probe,
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


class TestResolveSliceBaseBranchMergeBaseLookupFallback:
    """The new ``merge_base_lookup`` arm: a non-root slice with no
    recorded parent invokes ``merge_base(integration_ref,
    derived_parent_ref)`` to validate the legacy ancestor.

    * Non-None SHA → real fork point → fall through to the
      dependency-derived parent (legacy-correct stack target).
    * ``None`` return → no fork point → fall back to
      ``pipeline_branch`` (slice was never created OR its parent has
      been deleted).
    * Probe raises → conservative default ("has fork point") → fall
      through to the derived parent so a flaky gateway never silently
      swaps the parent.
    """

    def test_no_merge_base_falls_back_to_pipeline_branch(self) -> None:
        """Headline TASK-4-3 case: probe returns ``None`` (no fork
        point) ⇒ resolver returns ``pipeline_branch`` instead of the
        dependency-derived parent."""
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
        probe = MagicMock(name="merge_base_lookup", return_value=None)
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            merge_base_lookup=probe,
        )
        # The probe is called with the slice's integration ref AND the
        # derived parent ref, both as ``refs/remotes/origin/<name>``.
        probe.assert_called_with(
            "refs/remotes/origin/egg/issue-2777-slice-4/slice-2",
            "refs/remotes/origin/egg/issue-2777-slice-4/slice-1",
        )
        assert result == "egg/issue-2777-slice-4/work", (
            "non-root slice with no merge-base against derived parent "
            "must fall back to pipeline_branch (slice-4 TASK-4-3)"
        )

    def test_merge_base_resolved_keeps_derived_parent(self) -> None:
        """Probe returns a SHA ⇒ derived parent is preserved. The
        slice has a real fork point; the dependency-derived parent
        is the legacy-correct stack target."""
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
        probe = MagicMock(return_value="deadbeefdeadbeef")
        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            merge_base_lookup=probe,
        )
        assert result == "egg/issue-2777-slice-4/slice-1"

    def test_probe_failure_falls_through_to_derived_parent(self) -> None:
        """Adversarial probe: a flaky gateway raises mid-probe. The
        resolver MUST fall through to the derived-parent path rather
        than silently swap to ``pipeline_branch`` (which would re-stack
        a real slice onto the wrong base).
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

        def _flaky(_ref_a: str, _ref_b: str) -> str | None:
            raise RuntimeError("gateway down")

        result = _resolve_slice_base_branch(
            contract,
            "slice-2",
            pipeline_id="issue-2777-slice-4",
            pipeline_branch="egg/issue-2777-slice-4/work",
            merge_base_lookup=_flaky,
        )
        # Conservative default ("has fork point") preserves the derived
        # parent — never silently swap on a flaky probe.
        assert result == "egg/issue-2777-slice-4/slice-1"


# ---------------------------------------------------------------------------
# TASK-4-4: Bootstrap reconciliation 5-way classification
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
        reached → ``"resume"``: caller marks the scheduler
        ``mark_spawned`` so the run loop does NOT respawn."""
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
        ``"resume"`` so we don't silently mark-spawn a slice whose
        true origin state is unknown.
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
# TASK-4-5: Per-slice consensus tracker reconstruction (#2409 / AC-16)
# ---------------------------------------------------------------------------


class TestAC16CrossSliceTrackerIsolation:
    """**AC-16 closure (#2409)** — two concurrent slices' messages live
    under the same bare ``pipeline_id`` in the message store, tagged
    per-slice via ``metadata['slice_id']``. After orchestrator restart,
    the reconstructed slice-N tracker MUST NOT contain sibling slices'
    messages.

    The metadata-based filter is the AC-16 enforcement surface (see
    ``peer_consensus.py:~2035-2042``: ``_message_slice_id(m) ==
    slice_id``).
    """

    PIPELINE_ID = "issue-2777-ac16"

    def setup_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1", "slice-2"])

    def teardown_method(self) -> None:
        _purge_tracker_keys(self.PIPELINE_ID, [None, "slice-1", "slice-2"])

    def test_slice_2_reconstruction_excludes_slice_1_messages(self) -> None:
        """Headline AC-16 test bar: slice-1 has reached consensus
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
            "AC-16 cross-slice isolation regression"
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
    reconstruction when ``slice_id`` is supplied (architect v2
    AC-16). The fix removes the ``if slice_id is None`` gate around
    the reconstruction call — the metadata filter is the canonical
    scope mechanism.
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
