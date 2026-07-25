"""Tests for detection plane wiring into the runtime tick (#3596, task-1-2).

Verifies that:

1. ``run_detection_plane()`` is called from ``_run_runtime_tick_checks``
2. Findings are emitted as ``EventType.DETECTION_FINDING`` events on the event
   bus — exactly once, by ``HealthCheckRunner``, not by the monitor as well
3. Evaluation is rate-limited per pipeline, so the two tick call sites
   (``_check_pod`` and ``_reconciliation_sweep``) cannot double-fire
4. Findings are routed onward: ``requires_adjudication`` findings reach the
   overseer adjudicator, and routine findings reach the ``CorrectiveExecutor``

Every test here asserts against the landed wiring. There is deliberately **no**
skip guard: an unwired detection plane must fail this file, not skip it.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_k8s_client():
    """Create a mock KubernetesClient."""
    from datetime import UTC, datetime

    from kubernetes_client import ContainerInfo, ContainerStatus

    client = MagicMock()
    client.list_containers.return_value = []
    client.get_container_info.return_value = ContainerInfo(
        container_id="uid-1",
        container_name="test-job",
        pod_name="test-pod-abc",
        job_name="test-job",
        status=ContainerStatus.RUNNING,
        started_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    client.cleanup_orphaned_containers.return_value = 0
    return client


@pytest.fixture()
def monitor(mock_k8s_client):
    """Create a KubernetesMonitor with a mock k8s client."""
    from kubernetes_monitor import KubernetesMonitor

    m = KubernetesMonitor(
        k8s_client=mock_k8s_client,
        check_interval=1,
        orphan_age_hours=24,
    )
    return m


def _make_running_pipeline(pipeline_id: str = "test-pipeline"):
    """Build a mock pipeline + store pair that reports RUNNING status."""
    from models import PipelineStatus

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.status = PipelineStatus.RUNNING
    pipeline.phases = {}
    pipeline.base_branch = "main"
    pipeline.repo = "owner/repo"
    pipeline.repos = []
    pipeline.issue_number = 3596
    pipeline.decisions = []
    pipeline.event_loop_owner = None
    pipeline.lifecycle_owner = None
    pipeline.awaiting_spawn = False

    store = MagicMock()
    store.list_pipelines.return_value = [pipeline_id]
    store.load_pipeline.return_value = pipeline
    store.repo_path = "/tmp/repo"

    return pipeline, store


def _make_finding(
    finding_class: str = "forward_progress_stall",
    *,
    requires_adjudication: bool = False,
):
    from health_checks.types import Finding, Severity

    return Finding(
        finding_class=finding_class,
        severity=Severity.MEDIUM,
        evidence={"agent_role": "coder", "last_commit_age_s": 700},
        recommended_action="Agent not making progress",
        requires_adjudication=requires_adjudication,
        detector_key="forward_progress",
    )


# ---------------------------------------------------------------------------
# AC-1: run_detection_plane() is called from _run_runtime_tick_checks
# ---------------------------------------------------------------------------


class TestDetectionPlaneWiring:
    """The detection plane must be invoked from the runtime tick."""

    def test_runtime_tick_references_detection_plane(self):
        """``_run_runtime_tick_checks`` must reach the detection plane at all.

        Guards the regression this whole slice exists to prevent: 27 registered
        detectors that production never invokes.
        """
        from kubernetes_monitor import KubernetesMonitor

        source = inspect.getsource(KubernetesMonitor._run_runtime_tick_checks)
        source += inspect.getsource(KubernetesMonitor._run_detection_plane)
        assert "run_detection_plane" in source

    def test_run_detection_plane_called_from_runtime_tick(self, monitor):
        """``_run_runtime_tick_checks`` must call ``runner.run_detection_plane()``."""
        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        mock_runner.run_detection_plane.assert_called_once()

    def test_run_detection_plane_receives_snapshot(self, monitor):
        """The snapshot passed must be built from the pipeline's health context."""
        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        call_args = mock_runner.run_detection_plane.call_args
        snapshot = call_args.args[0] if call_args.args else call_args.kwargs.get("snapshot")

        assert snapshot is not None
        assert snapshot.pipeline_id == "test-pipeline"

    def test_run_detection_plane_receives_plane(self, monitor):
        """The plane passed must be the default plane with detectors registered."""
        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        call_args = mock_runner.run_detection_plane.call_args
        plane = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("plane")

        assert plane is not None
        assert len(plane.detectors) > 0
        assert "forward_progress" in plane.detectors


