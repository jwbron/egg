"""Tests for restart_phase ↔ consensus-timeout-timer reset (issue #3315).

A phase that is parked past the consensus-timeout budget and then restarted
used to fire a spurious consensus-timeout OVERSEER_ALERT + HITL decision
against the freshly-restarted phase, because the *old* ``_run_concurrent_phase``
thread kept polling with a stale ``start_time`` (its poll loop had no
``run_epoch`` check) and, separately, the decision it opened was never
withdrawn once consensus subsequently converged.

These tests cover:
- facet (a): the poll loop bails (non-zero, no escalation) when ``run_epoch``
  changes mid-flight, so a superseded thread cannot escalate.
- facet (c): ``_cancel_consensus_timeout_decisions`` cancels a pending
  ``consensus_timeout_incomplete`` HITL once the phase converges.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    DecisionStatus,
    PhaseExecution,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import (
    _CONSENSUS_TIMEOUT_HITL_CONTEXT,
    _cancel_consensus_timeout_decisions,
    _phase_bail_reason_impl,
    _run_concurrent_phase,
    _run_concurrent_phase_with_impasse_retry,
)

_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


def _make_pipeline(pipeline_id: str = "issue-3315") -> Pipeline:
    config = PipelineConfig()
    for key, val in {
        "concurrent_execution": True,
        "max_concurrent_agents": 5,
        "consensus_timeout_minutes": 30,
    }.items():
        try:
            setattr(config, key, val)
        except AttributeError, ValueError:
            config.__dict__[key] = val
    return Pipeline(
        id=pipeline_id,
        issue_number=3315,
        repo="owner/repo",
        branch="egg/issue-3315",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.REFINE,
        config=config,
        run_epoch=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _make_real_store(pipeline: Pipeline) -> MagicMock:
    """MagicMock store that round-trips a real Pipeline through load/save."""
    holder = {"json": pipeline.model_dump_json()}
    store = MagicMock()
    store.load_pipeline.side_effect = lambda _pid: Pipeline.model_validate_json(holder["json"])
    store.save_pipeline.side_effect = lambda p: holder.__setitem__("json", p.model_dump_json())
    return store


class TestEpochGuardBailsWithoutEscalation:
    """facet (a): a restart-superseded thread exits without escalating."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_superseded_thread_exits_without_timeout_escalation(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        # A stable monotonic clock (constant ⇒ ``elapsed == 0``). The point of
        # this test is *not* to drive elapsed past the timeout budget — it's
        # that the epoch guard short-circuits at step 0 of the poll loop,
        # before ``check_consensus`` and long before any elapsed/timeout
        # arithmetic, so a superseded thread can never reach the escalation
        # branch regardless of how much wall-clock has accrued. We assert
        # exactly that below (``check_consensus.call_count == 0`` + no
        # escalation), so the constant clock is sufficient.
        mock_monotonic.return_value = 100_000.0

        pipeline = _make_pipeline()
        phase_exec = PhaseExecution(phase=PipelinePhase.REFINE, status=PipelineStatus.RUNNING)

        # The reloaded pipeline carries a NEWER run_epoch — i.e. restart_phase
        # bumped it and a new _run_pipeline thread now owns the pipeline.
        mock_store = MagicMock()
        reloaded = MagicMock()
        reloaded.get_phase_execution.return_value = phase_exec
        reloaded.status = PipelineStatus.RUNNING
        reloaded.run_epoch = datetime(2030, 1, 1, tzinfo=UTC)
        reloaded.created_at = datetime(2030, 1, 1, tzinfo=UTC)
        mock_store.load_pipeline.return_value = reloaded

        executions = [
            AgentExecution(
                role=AgentRole.REFINER,
                status=AgentExecutionStatus.RUNNING,
                container_id="refiner-1",
                started_at=datetime.now(UTC),
            ),
        ]
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_spawner = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("routes.pipelines._handle_brc_consensus_timeout") as mock_timeout,
            patch("routes.pipelines._persist_hitl_decision") as mock_hitl,
        ):
            exit_code, logs = _run_concurrent_phase(
                pipeline_id="issue-3315",
                pipeline=pipeline,
                phase="refine",
                spawner=mock_spawner,
                store=mock_store,
                run_epoch=pipeline.run_epoch,
                **_CALL_ARGS,
            )

        # Bails non-zero (never treated as success → never advances the phase).
        assert exit_code == 1
        assert "superseded" in logs.lower()
        # The epoch check is step 0 — before consensus is ever evaluated.
        assert mock_executor_instance.check_consensus.call_count == 0
        # No consensus-timeout escalation of any kind fired.
        mock_timeout.assert_not_called()
        mock_hitl.assert_not_called()
        # The stale event loop was torn down so it stops requesting spawns.
        assert mock_executor_instance.stop_event_loop.called


