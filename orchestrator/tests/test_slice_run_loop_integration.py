"""Integration tests for the implement-phase slice run loop (#2137).

Covers the wire-up code that connects the previously library-only
slice DAG building blocks (``SliceScheduler``, ``stacked_pr_reconciler``,
``GatewayClient.create_slice_pr`` / ``rebase_onto``) to the
orchestrator's implement-phase run loop:

* ``_start_stacked_pr_reconciler`` — daemon-thread lifecycle, stop-event
  semantics, contract-loader gating, exception swallowing, rebase
  callable bridging, and interval honouring (TASK-5-3).
* ``_run_implement_phase_slices`` — slice scheduler iteration, parent
  branch resolution, ``parent_branch_at_creation`` persistence,
  per-slice PR creation, failure handling, reconciler lifecycle
  bracketing, and the empty-slices fast path (TASK-4-2 / TASK-4-4 /
  TASK-5-1 / TASK-5-3 plumbing).
* ``_run_concurrent_phase`` — slice_id propagation through
  ``EGG_PIPELINE_ID`` / ``EGG_SLICE_ID`` env override and through the
  ``ConcurrentPhaseExecutor`` constructor (TASK-4-3).
* ``_handle_brc_consensus_timeout`` — slice_id forwarded to the
  per-slice tracker lookup (TASK-4-3 / decision-14 hybrid).

These tests sit alongside the existing slice-scheduler /
stacked-PR-reconciler / slice-branch-naming unit tests and the
``test_concurrent_executor.py`` slice_id init test — together they
form the implement-phase tester surface for #2137.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing routes.pipelines (which transitively
# pulls container_spawner / docker_client).
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
from egg_contracts.models import (
    PipelinePhase as ContractPhase,
)
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import (  # noqa: E402
    _handle_brc_consensus_timeout,
    _run_concurrent_phase,
    _run_implement_phase_slices,
    _start_stacked_pr_reconciler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(pipeline_id: str = "issue-9999", issue_number: int | None = 9999) -> Pipeline:
    """Pipeline with concurrent_execution enabled for slice-loop tests."""
    config = PipelineConfig()
    for key, val in {
        "concurrent_execution": True,
        "max_concurrent_agents": 6,
        "consensus_timeout_minutes": 30,
    }.items():
        try:
            setattr(config, key, val)
        except (AttributeError, ValueError):
            config.__dict__[key] = val
    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch=f"egg/issue-{issue_number}" if issue_number else f"egg/{pipeline_id}",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_contract(
    pipeline_id: str = "issue-9999",
    issue_number: int = 9999,
    slices: list[Slice] | None = None,
) -> Contract:
    return Contract(
        schemaVersion="1.0",
        issue=IssueInfo(number=issue_number, title=f"#{issue_number}", url=""),
        pipeline_id=pipeline_id,
        current_phase=ContractPhase.IMPLEMENT,
        slices=slices or [],
    )


def _make_slice(
    slice_id: str,
    *,
    name: str | None = None,
    deps: list[str] | None = None,
    tasks: list[Task] | None = None,
) -> Slice:
    return Slice(
        id=slice_id,
        name=name or f"Slice {slice_id}",
        status=SliceStatus.PENDING,
        dependencies=deps or [],
        tasks=tasks or [],
    )


def _make_task(task_id: str, description: str = "") -> Task:
    return Task(
        id=task_id,
        description=description or f"Task {task_id}",
        status=TaskStatus.PENDING,
        files_affected=[],
    )


# ---------------------------------------------------------------------------
# _start_stacked_pr_reconciler
# ---------------------------------------------------------------------------


class TestStartStackedPrReconciler:
    """Daemon-thread lifecycle and tick semantics for the reconciler."""

    def test_returns_alive_daemon_thread_and_stop_event(self) -> None:
        pipeline = _make_pipeline()
        gateway = MagicMock()
        gateway.rebase_onto.return_value = True

        thread, stop_event = _start_stacked_pr_reconciler(
            pipeline.id,
            lambda: None,
            gateway,
            pipeline,
            interval_seconds=0.05,
        )
        try:
            assert thread.is_alive()
            assert thread.daemon is True
            assert pipeline.id in thread.name
            assert isinstance(stop_event, threading.Event)
        finally:
            stop_event.set()
            thread.join(timeout=2.0)
            assert not thread.is_alive(), "stop_event must terminate the daemon"

    def test_stop_event_terminates_within_one_interval(self) -> None:
        """Setting stop_event must wake the wait() and exit promptly."""
        pipeline = _make_pipeline()
        thread, stop_event = _start_stacked_pr_reconciler(
            pipeline.id,
            lambda: None,
            MagicMock(),
            pipeline,
            interval_seconds=10.0,  # long; stop_event must short-circuit
        )
        try:
            time.sleep(0.05)
            stop_event.set()
            thread.join(timeout=1.0)
            assert not thread.is_alive(), (
                "Event.wait must release immediately on set, not wait for the full interval"
            )
        finally:
            if thread.is_alive():
                stop_event.set()
                thread.join(timeout=2.0)

    def test_tick_invokes_contract_loader_and_reconcile_once(self) -> None:
        pipeline = _make_pipeline()
        contract = _make_contract(slices=[_make_slice("slice-1")])
        contract_loader = MagicMock(return_value=contract)

        with patch("orchestrator.stacked_pr_reconciler.reconcile_once") as mock_reconcile:
            mock_reconcile.return_value = MagicMock()
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                contract_loader,
                MagicMock(),
                pipeline,
                interval_seconds=0.02,
            )
            try:
                # Wait for at least one tick.
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    if mock_reconcile.call_count >= 1:
                        break
                    time.sleep(0.02)
            finally:
                stop_event.set()
                thread.join(timeout=2.0)

            assert contract_loader.call_count >= 1, "contract_loader must be invoked each tick"
            assert mock_reconcile.call_count >= 1, (
                "reconcile_once must be invoked when contract is non-None"
            )
            args, kwargs = mock_reconcile.call_args
            # The contract is passed positionally; the three callables come by kw.
            assert args[0] is contract or kwargs.get("contract") is contract or args[0] == contract
            # Callable seams.
            assert callable(kwargs["list_open_prs"])
            assert callable(kwargs["list_extant_branches"])
            assert callable(kwargs["rebase_onto"])

    def test_tick_skipped_when_loader_returns_none(self) -> None:
        pipeline = _make_pipeline()
        contract_loader = MagicMock(return_value=None)
        with patch("orchestrator.stacked_pr_reconciler.reconcile_once") as mock_reconcile:
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                contract_loader,
                MagicMock(),
                pipeline,
                interval_seconds=0.02,
            )
            try:
                # Let several ticks pass; we expect zero reconcile calls.
                time.sleep(0.2)
            finally:
                stop_event.set()
                thread.join(timeout=2.0)
            assert contract_loader.call_count >= 1
            assert mock_reconcile.call_count == 0, "None contract must short-circuit the tick body"

    def test_tick_continues_when_reconcile_raises(self) -> None:
        pipeline = _make_pipeline()
        contract = _make_contract(slices=[_make_slice("slice-1")])
        contract_loader = MagicMock(return_value=contract)

        with patch("orchestrator.stacked_pr_reconciler.reconcile_once") as mock_reconcile:
            mock_reconcile.side_effect = RuntimeError("boom")
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                contract_loader,
                MagicMock(),
                pipeline,
                interval_seconds=0.02,
            )
            try:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline and mock_reconcile.call_count < 2:
                    time.sleep(0.02)
                # Thread must still be alive after a raising tick.
                assert thread.is_alive()
                # The exception must NOT propagate out of the daemon.
                assert mock_reconcile.call_count >= 2, "Loop must keep ticking after a raising tick"
            finally:
                stop_event.set()
                thread.join(timeout=2.0)

    def test_rebase_onto_callable_bridges_to_gateway(self) -> None:
        """The rebase_onto seam threaded into reconcile_once must call the gateway."""
        pipeline = _make_pipeline()
        gateway = MagicMock()
        gateway.rebase_onto.return_value = True

        captured: dict[str, Any] = {}

        def _capture_callables(contract: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            # Stop after the first tick to keep the test fast.
            return MagicMock()

        contract = _make_contract(slices=[_make_slice("slice-1")])
        with patch(
            "orchestrator.stacked_pr_reconciler.reconcile_once",
            side_effect=_capture_callables,
        ):
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                lambda: contract,
                gateway,
                pipeline,
                interval_seconds=0.02,
                worktree_repo_path=Path("/tmp/test-worktree"),
            )
            try:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and "rebase_onto" not in captured:
                    time.sleep(0.02)
            finally:
                stop_event.set()
                thread.join(timeout=2.0)

        rebase_callable = captured["rebase_onto"]
        result = rebase_callable(
            "egg/issue-9999/slice-2", "egg/issue-9999", "egg/issue-9999/slice-1"
        )
        assert result is True
        gateway.rebase_onto.assert_called_once()
        call_args = gateway.rebase_onto.call_args
        # First positional arg is pipeline_id; remaining kwargs.
        assert call_args.args[0] == pipeline.id
        # Second positional is repo_path. Coder v5 fix (commit 7f4203469)
        # threads ``worktree_repo_path`` through to ``rebase_onto`` —
        # previously it was ``pipeline.branch`` which would 4xx the
        # gateway. We supply ``Path("/tmp/test-worktree")`` via the new
        # ``worktree_repo_path`` keyword on ``_start_stacked_pr_reconciler``
        # for this test (added below) and assert it flows through.
        assert call_args.args[1] == "/tmp/test-worktree", (
            "Coder v5 fix: rebase_onto must receive a real filesystem path "
            "as repo_path, not the branch string"
        )
        assert call_args.kwargs["branch"] == "egg/issue-9999/slice-2"
        assert call_args.kwargs["new_base"] == "egg/issue-9999"
        assert call_args.kwargs["old_base"] == "egg/issue-9999/slice-1"
        # agent_role is fixed to "coder" so the gateway accepts the
        # request through the existing per-agent /git endpoint.
        assert call_args.kwargs["agent_role"] == "coder"

    def test_rebase_onto_returns_false_on_gateway_exception(self) -> None:
        pipeline = _make_pipeline()
        gateway = MagicMock()
        gateway.rebase_onto.side_effect = RuntimeError("network down")

        captured: dict[str, Any] = {}

        def _capture_callables(contract: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return MagicMock()

        contract = _make_contract(slices=[_make_slice("slice-1")])
        with patch(
            "orchestrator.stacked_pr_reconciler.reconcile_once",
            side_effect=_capture_callables,
        ):
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                lambda: contract,
                gateway,
                pipeline,
                interval_seconds=0.02,
            )
            try:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and "rebase_onto" not in captured:
                    time.sleep(0.02)
            finally:
                stop_event.set()
                thread.join(timeout=2.0)

        rebase_callable = captured["rebase_onto"]
        # A raising gateway must surface as False — the reconciler counts
        # it as a failure but does not let the daemon die.
        assert rebase_callable("b", "n", "o") is False

    def test_explicit_interval_overrides_env_lookup(self) -> None:
        """Passing interval_seconds bypasses get_stacked_pr_reconciler_interval_seconds."""
        pipeline = _make_pipeline()
        with patch(
            "orchestrator.env_config.get_stacked_pr_reconciler_interval_seconds"
        ) as mock_env:
            mock_env.return_value = 30.0
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                lambda: None,
                MagicMock(),
                pipeline,
                interval_seconds=0.05,
            )
            try:
                # When interval_seconds is supplied, env_config should NOT be consulted.
                assert mock_env.call_count == 0
            finally:
                stop_event.set()
                thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# _run_implement_phase_slices
# ---------------------------------------------------------------------------


class TestRunImplementPhaseSlices:
    """Slice-loop dispatch, parent-branch resolution, PR creation, failure handling."""

    def _make_spawner(self) -> MagicMock:
        spawner = MagicMock()
        spawner.gateway = MagicMock()
        spawner.gateway.create_slice_pr.return_value = "https://example/pr/1"
        return spawner

    def _make_loader_save_pair(self, contract: Contract) -> tuple[MagicMock, MagicMock]:
        """Returns (load_contract, save_contract) mocks bound to a fresh contract.

        load_contract returns the same contract object each call so the
        loop's per-slice ``parent_branch_at_creation`` write is observable.
        """
        load_mock = MagicMock(return_value=contract)
        save_mock = MagicMock()
        return load_mock, save_mock

    def test_empty_slices_returns_failure_fast(self) -> None:
        pipeline = _make_pipeline()
        contract = _make_contract(slices=[])

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase") as mock_run_phase,
        ):
            exit_code, logs = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=self._make_spawner(),
                repo_volumes={},
                gateway_mode="public",
                repos=["owner/repo"],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )
        assert exit_code == 1
        assert "no slices in contract" in logs
        # No reconciler thread, no per-slice phase invocation.
        mock_start_recon.assert_not_called()
        mock_run_phase.assert_not_called()

    def test_single_root_slice_uses_pipeline_branch_as_parent(self) -> None:
        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", tasks=[_make_task("task-1-1")])
        contract = _make_contract(slices=[slice_obj])
        load_mock, save_mock = self._make_loader_save_pair(contract)

        with (
            patch("egg_contracts.loader.load_contract", load_mock),
            patch("egg_contracts.loader.save_contract", save_mock),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch(
                "routes.pipelines._run_concurrent_phase", return_value=(0, "ok")
            ) as mock_run_phase,
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = self._make_spawner()
            exit_code, logs = _run_implement_phase_slices(
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
        assert exit_code == 0
        # _run_concurrent_phase invoked exactly once, with slice_id=slice-1.
        assert mock_run_phase.call_count == 1
        call_kwargs = mock_run_phase.call_args.kwargs
        assert call_kwargs["slice_id"] == "slice-1"
        # parent_branch_at_creation persisted to the pipeline branch.
        assert slice_obj.parent_branch_at_creation == pipeline.branch
        save_mock.assert_called()
        # Per-slice PR opens against the pipeline branch.
        spawner.gateway.create_slice_pr.assert_called_once()
        pr_kwargs = spawner.gateway.create_slice_pr.call_args.kwargs
        assert pr_kwargs["base"] == pipeline.branch
        assert pr_kwargs["head"] == f"{pipeline.branch}/slice-1"
        assert pr_kwargs["slice_id"] == "slice-1"

    def test_child_slice_targets_parent_integration_branch(self) -> None:
        pipeline = _make_pipeline()
        root = _make_slice("slice-1", tasks=[_make_task("task-1-1")])
        child = _make_slice("slice-2", deps=["slice-1"], tasks=[_make_task("task-2-1")])
        contract = _make_contract(slices=[root, child])
        load_mock = MagicMock(return_value=contract)
        save_mock = MagicMock()

        with (
            patch("egg_contracts.loader.load_contract", load_mock),
            patch("egg_contracts.loader.save_contract", save_mock),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch(
                "routes.pipelines._run_concurrent_phase", return_value=(0, "ok")
            ) as mock_run_phase,
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = self._make_spawner()
            exit_code, _ = _run_implement_phase_slices(
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
        assert exit_code == 0
        assert mock_run_phase.call_count == 2
        # First call: root slice; parent = pipeline branch.
        first_kwargs = mock_run_phase.call_args_list[0].kwargs
        assert first_kwargs["slice_id"] == "slice-1"
        # Second call: child slice; parent_branch persisted matches root's
        # integration branch (issue-N/slice-1).
        second_kwargs = mock_run_phase.call_args_list[1].kwargs
        assert second_kwargs["slice_id"] == "slice-2"
        assert child.parent_branch_at_creation == f"egg/issue-{pipeline.issue_number}/slice-1"
        # Per-slice PRs: child's base is the root's integration branch.
        pr_calls = spawner.gateway.create_slice_pr.call_args_list
        assert len(pr_calls) == 2
        assert pr_calls[0].kwargs["base"] == pipeline.branch
        assert pr_calls[1].kwargs["base"] == f"egg/issue-{pipeline.issue_number}/slice-1"

    def test_slice_failure_records_failure_does_not_abort(self) -> None:
        pipeline = _make_pipeline()
        root = _make_slice("slice-1")
        sibling = _make_slice("slice-2")
        contract = _make_contract(slices=[root, sibling])

        # Fail slice-1; slice-2 (independent) still runs.
        def _phase_side_effect(**kwargs: Any) -> tuple[int, str]:
            sid = kwargs.get("slice_id")
            if sid == "slice-1":
                return 1, "slice-1 failed"
            return 0, "slice-2 ok"

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch(
                "routes.pipelines._run_concurrent_phase", side_effect=_phase_side_effect
            ) as mock_run_phase,
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = self._make_spawner()
            exit_code, logs = _run_implement_phase_slices(
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
        # Both siblings ran (slice failure does not cancel sibling — refine-phase decision-2).
        invoked_slice_ids = {c.kwargs["slice_id"] for c in mock_run_phase.call_args_list}
        assert invoked_slice_ids == {"slice-1", "slice-2"}
        # Failed slice surfaces the non-zero overall exit code.
        assert exit_code == 1
        # Failed slice does NOT get a PR open.
        pr_calls_slice_ids = [
            c.kwargs["slice_id"] for c in spawner.gateway.create_slice_pr.call_args_list
        ]
        assert "slice-1" not in pr_calls_slice_ids
        assert "slice-2" in pr_calls_slice_ids

    def test_pr_creation_failure_marks_slice_failed(self) -> None:
        """Coder v6 hardened the PR-creation path: a slice whose
        ``create_slice_pr`` raises is now ``record_failure()``-d and
        contributes to a non-zero overall exit code, instead of being
        silently ``record_complete()``-d. Sibling slices still run
        (decision-2 sibling-independence). This test pins the post-v6
        invariant — previously asserted ``exit_code == 0`` (silent
        fallback) which we now treat as a regression."""
        pipeline = _make_pipeline()
        contract = _make_contract(slices=[_make_slice("slice-1"), _make_slice("slice-2")])

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = self._make_spawner()
            spawner.gateway.create_slice_pr.side_effect = [
                RuntimeError("rate limited"),
                "https://example/pr/2",
            ]
            exit_code, _ = _run_implement_phase_slices(
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
        # PR creation failure on slice-1 surfaces as non-zero exit
        # (no silent fallback). slice-2 still runs (decision-2
        # sibling-independence) and gets its own PR opened.
        assert exit_code != 0, (
            "Coder v6 invariant: PR creation failure must surface as a "
            "non-zero exit — no silent record_complete on failure"
        )
        assert spawner.gateway.create_slice_pr.call_count == 2, (
            "Sibling slice must still run regardless of slice-1's PR failure"
        )

    def test_reconciler_started_and_stopped(self) -> None:
        pipeline = _make_pipeline()
        contract = _make_contract(slices=[_make_slice("slice-1")])

        fake_thread = MagicMock(spec=threading.Thread)
        fake_event = threading.Event()
        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch(
                "routes.pipelines._start_stacked_pr_reconciler",
                return_value=(fake_thread, fake_event),
            ) as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            spawner = self._make_spawner()
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
        mock_start_recon.assert_called_once()
        # The reconciler stop event is set in the finally clause so the
        # daemon thread can exit cleanly.
        assert fake_event.is_set(), "stop_event must be set on slice-loop exit"
        # Thread.join is invoked with a bounded timeout to avoid blocking.
        fake_thread.join.assert_called_once()

    def test_single_slice_path_skips_pr_when_repo_unset(self) -> None:
        """If pipeline.repo is empty the loop must not attempt create_slice_pr."""
        pipeline = _make_pipeline()
        pipeline.repo = ""
        contract = _make_contract(slices=[_make_slice("slice-1")])

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = self._make_spawner()
            exit_code, _ = _run_implement_phase_slices(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                spawner=spawner,
                repo_volumes={},
                gateway_mode="public",
                repos=[],
                sandbox_env={},
                store=MagicMock(),
                certs_volume=None,
                worktree_repo_path=Path("/tmp/x"),
            )
        assert exit_code == 0
        spawner.gateway.create_slice_pr.assert_not_called()


# ---------------------------------------------------------------------------
# Coder fixes for reviewer_code_holistic v1 NACK (now regression guards)
# ---------------------------------------------------------------------------
#
# The tests below started life as ``pytest.mark.xfail(strict=True)``
# markers pinning post-fix invariants for the three blocking findings
# the holistic reviewer flagged on coder commit 36d34da9 (tester v1 NACK).
# Coder v5 (commit 7f4203469) landed the fixes, so the markers have
# been removed and the assertions promoted to regular regression
# guards. The seam names follow the actual coder fix:
#
#   * Holistic NACK #1 → ``GatewayClient.create_slice_integration_branch``
#     (the coder's fix; my v1 xfail named the missing seam
#     ``push_worktree_branch`` which was the closest existing primitive
#     at the time).
#   * Holistic NACK #2 → ``GatewayClient.list_open_prs`` /
#     ``list_remote_branches`` are now implemented and threaded into
#     the reconciler.


class TestCoderFixesForHolisticReview:
    """Regression guards locking in the coder v5 fixes for holistic v1 NACK."""

    def test_integration_branch_created_before_create_slice_pr(self) -> None:
        """Holistic NACK #1 fix: ``create_slice_integration_branch`` must
        push the per-slice ref before the per-slice PR is opened so the
        head contains commits when gh pr create runs."""
        pipeline = _make_pipeline()
        slice_obj = _make_slice("slice-1", tasks=[_make_task("task-1-1")])
        contract = _make_contract(slices=[slice_obj])

        call_order: list[str] = []

        def _track_create_branch(*args: Any, **kwargs: Any) -> bool:
            call_order.append("create_slice_integration_branch")
            return True

        def _track_create_pr(*args: Any, **kwargs: Any) -> str:
            call_order.append("create_slice_pr")
            return "https://example/pr/1"

        with (
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract"),
            patch("routes.pipelines._start_stacked_pr_reconciler") as mock_start_recon,
            patch("routes.pipelines._run_concurrent_phase", return_value=(0, "ok")),
            patch("orchestrator.peer_consensus.remove_peer_consensus_tracker"),
        ):
            mock_start_recon.return_value = (MagicMock(), threading.Event())
            spawner = MagicMock()
            spawner.gateway = MagicMock()
            spawner.gateway.create_slice_integration_branch = MagicMock(
                side_effect=_track_create_branch
            )
            spawner.gateway.create_slice_pr = MagicMock(side_effect=_track_create_pr)

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
        # Coder v5 fix: integration branch is created BEFORE the PR is
        # opened, so gh pr create finds a populated head ref.
        assert spawner.gateway.create_slice_integration_branch.called, (
            "Coder must push the slice integration branch (egg/issue-N/slice-1) "
            "before calling create_slice_pr"
        )
        assert (
            "create_slice_integration_branch" in call_order and "create_slice_pr" in call_order
        ), "both seams must be exercised"
        assert call_order.index("create_slice_integration_branch") < call_order.index(
            "create_slice_pr"
        ), (
            "create_slice_integration_branch must run BEFORE create_slice_pr — "
            "otherwise gh pr create fails on an empty head"
        )

    def test_reconciler_detects_real_orphans_not_no_op(self) -> None:
        pipeline = _make_pipeline()
        gateway = MagicMock()
        gateway.list_open_prs = MagicMock(
            return_value=[
                {
                    "number": 1,
                    "head": "egg/issue-9999/slice-2",
                    "base": "egg/issue-9999/slice-1",
                }
            ]
        )
        gateway.list_remote_branches = MagicMock(return_value={"egg/issue-9999"})
        gateway.rebase_onto.return_value = True

        captured: dict[str, Any] = {}

        def _capture_callables(contract: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return MagicMock(orphans_detected=0)

        contract = _make_contract(
            slices=[
                _make_slice("slice-1"),
                _make_slice(
                    "slice-2", deps=["slice-1"]
                ),  # parent-branch-at-creation persisted by the loop
            ]
        )
        with patch(
            "orchestrator.stacked_pr_reconciler.reconcile_once",
            side_effect=_capture_callables,
        ):
            thread, stop_event = _start_stacked_pr_reconciler(
                pipeline.id,
                lambda: contract,
                gateway,
                pipeline,
                interval_seconds=0.02,
            )
            try:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and "list_open_prs" not in captured:
                    time.sleep(0.02)
            finally:
                stop_event.set()
                thread.join(timeout=2.0)
        # The list-callable threaded into reconcile_once must call the
        # gateway's list helper — not return an empty list directly.
        list_open_prs_callable = captured["list_open_prs"]
        result = list_open_prs_callable()
        assert gateway.list_open_prs.called, (
            "Reconciler's list_open_prs callable must call "
            "GatewayClient.list_open_prs(repo); the current stub returns []"
        )
        assert len(result) > 0, (
            "Reconciler must surface at least one PR for orphan detection; "
            "today the stub returns an empty list"
        )


# ---------------------------------------------------------------------------
# _run_concurrent_phase slice_id propagation
# ---------------------------------------------------------------------------


class TestRunConcurrentPhaseSliceIdPropagation:
    """slice_id must flow into sandbox env and ConcurrentPhaseExecutor init."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=0.0)
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_no_slice_id_leaves_env_intact(
        self,
        MockExecutor: MagicMock,
        mock_prompt: MagicMock,
        mock_state_lock: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        # When slice_id is None, EGG_PIPELINE_ID/EGG_SLICE_ID stay
        # untouched — pre-slicing semantics.
        pipeline = _make_pipeline()
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        instance = MagicMock()
        instance.spawn_all.return_value = []  # no agents — short-circuit
        instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = instance

        store = MagicMock()
        store.load_pipeline.return_value = MagicMock()
        store.load_pipeline.return_value.get_phase_execution.return_value = MagicMock()

        original_env = {"EGG_PIPELINE_ID": pipeline.id, "OTHER": "v"}
        _run_concurrent_phase(
            pipeline_id=pipeline.id,
            pipeline=pipeline,
            phase="implement",
            spawner=MagicMock(),
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env=original_env,
            store=store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/x"),
        )
        # Caller-supplied dict is not mutated either way.
        assert original_env == {"EGG_PIPELINE_ID": pipeline.id, "OTHER": "v"}
        # Executor constructed with slice_id=None.
        assert MockExecutor.call_args.kwargs.get("slice_id") is None

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=0.0)
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_slice_id_overrides_env_and_forwards_to_executor(
        self,
        MockExecutor: MagicMock,
        mock_prompt: MagicMock,
        mock_state_lock: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        pipeline = _make_pipeline()
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        instance = MagicMock()
        instance.spawn_all.return_value = []
        instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = instance

        store = MagicMock()
        store.load_pipeline.return_value = MagicMock()
        store.load_pipeline.return_value.get_phase_execution.return_value = MagicMock()

        original_env = {"EGG_PIPELINE_ID": pipeline.id, "OTHER": "v"}
        _run_concurrent_phase(
            pipeline_id=pipeline.id,
            pipeline=pipeline,
            phase="implement",
            spawner=MagicMock(),
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env=original_env,
            store=store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/x"),
            slice_id="slice-3",
        )
        # Caller's dict is not mutated; the function takes a shallow copy.
        assert original_env == {"EGG_PIPELINE_ID": pipeline.id, "OTHER": "v"}
        # Executor receives slice_id="slice-3".
        assert MockExecutor.call_args.kwargs["slice_id"] == "slice-3"


# ---------------------------------------------------------------------------
# _handle_brc_consensus_timeout slice_id propagation
# ---------------------------------------------------------------------------


class TestHandleBrcConsensusTimeoutSliceId:
    """slice_id must reach the per-slice tracker lookup."""

    def test_slice_id_forwarded_to_tracker_lookup(self) -> None:
        pipeline = _make_pipeline()

        # Patch peer_consensus.get_peer_consensus_tracker — this is the
        # symbol the function imports.
        with patch("peer_consensus.get_peer_consensus_tracker") as mock_get:
            tracker = MagicMock()
            tracker.handle_timeout.return_value = {"action": "noop"}
            tracker.is_timeout_handled.return_value = False
            mock_get.return_value = tracker
            _handle_brc_consensus_timeout(
                pipeline=pipeline,
                pipeline_id=pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["coder"],
                store=MagicMock(),
                slice_id="slice-7",
            )
            # The lookup must include the slice scope.
            assert mock_get.called
            args, kwargs = mock_get.call_args
            # Forwarded as a positional or kw arg depending on signature.
            assert pipeline.id in args
            assert "slice-7" in args or kwargs.get("slice_id") == "slice-7"

    def test_no_slice_id_uses_pipeline_scope(self) -> None:
        pipeline = _make_pipeline()
        with patch("peer_consensus.get_peer_consensus_tracker") as mock_get:
            tracker = MagicMock()
            tracker.handle_timeout.return_value = {"action": "noop"}
            tracker.is_timeout_handled.return_value = False
            mock_get.return_value = tracker
            _handle_brc_consensus_timeout(
                pipeline=pipeline,
                pipeline_id=pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["coder"],
                store=MagicMock(),
            )
            assert mock_get.called
            args, kwargs = mock_get.call_args
            # When slice_id is None it is still forwarded; the tracker
            # store interprets None as the pipeline-scoped tracker.
            slice_passed = kwargs.get("slice_id")
            if slice_passed is None and len(args) >= 2:
                slice_passed = args[1]
            assert slice_passed is None

    def test_typeerror_falls_back_to_pipeline_scope(self) -> None:
        """Older import-shim trackers without slice_id fall back gracefully."""
        pipeline = _make_pipeline()

        call_history: list[tuple] = []

        def _shim_get(*args: Any, **kwargs: Any) -> MagicMock:
            call_history.append((args, kwargs))
            if len(args) > 1 or "slice_id" in kwargs:
                raise TypeError("legacy shim — no slice_id support")
            tracker = MagicMock()
            tracker.handle_timeout.return_value = {"action": "noop"}
            tracker.is_timeout_handled.return_value = False
            return tracker

        with patch("peer_consensus.get_peer_consensus_tracker", side_effect=_shim_get):
            _handle_brc_consensus_timeout(
                pipeline=pipeline,
                pipeline_id=pipeline.id,
                consensus_timeout=1800.0,
                blocking_agents=["coder"],
                store=MagicMock(),
                slice_id="slice-7",
            )
        # Two calls: first with slice_id (raises TypeError), second
        # without slice_id (succeeds).
        assert len(call_history) == 2
        # Second call has only the pipeline_id positionally.
        second_args, second_kwargs = call_history[1]
        assert second_args == (pipeline.id,) or (
            second_args == (pipeline.id, None) and "slice_id" not in second_kwargs
        )