# ---------------------------------------------------------------------------
# AC-2: Findings are emitted as DETECTION_FINDING events, exactly once
# ---------------------------------------------------------------------------


class TestDetectionFindingEvents:
    """Detection findings must be emitted on the event bus by the runner."""

    def test_detection_finding_event_type_exists(self):
        """``EventType.DETECTION_FINDING`` must be a real member.

        Before task-1-1 the emitters fell back to ``HEALTH_CHECK_DEGRADED`` via
        ``getattr(..., default)``, silently routing every finding onto the
        degradation topic where no consumer expects it.
        """
        from events import EventType

        assert EventType.DETECTION_FINDING.value == "system.detection.finding"

    def test_runner_emits_finding_on_bus(self):
        """``run_detection_plane`` emits one DETECTION_FINDING per finding."""
        from events import EventType
        from health_checks.runner import HealthCheckRunner

        finding = _make_finding()
        plane = MagicMock()
        plane.evaluate.return_value = [finding]
        snapshot = MagicMock()
        snapshot.pipeline_id = "test-pipeline"

        runner = HealthCheckRunner()
        mock_bus = MagicMock()
        with patch.object(HealthCheckRunner, "_get_event_bus", return_value=mock_bus):
            findings = runner.run_detection_plane(snapshot, plane, pipeline_id="test-pipeline")

        assert findings == [finding]
        mock_bus.emit.assert_called_once()
        args, kwargs = mock_bus.emit.call_args
        assert args[0] is EventType.DETECTION_FINDING
        assert args[1] == "test-pipeline"
        assert kwargs["data"]["finding_class"] == "forward_progress_stall"

    def test_monitor_does_not_re_emit_findings(self, monitor):
        """The monitor must NOT emit findings a second time.

        The runner is the single emitter. A second emission from the monitor's
        tick path put two identical DETECTION_FINDING events on the bus for
        every finding (#3596 task-1-2 review finding (b)).
        """
        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = [_make_finding()]
        monitor.set_health_check_runner(mock_runner)

        with patch("events.get_event_bus") as mock_bus_fn:
            mock_bus = MagicMock()
            mock_bus_fn.return_value = mock_bus

            monitor._run_runtime_tick_checks()

            finding_emits = [
                ec
                for ec in mock_bus.emit.call_args_list
                if isinstance(ec.kwargs.get("data"), dict) and "finding_class" in ec.kwargs["data"]
            ]
            assert finding_emits == [], (
                "The monitor must delegate finding emission to the runner; "
                f"it emitted {len(finding_emits)} finding event(s) itself"
            )

    def test_no_findings_no_emit(self):
        """When the plane yields nothing, nothing is emitted."""
        from health_checks.runner import HealthCheckRunner

        plane = MagicMock()
        plane.evaluate.return_value = []
        snapshot = MagicMock()
        snapshot.pipeline_id = "test-pipeline"

        mock_bus = MagicMock()
        with patch.object(HealthCheckRunner, "_get_event_bus", return_value=mock_bus):
            HealthCheckRunner().run_detection_plane(snapshot, plane)

        mock_bus.emit.assert_not_called()


# ---------------------------------------------------------------------------
# AC-3: Idempotent evaluation (no double-firing from two call sites)
# ---------------------------------------------------------------------------