class TestAutoWithdrawConsensusTimeoutDecision:
    """facet (c): convergence withdraws a stale consensus-timeout HITL."""

    def test_pending_timeout_decision_is_cancelled(self):
        pipeline = _make_pipeline()
        decision = pipeline.add_decision(
            question="Consensus timed out; consensus incomplete; agents never confirmed: refiner.",
            options=["Retry phase", "Accept current state", "Abort phase"],
            phase=PipelinePhase.REFINE,
        )
        decision.context = _CONSENSUS_TIMEOUT_HITL_CONTEXT

        withdrawn = _cancel_consensus_timeout_decisions(pipeline)

        assert withdrawn == 1
        assert decision.status == DecisionStatus.CANCELLED
        assert decision.resolution is not None
        assert "converged" in decision.resolution
        assert decision.resolved_at is not None
        assert pipeline.get_pending_decisions() == []

    def test_unrelated_pending_decision_is_left_alone(self):
        pipeline = _make_pipeline()
        # A different pending decision (e.g. an objection HITL) must survive.
        other = pipeline.add_decision(
            question="Agent(s) objecting to phase completion. How to proceed?",
            options=["Override objections", "Wait for resolution", "Abort phase"],
            phase=PipelinePhase.REFINE,
        )

        withdrawn = _cancel_consensus_timeout_decisions(pipeline)

        assert withdrawn == 0
        assert other.status == DecisionStatus.PENDING

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_convergence_withdraws_stale_decision_end_to_end(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """A stale consensus-timeout HITL on disk is cancelled when the phase
        converges — proving the wiring into ``_update_agents_complete``."""
        mock_monotonic.return_value = 10.0

        # Disk pipeline carries a pending consensus-timeout decision opened by
        # a (now superseded) thread before this phase converged.
        disk_pipeline = _make_pipeline()
        stale = disk_pipeline.add_decision(
            question="Consensus timed out; consensus incomplete; agents never confirmed: refiner.",
            options=["Retry phase", "Accept current state", "Abort phase"],
            phase=PipelinePhase.REFINE,
        )
        stale.context = _CONSENSUS_TIMEOUT_HITL_CONTEXT
        mock_store = _make_real_store(disk_pipeline)

        executions = [
            AgentExecution(
                role=AgentRole.REFINER,
                status=AgentExecutionStatus.RUNNING,
                container_id="refiner-1",
                started_at=datetime.now(UTC),
            ),
        ]
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_spawner = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, _logs = _run_concurrent_phase(
            pipeline_id="issue-3315",
            pipeline=_make_pipeline(),
            phase="refine",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        # The decision was withdrawn in the same write that marked agents
        # COMPLETE — reload from the store and confirm it is no longer pending.
        persisted = mock_store.load_pipeline("issue-3315")
        assert persisted.get_pending_decisions() == []
        cancelled = next(d for d in persisted.decisions if d.id == stale.id)
        assert cancelled.status == DecisionStatus.CANCELLED
        assert cancelled.resolution is not None
        assert "converged" in cancelled.resolution


class TestPipelineSupersededHelper:
    """The epoch-supersession arm of the shared bail predicate (facet a).

    The standalone ``_pipeline_superseded_by_restart`` predicate these cases
    used to exercise was folded into ``_phase_bail_reason_impl`` (#3633), so
    they now pin the same #3315 semantics on the live implementation — the
    one both the poll loop and the impasse-retry wrapper actually call.
    """

    @staticmethod
    def _running(run_epoch, created_at=None):
        reloaded = MagicMock()
        reloaded.status = PipelineStatus.RUNNING
        reloaded.run_epoch = run_epoch
        reloaded.created_at = created_at if created_at is not None else run_epoch
        return reloaded

    def test_none_run_epoch_is_never_superseded(self):
        # Direct-call paths that don't thread an epoch opt out of the epoch
        # arm entirely — no epoch, no supersession, whatever is on disk.
        store = MagicMock()
        store.load_pipeline.return_value = self._running(datetime(2030, 1, 1, tzinfo=UTC))
        assert (
            _phase_bail_reason_impl(store=store, pipeline_id="issue-3315", run_epoch=None) is None
        )

    def test_newer_on_disk_epoch_means_superseded(self):
        store = MagicMock()
        store.load_pipeline.return_value = self._running(datetime(2030, 1, 1, tzinfo=UTC))
        assert (
            _phase_bail_reason_impl(
                store=store,
                pipeline_id="issue-3315",
                run_epoch=datetime(2020, 1, 1, tzinfo=UTC),
            )
            == "superseded_by_restart"
        )

    def test_matching_epoch_is_not_superseded(self):
        epoch = datetime(2025, 6, 1, tzinfo=UTC)
        store = MagicMock()
        store.load_pipeline.return_value = self._running(epoch)
        assert (
            _phase_bail_reason_impl(store=store, pipeline_id="issue-3315", run_epoch=epoch) is None
        )

    def test_load_failure_returns_no_bail(self):
        # A transient store hiccup must never tear down a running phase.
        store = MagicMock()
        store.load_pipeline.side_effect = RuntimeError("git read failed")
        assert (
            _phase_bail_reason_impl(
                store=store,
                pipeline_id="issue-3315",
                run_epoch=datetime(2020, 1, 1, tzinfo=UTC),
            )
            is None
        )


class TestImpasseRetrySkipsRoutingWhenSuperseded:
    """facet (a), slice path: a superseded thread does not route impasses.

    A stale producer-written impasse file must not drive ``route_impasses``
    into a HITL against a freshly-restarted phase.
    """

    @patch("orchestrator.impasse_routing.route_impasses")
    @patch("orchestrator.impasse_routing.collect_impasses")
    @patch("routes.pipelines._run_concurrent_phase")
    def test_superseded_thread_skips_route_impasses(self, mock_run, mock_collect, mock_route):
        mock_run.return_value = (0, "phase logs")
        # Non-empty impasse scan — without the guard this would route to HITL.
        mock_collect.return_value = [MagicMock()]

        # On-disk pipeline carries a NEWER epoch — a restart superseded us.
        reloaded = MagicMock()
        reloaded.run_epoch = datetime(2030, 1, 1, tzinfo=UTC)
        reloaded.created_at = datetime(2030, 1, 1, tzinfo=UTC)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = reloaded

        exit_code, logs = _run_concurrent_phase_with_impasse_retry(
            pipeline_id="issue-3315",
            pipeline=_make_pipeline(),
            phase="implement",
            spawner=MagicMock(),
            store=mock_store,
            run_epoch=datetime(2020, 1, 1, tzinfo=UTC),
            **_CALL_ARGS,
        )

        # Returned the (superseded) phase result untouched, and never routed.
        assert exit_code == 0
        assert logs == "phase logs"
        mock_route.assert_not_called()

    @patch("orchestrator.impasse_routing.route_impasses")
    @patch("orchestrator.impasse_routing.collect_impasses")
    @patch("routes.pipelines._run_concurrent_phase")
    def test_live_thread_still_routes_impasses(self, mock_run, mock_collect, mock_route):
        # Same setup but the on-disk epoch matches — routing must still happen.
        mock_run.return_value = (0, "phase logs")
        mock_collect.return_value = [MagicMock()]
        mock_route.return_value = []

        epoch = datetime(2025, 6, 1, tzinfo=UTC)
        reloaded = MagicMock()
        reloaded.run_epoch = epoch
        reloaded.created_at = epoch
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = reloaded

        _run_concurrent_phase_with_impasse_retry(
            pipeline_id="issue-3315",
            pipeline=_make_pipeline(),
            phase="implement",
            spawner=MagicMock(),
            store=mock_store,
            run_epoch=epoch,
            **_CALL_ARGS,
        )

        mock_route.assert_called()