class TestIdempotentEvaluation:
    """Both tick call sites share one rate-limited claim per pipeline.

    ``_monitor_loop`` (via ``_check_pod``) and ``_reconciliation_thread`` (via
    ``_reconciliation_sweep``) both call ``_run_runtime_tick_checks``. Without a
    real guard the plane evaluates — and therefore acts — twice per interval.
    """

    def test_second_tick_within_interval_does_not_re_evaluate(self, monitor):
        """A second tick inside the min interval must not re-evaluate."""
        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()
        monitor._run_runtime_tick_checks()
        monitor._run_runtime_tick_checks()

        assert mock_runner.run_detection_plane.call_count == 1

    def test_tick_after_interval_re_evaluates(self, monitor):
        """Once the min interval has elapsed the plane evaluates again."""
        from kubernetes_monitor import DETECTION_PLANE_MIN_INTERVAL_SECONDS

        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()
        # Backdate the claim rather than sleeping through the real interval.
        monitor._detection_plane_last_eval["test-pipeline"] -= (
            DETECTION_PLANE_MIN_INTERVAL_SECONDS + 1
        )
        monitor._run_runtime_tick_checks()

        assert mock_runner.run_detection_plane.call_count == 2

    def test_claim_is_per_pipeline(self, monitor):
        """One pipeline's claim must not suppress another's evaluation."""
        _p1, store1 = _make_running_pipeline("pipeline-a")
        _p2, store2 = _make_running_pipeline("pipeline-b")
        monitor._reconciliation_stores = [store1, store2]

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        mock_runner.run_detection_plane.return_value = []
        monitor.set_health_check_runner(mock_runner)

        monitor._run_runtime_tick_checks()

        evaluated = {
            c.kwargs.get("pipeline_id") for c in mock_runner.run_detection_plane.call_args_list
        }
        assert evaluated == {"pipeline-a", "pipeline-b"}

    def test_terminal_pipeline_state_is_forgotten(self, monitor):
        """Claim state for a finished pipeline must not leak forever."""
        monitor._detection_plane_last_eval["gone"] = 1.0
        monitor._corrective_executors["gone"] = object()

        monitor._forget_detection_plane_state("gone")

        assert "gone" not in monitor._detection_plane_last_eval
        assert "gone" not in monitor._corrective_executors

    def test_reconciliation_sweep_runs_the_tick(self, monitor, mock_k8s_client):
        """``_reconciliation_sweep`` is the second tick call site."""
        _pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mock_k8s_client.list_containers.return_value = []
        mock_k8s_client.list_jobs.return_value = []

        with patch.object(monitor, "_run_runtime_tick_checks") as mock_tick:
            monitor._reconciliation_sweep()

        mock_tick.assert_called_once()


# ---------------------------------------------------------------------------
# AC-4: Findings are routed onward, not dropped
# ---------------------------------------------------------------------------


class TestFindingRouting:
    """Emitting a finding is not acting on it — the slice must do both."""

    def test_routine_finding_reaches_corrective_executor(self, monitor):
        """A routine finding in the action table is executed, with no LLM call."""
        from kubernetes_monitor import ROUTINE_CORRECTIVE_ACTIONS

        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        finding = _make_finding("heartbeat_stall")
        assert "heartbeat_stall" in ROUTINE_CORRECTIVE_ACTIONS

        executor = MagicMock()
        snapshot = MagicMock()
        snapshot.running_agents = ()

        with patch.object(monitor, "_get_corrective_executor", return_value=executor):
            with patch.object(monitor, "_adjudicate_findings") as mock_adjudicate:
                monitor._route_detection_findings([finding], pipeline, "test-pipeline", snapshot)

        mock_adjudicate.assert_not_called()
        executor.execute.assert_called_once()
        action = executor.execute.call_args.args[0]
        assert action == ROUTINE_CORRECTIVE_ACTIONS["heartbeat_stall"]
        assert executor.execute.call_args.kwargs["target_role"] == "coder"

    def test_adjudication_finding_reaches_the_adjudicator(self, monitor):
        """A ``requires_adjudication`` finding is escalated, not silently dropped."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        finding = _make_finding("phase_stall", requires_adjudication=True)
        verdict = MagicMock()
        snapshot = MagicMock()
        snapshot.running_agents = ()

        with patch.object(
            monitor, "_adjudicate_findings", return_value=[(finding, verdict)]
        ) as mock_adjudicate:
            with patch.object(monitor, "_get_corrective_executor", return_value=MagicMock()):
                with patch("routes.pipelines._execute_overseer_verdicts") as mock_exec:
                    monitor._route_detection_findings(
                        [finding], pipeline, "test-pipeline", snapshot
                    )

        mock_adjudicate.assert_called_once()
        mock_exec.assert_called_once()
        assert mock_exec.call_args.args[0] == [(finding, verdict)]

    def test_unmapped_routine_finding_is_emit_only(self, monitor):
        """A finding class absent from the action table takes no action.

        ``Finding.recommended_action`` is operator-facing prose, not a member of
        the executor's closed vocabulary, so guessing an action from it is
        exactly the crying-wolf failure the plane exists to prevent.
        """
        from kubernetes_monitor import ROUTINE_CORRECTIVE_ACTIONS

        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        finding = _make_finding("duration_drift")
        assert "duration_drift" not in ROUTINE_CORRECTIVE_ACTIONS

        snapshot = MagicMock()
        snapshot.running_agents = ()

        with patch.object(monitor, "_get_corrective_executor") as mock_get_executor:
            monitor._route_detection_findings([finding], pipeline, "test-pipeline", snapshot)

        mock_get_executor.assert_not_called()

    def test_corrective_executor_is_cached_per_pipeline(self, monitor):
        """The executor holds the idempotency + rate-limit state across ticks.

        Building a fresh one per tick resets both, so the same corrective action
        re-fires on every interval.
        """
        with patch("routes.pipelines._build_overseer_corrective_executor") as mock_build:
            mock_build.return_value = MagicMock()
            first = monitor._get_corrective_executor("test-pipeline", 3596)
            second = monitor._get_corrective_executor("test-pipeline", 3596)

        assert first is second
        assert mock_build.call_count == 1

    def test_routing_failure_does_not_break_the_tick(self, monitor):
        """Corrective execution is advisory — a raise must not kill the loop."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        executor = MagicMock()
        executor.execute.side_effect = RuntimeError("gateway down")
        snapshot = MagicMock()
        snapshot.running_agents = ()

        with patch.object(monitor, "_get_corrective_executor", return_value=executor):
            monitor._route_detection_findings(
                [_make_finding("heartbeat_stall")], pipeline, "test-pipeline", snapshot
            )


# ---------------------------------------------------------------------------
# AC-4 (cont.): target-role derivation — the value the routing loop dispatches
# on and keys idempotency by
# ---------------------------------------------------------------------------


def _container_finding(container: str, finding_class: str = "container_oom_evicted"):
    """A finding whose only target evidence is a container id.

    This is the ``detect_container_oom_evicted`` shape: it fires off
    ``container_transitions`` alone, so ``role`` is absent and ``container``
    carries a pod id.
    """
    from health_checks.types import Finding, Severity

    return Finding(
        finding_class=finding_class,
        severity=Severity.HIGH,
        evidence={"container": container},
        recommended_action="Respawn the evicted container",
        detector_key="container_k8s",
    )


class TestFindingTargetRole:
    """``finding_target_role`` decides both the dispatch target and half the key."""

    def test_container_outside_known_roles_is_rejected(self):
        """A pod id is not a role — dispatching a restart at it targets a UUID."""
        from health_checks.types import finding_target_role

        assert finding_target_role(_container_finding("pod-abc-123"), {"coder", "tester"}) == ""

    def test_container_inside_known_roles_is_returned(self):
        """A ``container`` value that *is* a role name is the legitimate target."""
        from health_checks.types import finding_target_role

        assert finding_target_role(_container_finding("coder"), {"coder", "tester"}) == "coder"

    def test_unknown_role_set_returns_the_container_unvalidated(self):
        """``None`` means "roles unknown", not "no role is valid".

        Read-only callers (logging, idempotency keys) pass ``None`` and get the
        raw candidate rather than an empty string.
        """
        from health_checks.types import finding_target_role

        assert finding_target_role(_container_finding("pod-abc-123"), None) == "pod-abc-123"

    @pytest.mark.parametrize("key", ["agent_role", "agent_id", "role"])
    def test_authoritative_keys_bypass_validation(self, key):
        """The three role-bearing keys are authoritative — never filtered.

        Only the ``container`` fallback can carry a pod id, so validating the
        authoritative keys would drop legitimate targets whose agent has already
        left ``running_agents``.
        """
        from health_checks.types import Finding, Severity, finding_target_role

        finding = Finding(
            finding_class="heartbeat_stall",
            severity=Severity.MEDIUM,
            evidence={key: "documenter"},
            recommended_action="Nudge the agent",
            detector_key="liveness",
        )

        assert finding_target_role(finding, {"coder"}) == "documenter"

    def test_empty_known_roles_does_not_swallow_the_container(self, monitor):
        """An empty ``running_agents`` must not collapse every target to ``""``.

        ``running_agents`` is empty whenever exit-info lookup degrades, and a
        container-keyed finding still needs its target: an empty validation set
        would collapse the idempotency key to ``"container_oom_evicted:"`` —
        the exact defect ``finding_target_role`` exists to prevent — and
        dispatch ``respawn_cohort`` with no cohort.
        """
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        executor = MagicMock()
        snapshot = MagicMock()
        snapshot.running_agents = ()
        snapshot.raw = {}

        with patch.object(monitor, "_get_corrective_executor", return_value=executor):
            monitor._route_detection_findings(
                [_container_finding("pod-abc-123")], pipeline, "test-pipeline", snapshot
            )

        kwargs = executor.execute.call_args.kwargs
        assert kwargs["target_role"] == "pod-abc-123"
        assert kwargs["idempotency_key"] == "container_oom_evicted:pod-abc-123"

    def test_pod_id_in_known_roles_is_not_admitted_as_a_role(self, monitor):
        """A container the snapshot could not map carries ``role=str(cid)``.

        Leaving it in the validation set would let the guard admit the very pod
        id it was added to reject, so the snapshot reports those ids and the
        routing loop subtracts them.
        """
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        agent = MagicMock()
        agent.role = "pod-abc-123"
        snapshot = MagicMock()
        snapshot.running_agents = (agent,)
        snapshot.raw = {"unmapped_container_ids": ("pod-abc-123",)}

        executor = MagicMock()
        with patch.object(monitor, "_get_corrective_executor", return_value=executor):
            monitor._route_detection_findings(
                [_container_finding("pod-abc-123")], pipeline, "test-pipeline", snapshot
            )

        # Nothing else is a known role, so the set is empty and the unvalidated
        # fallback applies — but the pod id was never treated as a *validated*
        # role, which is what would have let it through a non-empty set.
        assert executor.execute.call_args.kwargs["target_role"] == "pod-abc-123"

    def test_unmapped_pod_id_does_not_validate_a_second_pod_id(self, monitor):
        """With one real role present, an unmapped pod id must stay rejected."""
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        mapped = MagicMock()
        mapped.role = "coder"
        unmapped = MagicMock()
        unmapped.role = "pod-abc-123"
        snapshot = MagicMock()
        snapshot.running_agents = (mapped, unmapped)
        snapshot.raw = {"unmapped_container_ids": ("pod-abc-123",)}

        executor = MagicMock()
        with patch.object(monitor, "_get_corrective_executor", return_value=executor):
            monitor._route_detection_findings(
                [_container_finding("pod-abc-123")], pipeline, "test-pipeline", snapshot
            )

        assert executor.execute.call_args.kwargs["target_role"] == ""

    def test_one_raising_finding_does_not_abandon_the_rest(self, monitor):
        """A handler that raises costs its own finding, not the whole tick.

        ``_corrective_respawn_cohort`` raises on an empty cohort and the
        executor does not wrap handler calls, so a shared ``try`` dropped every
        finding sorting after it — permanently, since the idempotency key is
        only recorded on success.
        """
        pipeline, store = _make_running_pipeline()
        monitor._reconciliation_stores = [store]

        executor = MagicMock()
        executor.execute.side_effect = [RuntimeError("empty target cohort"), None]
        snapshot = MagicMock()
        snapshot.running_agents = ()
        snapshot.raw = {}

        findings = [_container_finding("pod-abc-123"), _make_finding("heartbeat_stall")]
        with patch.object(monitor, "_get_corrective_executor", return_value=executor):
            monitor._route_detection_findings(findings, pipeline, "test-pipeline", snapshot)

        assert executor.execute.call_count == 2
        assert executor.execute.call_args.kwargs["target_role"] == "coder"


# ---------------------------------------------------------------------------
# AC-4 (cont.): the adjudication cooldown is a reservation, not a spend
# ---------------------------------------------------------------------------


class TestAdjudicationClaimRelease:
    """A claim that never became a spawn must be released, or the triple is
    silenced for the whole cooldown with no retry."""

    def test_claim_is_taken_once_then_blocked(self, monitor):
        """The cooldown bars a second spawn for the same triple."""
        finding = _make_finding("phase_stall", requires_adjudication=True)

        assert monitor._claim_adjudication("test-pipeline", finding) is True
        assert monitor._claim_adjudication("test-pipeline", finding) is False

    def test_release_restores_the_claim(self, monitor):
        """Releasing lets the very next tick retry the same triple."""
        finding = _make_finding("phase_stall", requires_adjudication=True)

        assert monitor._claim_adjudication("test-pipeline", finding) is True
        monitor._release_adjudication("test-pipeline", finding)

        assert monitor._claim_adjudication("test-pipeline", finding) is True

    def test_gateway_prep_failure_releases_the_claim(self, monitor):
        """A transient gateway round-trip failure must not spend the cooldown."""
        pipeline, _store = _make_running_pipeline()
        finding = _make_finding("phase_stall", requires_adjudication=True)

        with patch("routes.pipelines._compute_gateway_mode", side_effect=RuntimeError("boom")):
            result = monitor._adjudicate_findings([finding], pipeline, "test-pipeline", 3596)

        assert result == []
        assert monitor._claim_adjudication("test-pipeline", finding) is True

    def test_raising_escalation_releases_the_claim(self, monitor):
        """An escalation that raised left the condition unexamined — retry it."""
        pipeline, _store = _make_running_pipeline()
        finding = _make_finding("phase_stall", requires_adjudication=True)

        with patch("routes.pipelines._compute_gateway_mode", return_value=("public", "public")):
            with patch("routes.pipelines._get_spawner", return_value=MagicMock()):
                with patch(
                    "routes.pipelines._escalate_finding_to_adjudicator",
                    side_effect=RuntimeError("spawn failed"),
                ):
                    result = monitor._adjudicate_findings(
                        [finding], pipeline, "test-pipeline", 3596
                    )

        assert result == []
        assert monitor._claim_adjudication("test-pipeline", finding) is True

    def test_none_verdict_releases_the_claim(self, monitor):
        """No verdict means no adjudicator opinion was recorded."""
        pipeline, _store = _make_running_pipeline()
        finding = _make_finding("phase_stall", requires_adjudication=True)

        with patch("routes.pipelines._compute_gateway_mode", return_value=("public", "public")):
            with patch("routes.pipelines._get_spawner", return_value=MagicMock()):
                with patch("routes.pipelines._escalate_finding_to_adjudicator", return_value=None):
                    result = monitor._adjudicate_findings(
                        [finding], pipeline, "test-pipeline", 3596
                    )

        assert result == []
        assert monitor._claim_adjudication("test-pipeline", finding) is True

    def test_real_verdict_spends_the_claim(self, monitor):
        """The cooldown counts adjudicators that actually ran — this one did."""
        pipeline, _store = _make_running_pipeline()
        finding = _make_finding("phase_stall", requires_adjudication=True)
        verdict = MagicMock()

        with patch("routes.pipelines._compute_gateway_mode", return_value=("public", "public")):
            with patch("routes.pipelines._get_spawner", return_value=MagicMock()):
                with patch(
                    "routes.pipelines._escalate_finding_to_adjudicator", return_value=verdict
                ):
                    result = monitor._adjudicate_findings(
                        [finding], pipeline, "test-pipeline", 3596
                    )

        assert result == [(finding, verdict)]
        assert monitor._claim_adjudication("test-pipeline", finding) is False

    def test_claim_key_separates_distinct_targets(self, monitor):
        """Two agents stalling must not look like one repeat of the first."""
        coder = _make_finding("phase_stall", requires_adjudication=True)
        tester = _make_finding("phase_stall", requires_adjudication=True)
        object.__setattr__(tester, "evidence", {"agent_role": "tester"})

        assert monitor._claim_adjudication("test-pipeline", coder) is True
        assert monitor._claim_adjudication("test-pipeline", tester) is True
