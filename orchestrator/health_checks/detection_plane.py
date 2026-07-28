"""Orchestrator-side deterministic detection plane (#2270 §-core, slice-4).

This is the structural replacement for the respawning overseer watcher pod
(refine HITL Option C). Instead of a long-lived agent polling and an LLM
classifying every observation, the orchestrator runs a set of cheap,
deterministic **detectors** over an :class:`EventStreamSnapshot` on its own
event loop. Each detector is a pure function ``snapshot -> Finding | None``:

* the overwhelming majority of observations are normal and yield ``None`` with
  **no LLM call** (the §2 "stop crying wolf" fix is structural here — a detector
  only fires on a condition it can prove from the snapshot); and
* a finding that is genuinely ambiguous / high-stakes sets
  ``requires_adjudication=True``, and *only then* does the orchestrator spawn a
  NORMAL on-demand OVERSEER agent to ADVISE (the escalation path in
  ``routes/pipelines._escalate_finding_to_adjudicator``).

The :class:`EventStreamSnapshot` here is the production mirror of the slice-1
calibration corpus snapshot (``tests/overseer_calibration/corpus.py``): same
field names, so a detector written against this type is driven verbatim by the
calibration harness, and the corpus never imports production code. Slices 7 and
8 register their detectors here; slice-4 ships the plane plus the
lifecycle-owner-aware :class:`PhaseStallDetector` (the #3230 false-stall fix's
core) so the ``phase_stall`` corpus rows flip to strict.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover - logging shim
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from health_checks.types import Finding, FindingClass, Severity

logger = get_logger("orchestrator.health_checks.detection_plane")


# ---------------------------------------------------------------------------
# Lifecycle owner — the #3230 distinction that makes the stall detector honest.
# ---------------------------------------------------------------------------


class LifecycleOwner(StrEnum):
    """Who owns the agent lifecycle at the instant the snapshot was taken.

    Under orchestrator-owned on-demand spawning (#3064) a phase can be RUNNING
    with zero *live container* agents for a beat while the orchestrator is about
    to spawn the next one-shot agent. That is **not** a stall — the #3230 false
    alerts came from treating "0 running agents" as "wedged". ``NONE`` means
    nothing is queued to make progress, which is the genuine stall condition.
    """

    ORCHESTRATOR = "orchestrator"
    AGENT = "agent"
    NONE = "none"


# ---------------------------------------------------------------------------
# Snapshot data model — the production input detectors evaluate. Field-compatible
# with the slice-1 corpus snapshot so the calibration harness drives detectors
# written against this type directly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunningAgent:
    """One agent in the running-agent set, annotated with its lifecycle owner."""

    role: str
    state: str
    lifecycle_owner: str = LifecycleOwner.ORCHESTRATOR.value
    exit_code: int | None = None
    exit_reason: str | None = None
    last_tool_call_age_s: float | None = None
    last_heartbeat_age_s: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunningAgent:
        return cls(
            role=data["role"],
            state=data.get("state", ""),
            lifecycle_owner=data.get("lifecycle_owner", LifecycleOwner.ORCHESTRATOR.value),
            exit_code=data.get("exit_code"),
            exit_reason=data.get("exit_reason"),
            last_tool_call_age_s=data.get("last_tool_call_age_s"),
            last_heartbeat_age_s=data.get("last_heartbeat_age_s"),
        )


@dataclass(frozen=True)
class EventStreamSnapshot:
    """A point-in-time snapshot of pipeline state a detector evaluates.

    Built on the orchestrator event loop from live state (see
    :func:`snapshot_from_health_context`) and, in tests, parsed from the
    calibration fixtures. Intentionally permissive — ``raw`` retains the full
    source dict so a later detector can read a forward-compatible field without
    a schema change.
    """

    snapshot_id: str
    pipeline_id: str = ""
    phase: str = ""
    running_agents: tuple[RunningAgent, ...] = ()
    consensus: dict[str, Any] = field(default_factory=dict)
    phase_state: dict[str, Any] = field(default_factory=dict)
    decision_state: dict[str, Any] = field(default_factory=dict)
    container_transitions: tuple[dict[str, Any], ...] = ()
    gateway_error_counters: dict[str, Any] = field(default_factory=dict)
    cost_counters: dict[str, Any] = field(default_factory=dict)
    midturn_messages: tuple[dict[str, Any], ...] = ()
    git_state: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventStreamSnapshot:
        return cls(
            snapshot_id=data["snapshot_id"],
            pipeline_id=data.get("pipeline_id", ""),
            phase=data.get("phase", ""),
            running_agents=tuple(RunningAgent.from_dict(a) for a in data.get("running_agents", [])),
            consensus=dict(data.get("consensus", {})),
            phase_state=dict(data.get("phase_state", {})),
            decision_state=dict(data.get("decision_state", {})),
            container_transitions=tuple(data.get("container_transitions", [])),
            gateway_error_counters=dict(data.get("gateway_error_counters", {})),
            cost_counters=dict(data.get("cost_counters", {})),
            midturn_messages=tuple(data.get("midturn_messages", [])),
            git_state=dict(data.get("git_state", {})),
            runtime=dict(data.get("runtime", {})),
            raw=dict(data),
        )

    def lifecycle_owner(self) -> str:
        """Resolve the lifecycle owner for this snapshot (#3230).

        Prefers the phase-state level annotation; falls back to the first
        running agent's owner; else ``NONE`` (nothing queued to make progress).
        """
        owner = self.phase_state.get("lifecycle_owner")
        if owner:
            return str(owner)
        if self.running_agents:
            return self.running_agents[0].lifecycle_owner
        return LifecycleOwner.NONE.value


# ---------------------------------------------------------------------------
# Detector protocol — a pure, cheap function over a snapshot.
# ---------------------------------------------------------------------------


@runtime_checkable
class Detector(Protocol):
    """A deterministic detector: pure, never raises, returns Optional[Finding].

    Detectors are callables so the slice-1 calibration harness
    (``Detector = Callable[[snapshot], Finding | None]``) can drive a production
    detector with no adapter. ``detector_key`` ties a detector to its
    calibration corpus rows.
    """

    detector_key: str
    name: str

    def __call__(self, snapshot: EventStreamSnapshot) -> Finding | None: ...


# ---------------------------------------------------------------------------
# The detection plane — registry + evaluator.
# ---------------------------------------------------------------------------


class DetectionPlane:
    """Runs registered detectors over a snapshot, yielding findings.

    The orchestrator builds one plane (see :func:`default_detection_plane`) and
    calls :meth:`evaluate` on each runtime tick / lifecycle event. Detector
    execution is exception-isolated: a buggy detector degrades to "no finding"
    and is logged, never crashing the event loop.
    """

    def __init__(self) -> None:
        self._detectors: list[Detector] = []

    @classmethod
    def default(cls) -> DetectionPlane:
        """Build a plane pre-wired with the detectors delivered so far.

        Slice-4 wires the lifecycle-owner-aware phase-stall detector. Slice-8
        (#2270 §5) registers the coverage-gap detector survey here — this is the
        single, stable coupling point the calibration corpus bridges to (it
        auto-registers every detector ``default().detectors`` carries by its
        ``detector_key``). Registration is delegated to
        :func:`_register_coverage_gap_detectors` (lazy imports of the tier1
        detector modules, so the plane module stays import-cheap).
        """
        plane = cls()
        plane.register(PhaseStallDetector())
        _register_coverage_gap_detectors(plane)
        return plane

    def register(self, detector: Detector) -> None:
        """Register a detector. Later registration order is preserved."""
        self._detectors.append(detector)
        logger.debug("Detector registered", detector_key=detector.detector_key)

    @property
    def detectors(self) -> dict[str, Detector]:
        """Registered detectors keyed by ``detector_key`` (registration order).

        Returning a mapping lets callers ask ``"phase_stall" in plane.detectors``
        while still iterating the detector objects via ``.values()``.
        """
        return {d.detector_key: d for d in self._detectors}

    def evaluate_one(self, detector: Detector, snapshot: EventStreamSnapshot) -> Finding | None:
        """Run a single detector, swallowing any internal error."""
        try:
            return detector(snapshot)
        except Exception as exc:  # noqa: BLE001 — a detector must never crash the loop
            logger.warning(
                "Detector raised; treating as no-finding",
                detector_key=getattr(detector, "detector_key", "?"),
                error=str(exc),
            )
            return None

    def evaluate(self, snapshot: EventStreamSnapshot) -> list[Finding]:
        """Run every registered detector and collect the non-None findings."""
        findings: list[Finding] = []
        for detector in self._detectors:
            finding = self.evaluate_one(detector, snapshot)
            if finding is not None:
                findings.append(finding)
        return findings

    @staticmethod
    def requires_adjudication(findings: Iterable[Finding]) -> list[Finding]:
        """Filter to findings the orchestrator must escalate to an adjudicator."""
        return [f for f in findings if f.requires_adjudication]


def escalate_findings(
    findings: Iterable[Finding],
    *,
    spawn_adjudicator: Any,
) -> list[Finding]:
    """Escalate exactly the findings that require adjudication, nothing else.

    The cost guard at the heart of Option C: deterministic detectors resolve the
    routine majority in-process with no LLM, and ``spawn_adjudicator`` is invoked
    **once per finding whose ``requires_adjudication`` is set** — never for the
    rest, and never at all when there are no findings.

    ``spawn_adjudicator`` is an injected single-argument callable
    ``(finding) -> Any``; the orchestrator wires it to the slice-3
    ``spawn_agent_job(agent_role=OVERSEER, …)`` path (the unit test injects a
    spy). Returns the escalated findings in order, so the caller can pair them
    with the verdicts the adjudicator produced.
    """
    escalated: list[Finding] = []
    for finding in findings:
        if getattr(finding, "requires_adjudication", False):
            spawn_adjudicator(finding)
            escalated.append(finding)
    return escalated


# ---------------------------------------------------------------------------
# Slice-4 detector: lifecycle-owner-aware phase stall (#3230 core).
# ---------------------------------------------------------------------------

# Default grace window before a zero-agent RUNNING phase with no queued owner is
# treated as wedged. Conservative on purpose (the §2 calibration goal): the
# #3230 false-stall snapshot is silenced by lifecycle ownership *and* stays well
# under this window, while the genuine stall is wedged far past it.
_DEFAULT_PHASE_STALL_GRACE_SECONDS = 3600


class PhaseStallDetector:
    """Fire only on a genuinely wedged phase — never on #3230 false stalls.

    A finding is produced iff ALL hold:
      * the phase status is RUNNING;
      * there are zero running agents;
      * no lifecycle owner is queued to spawn the next one-shot agent
        (owner is ``NONE`` and ``awaiting_spawn`` is falsey) — the #3230 fix;
      * no HITL decision is parked (a deliberate wait is not a stall); and
      * the phase has been wedged past the grace window.

    Because the genuine stall is inherently ambiguous (the orchestrator can see
    *that* nothing is progressing but not always *why*), the finding sets
    ``requires_adjudication=True`` so an on-demand OVERSEER advises before any
    corrective action.
    """

    detector_key: str = "phase_stall"
    name: str = "phase_stall_detector"

    def __init__(self, grace_seconds: int = _DEFAULT_PHASE_STALL_GRACE_SECONDS) -> None:
        self.grace_seconds = grace_seconds

    def __call__(self, snapshot: EventStreamSnapshot) -> Finding | None:
        phase_state = dict(getattr(snapshot, "phase_state", {}) or {})

        status = str(phase_state.get("status", "")).upper()
        if status != "RUNNING":
            return None

        # Agents are live → not a zero-agent stall (their own liveness/heartbeat
        # detectors own that case).
        if getattr(snapshot, "running_agents", ()):
            return None

        # #3230: the orchestrator (or an agent) owns the lifecycle and is about
        # to spawn the next one-shot agent → progress is queued, not stalled.
        owner = self._lifecycle_owner(snapshot, phase_state)
        if owner in (LifecycleOwner.ORCHESTRATOR.value, LifecycleOwner.AGENT.value):
            return None
        if phase_state.get("awaiting_spawn"):
            return None

        # A parked HITL is a deliberate wait, not a wedge.
        decision_state = dict(getattr(snapshot, "decision_state", {}) or {})
        if decision_state.get("pending_hitl") or decision_state.get("open_decisions"):
            return None

        # Past the grace window?
        age = phase_state.get("started_age_s")
        try:
            age_s = float(age) if age is not None else None
        except TypeError, ValueError:
            age_s = None
        if age_s is None or age_s < self.grace_seconds:
            return None

        blocking = []
        consensus = getattr(snapshot, "consensus", {}) or {}
        if isinstance(consensus, dict):
            blocking = list(consensus.get("blocking_agents", []) or [])

        return Finding(
            finding_class=FindingClass.PHASE_STALL,
            severity=Severity.HIGH,
            evidence={
                "phase": getattr(snapshot, "phase", ""),
                "status": status,
                "running_agents": 0,
                "lifecycle_owner": owner,
                "started_age_s": age_s,
                "grace_seconds": self.grace_seconds,
                "blocking_agents": blocking,
            },
            recommended_action=(
                "Phase is RUNNING with no running agents and no owner queued to "
                "spawn one, wedged past the grace window. Adjudicate whether to "
                "nudge the blocking cohort, respawn it, or open an operator HITL."
            ),
            requires_adjudication=True,
            detector_key=self.detector_key,
        )

    @staticmethod
    def _lifecycle_owner(snapshot: EventStreamSnapshot, phase_state: dict[str, Any]) -> str:
        owner = phase_state.get("lifecycle_owner")
        if owner:
            return str(owner)
        agents = getattr(snapshot, "running_agents", ()) or ()
        if agents:
            return str(getattr(agents[0], "lifecycle_owner", LifecycleOwner.NONE.value))
        return LifecycleOwner.NONE.value


def detect_phase_stall(
    snapshot: EventStreamSnapshot,
    *,
    grace_seconds: int = _DEFAULT_PHASE_STALL_GRACE_SECONDS,
) -> Finding | None:
    """Module-level slice-4 detector — the form the calibration corpus registers.

    Shares one implementation with :class:`PhaseStallDetector` (the plane
    registers the *object* so it self-describes via ``detector_key`` / ``name``;
    the corpus registers this bare function). Fires ``phase_stall`` / ``high`` /
    ``requires_adjudication=True`` on a genuinely wedged phase and stays silent
    on the #3230 false stall — see :class:`PhaseStallDetector` for the full rule.
    """
    return PhaseStallDetector(grace_seconds=grace_seconds)(snapshot)


# ---------------------------------------------------------------------------
# Plane factory + live-state snapshot builder.
# ---------------------------------------------------------------------------


def _register_coverage_gap_detectors(plane: DetectionPlane) -> None:
    """Register the slice-8 §5 coverage-gap detectors onto ``plane`` (idempotent).

    The slice-8 survey lives in ``health_checks.tier1`` as bare
    ``detect_* -> Finding | None`` functions (each carrying ``detector_key`` /
    ``name`` attributes so it satisfies the :class:`Detector` protocol). They
    are deterministic (``requires_adjudication=False`` except the
    orchestrator-thread-death case), so the bounded corrective executor
    (slice-6) consumes their findings without an LLM. A detector whose
    ``detector_key`` is already present is skipped, so this is safe on repeats.

    Note: a detector only fires in a live run once
    :func:`snapshot_from_health_context` populates the field it reads
    (``container_transitions`` / ``cost_counters`` / ``git_state`` /
    ``gateway_error_counters`` / ``decision_state`` …); the calibration corpus
    drives every detector with fully-populated fixtures today.
    """
    from health_checks.tier1.brc_thrashing import (
        detect_brc_thrash,
        detect_incomplete_consensus_deferral,
    )
    from health_checks.tier1.consensus_stall import detect_heartbeat_stall
    from health_checks.tier1.container_k8s import (
        detect_container_death,
        detect_container_oom_evicted,
        detect_container_restart_loop,
        detect_overseer_self_injection,
    )
    from health_checks.tier1.cost_budget import detect_cost_anomaly
    from health_checks.tier1.decision_queue import (
        detect_approved_decision_orphaned,
        detect_auto_advance_wedge,
        detect_hitl_queue_backlog,
        detect_restarted_decision_replay,
    )
    from health_checks.tier1.gateway_health import (
        detect_gateway_error_spike,
        detect_gateway_repeated_denial,
        detect_gateway_token_expiry,
    )
    from health_checks.tier1.llm_substrate import (
        detect_anthropic_5xx_sustained,
        detect_effective_model_drift,
        detect_llm_substrate_unreachable,
    )
    from health_checks.tier1.loop_detection import detect_tool_input_loop
    from health_checks.tier1.runtime_liveness import (
        detect_agent_restart_propagation,
        detect_duration_drift,
        detect_run_pipeline_thread_liveness,
    )
    from health_checks.tier1.worktree_branch import (
        detect_disk_inode_pressure,
        detect_pr_external_mutation,
        detect_pushed_pr_not_updated,
        detect_worktree_corruption,
    )
    from overseer.self_monitor import detect_overseer_self_health

    coverage_gap_detectors = (
        detect_container_death,
        detect_container_oom_evicted,
        detect_container_restart_loop,
        detect_overseer_self_injection,
        detect_run_pipeline_thread_liveness,
        detect_duration_drift,
        detect_agent_restart_propagation,
        detect_auto_advance_wedge,
        detect_approved_decision_orphaned,
        detect_restarted_decision_replay,
        detect_hitl_queue_backlog,
        detect_worktree_corruption,
        detect_disk_inode_pressure,
        detect_pr_external_mutation,
        detect_pushed_pr_not_updated,
        detect_gateway_error_spike,
        detect_gateway_repeated_denial,
        detect_gateway_token_expiry,
        detect_brc_thrash,
        detect_incomplete_consensus_deferral,
        detect_cost_anomaly,
        detect_llm_substrate_unreachable,
        detect_effective_model_drift,
        detect_anthropic_5xx_sustained,
        detect_overseer_self_health,
        # detect_heartbeat_stall is registered LAST so that the consensus-stall
        # double-fire guard (TASK-2-2) can check whether ConsensusStallCheck
        # has already reported for this snapshot before firing.
        detect_heartbeat_stall,
        # detect_tool_input_loop is the #3665 slice-3 primary deliverable:
        # a deterministic detector for repetition loops that fires when an
        # agent produces zero new tool inputs over a trailing window.
        detect_tool_input_loop,
    )

    existing = set(plane.detectors)
    for detector in coverage_gap_detectors:
        if getattr(detector, "detector_key", None) in existing:
            continue
        plane.register(detector)


def default_detection_plane() -> DetectionPlane:
    """Build the detection plane with the detectors delivered so far.

    Thin alias for :meth:`DetectionPlane.default` kept for call sites that read
    as a factory function (e.g. ``routes/pipelines``).
    """
    return DetectionPlane.default()


def snapshot_from_health_context(context: Any) -> EventStreamSnapshot:
    """Build an :class:`EventStreamSnapshot` from a ``PipelineHealthContext``.

    Best-effort and defensive: the detection plane runs on the event loop and
    must never crash on a partially-populated context. Fields the live state
    doesn't expose yet stay empty (a detector simply won't fire on them); slices
    7/8 enrich this builder as their detectors need more signal.

    Populates the 5 in-scope fields (#3665 slice-1):
      - ``midturn_messages`` — agent tool-call logs for loop detection (TASK-1-1)
      - ``runtime`` — driver heartbeat ages (TASK-1-2)
      - ``consensus`` — peer consensus tracker evaluation (TASK-1-3)
      - ``container_transitions`` — pod state transitions (TASK-1-4)
      - ``running_agents`` — agent role + age fields (TASK-1-5)

    The remaining 4 fields (``decision_state``, ``gateway_error_counters``,
    ``cost_counters``, ``git_state``) are Tier 3-4 candidates and remain
    empty by decision.
    """
    pipeline = getattr(context, "pipeline", None)
    pipeline_id = getattr(context, "pipeline_id", "") or getattr(pipeline, "id", "")
    phase = getattr(context, "current_phase", None)
    phase_value = getattr(phase, "value", "") if phase is not None else ""

    lifecycle_owner = _context_lifecycle_owner(context, pipeline)
    phase_state: dict[str, Any] = {
        "status": _context_phase_status(pipeline, phase_value),
        "lifecycle_owner": lifecycle_owner,
        "event_loop_owner": getattr(context, "event_loop_owner", None)
        or _getattr_chain(pipeline, "event_loop_owner"),
        "started_age_s": getattr(context, "phase_started_age_s", None),
        "awaiting_spawn": getattr(context, "awaiting_spawn", None),
    }

    running_agents = _build_running_agents(context, pipeline, pipeline_id, phase_value, lifecycle_owner)

    return EventStreamSnapshot(
        snapshot_id=f"{pipeline_id}:{phase_value}",
        pipeline_id=str(pipeline_id),
        phase=str(phase_value),
        running_agents=running_agents,
        phase_state=phase_state,
        runtime=_build_runtime_section(pipeline_id),
        consensus=_build_consensus_section(pipeline_id),
        container_transitions=_build_container_transitions(context, pipeline_id, phase_value),
        midturn_messages=_build_midturn_messages(context, pipeline_id, phase_value),
    )


def _context_lifecycle_owner(context: Any, pipeline: Any) -> str:
    """Resolve the lifecycle owner from context/pipeline, defaulting to ORCHESTRATOR.

    Under #3064 orchestrator-owned spawning the orchestrator owns the lifecycle;
    we only down-grade to ``NONE`` when something explicitly says so. Defaulting
    to ORCHESTRATOR keeps the #3230 false-stall fix conservative (prefer silence).
    """
    owner = getattr(context, "lifecycle_owner", None)
    if owner:
        return str(owner)
    event_loop_owner = _getattr_chain(pipeline, "event_loop_owner")
    if event_loop_owner:
        return LifecycleOwner.ORCHESTRATOR.value
    return LifecycleOwner.ORCHESTRATOR.value


def _context_phase_status(pipeline: Any, phase_value: str) -> str:
    try:
        phases = getattr(pipeline, "phases", {}) or {}
        phase_exec = phases.get(phase_value)
        status = getattr(phase_exec, "status", None)
        if status is not None:
            return str(getattr(status, "value", status)).upper()
    except Exception:  # noqa: BLE001 — defensive snapshot building
        pass
    return ""


def _getattr_chain(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Field builders for snapshot_from_health_context (slice-1, TASK-1-1..TASK-1-5).
# Each is defensive: a failure in one builder yields an empty field, never a
# crash that takes down the event loop.
# ---------------------------------------------------------------------------


def _build_runtime_section(pipeline_id: str) -> dict[str, Any]:
    """Populate the ``runtime`` section from driver_heartbeat ages (TASK-1-2).

    Wires ``driver_heartbeat.tick_age_seconds()`` and ``spawn_age_seconds()``
    so that ``detect_run_pipeline_thread_liveness`` and ``DriverLivenessCheck``
    can read the ages from the snapshot rather than calling the module directly.
    """
    try:
        from driver_heartbeat import spawn_age_seconds, tick_age_seconds

        return {
            "tick_age_s": tick_age_seconds(pipeline_id),
            "spawn_age_s": spawn_age_seconds(pipeline_id),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_consensus_section(pipeline_id: str) -> dict[str, Any]:
    """Populate the ``consensus`` section from the peer-consensus tracker (TASK-1-3).

    Wires ``peer_consensus.get_peer_consensus_tracker().evaluate()`` so that
    ``detect_brc_thrash``, ``detect_incomplete_consensus_deferral``, and the
    consensus field readers in ``PhaseStallDetector`` can read the tracker
    output from the snapshot.
    """
    try:
        from peer_consensus import get_peer_consensus_tracker

        tracker = get_peer_consensus_tracker(pipeline_id)
        if tracker is None:
            return {}
        return dict(tracker.evaluate())
    except Exception:  # noqa: BLE001 — defensive; tracker may be absent
        return {}


def _build_container_transitions(
    context: Any, pipeline_id: str, phase_value: str
) -> tuple[dict[str, Any], ...]:
    """Populate ``container_transitions`` from the kubernetes_monitor's pod-state log (TASK-1-4).

    The monitor tracks ``_pod_states`` (pod_id -> ContainerStatus) on every
    state change. We surface those as transition records so that
    ``detect_container_death``, ``detect_container_oom_evicted``,
    ``detect_container_restart_loop``, and ``detect_overseer_self_injection``
    can read real pod transitions from the snapshot.
    """
    try:
        from kubernetes_monitor import get_kubernetes_monitor

        monitor = get_kubernetes_monitor()
        if monitor is None:
            return ()
        pod_states = getattr(monitor, "_pod_states", None)
        if not pod_states:
            return ()
        # Build transition records from the live pod-state map. Each record
        # carries the pod_id, current status, and the pipeline/phase context so
        # detectors can correlate.
        transitions: list[dict[str, Any]] = []
        for pod_id, status in pod_states.items():
            transitions.append(
                {
                    "pod_id": pod_id,
                    "status": str(status),
                    "pipeline_id": pipeline_id,
                    "phase": phase_value,
                }
            )
        return tuple(transitions)
    except Exception:  # noqa: BLE001 — defensive
        return ()


def _build_midturn_messages(
    context: Any, pipeline_id: str, phase_value: str
) -> tuple[dict[str, Any], ...]:
    """Populate ``midturn_messages`` from agent tool-call logs (TASK-1-1).

    Reads captured agent logs from ``agent_log_store`` (which persists pod
    stdout at removal via ``read_job_log_snapshot``) and parses tool-call
    records. Each record carries a ``tool_name`` and ``input_hash`` so that
    the deterministic loop detector (``detect_tool_input_loop``) can count
    distinct tool inputs over a trailing window.

    The k8s log API truncates at ~100 chars per line, so we prefer the
    full-length captured logs from ``agent_log_store`` (TASK-3-2 increases
    fidelity further).
    """
    try:
        from agent_log_store import get_agent_log_store

        store = get_agent_log_store()
        records = store.list_records(pipeline_id, include_logs=True)
        messages: list[dict[str, Any]] = []
        for record in records:
            logs = record.get("logs", "")
            if not logs:
                continue
            for parsed in _parse_tool_calls_from_logs(logs, record):
                messages.append(parsed)
        return tuple(messages)
    except Exception:  # noqa: BLE001 — defensive
        return ()


def _parse_tool_calls_from_logs(
    logs: str, record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Extract tool-call records from agent log text.

    Agent stdout contains JSON-structured log lines emitted by
    ``egg_agent/client.py`` via ``logger.info("Tool call", ...)``. Each line
    is a JSON object with fields like ``message``, ``extra.event_type``,
    ``extra.tool_name``, ``extra.tool_use_id``, ``extra.input``.

    We parse these into ``{tool_name, input_hash, input, agent_role, job_name}``
    records. The ``input_hash`` is the full SHA-256 of the ``(tool_name, input)``
    pair — truncating at any length reintroduces the prefix-collapse that
    TASK-3-2 exists to remove (per the plan's HITL resolution).
    """
    import hashlib
    import json

    records: list[dict[str, Any]] = []
    for line in logs.splitlines():
        line = line.strip()
        if not line:
            continue
        # Try JSON parsing first (egg_logging JSON formatter)
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        # Check if this is a tool call log entry
        message = entry.get("message", "")
        extra = entry.get("extra", {})
        if message != "Tool call" or extra.get("event_type") != "tool_use":
            continue
        tool_name = extra.get("tool_name", "")
        input_text = extra.get("input", "")
        # Hash the full (tool_name, input) pair — no truncation
        input_hash = hashlib.sha256(
            f"{tool_name}:{input_text}".encode()
        ).hexdigest()
        records.append(
            {
                "tool_name": tool_name,
                "input_hash": input_hash,
                "input": input_text,
                "agent_role": record.get("agent_role"),
                "job_name": record.get("job_name"),
            }
        )
    return records


def _build_running_agents(
    context: Any,
    pipeline: Any,
    pipeline_id: str,
    phase_value: str,
    lifecycle_owner: str,
) -> tuple[RunningAgent, ...]:
    """Build RunningAgent records with proper role + age fields (TASK-1-5).

    Fixes the bug where ``role`` was set to the container ID instead of the
    agent role. Also populates ``last_tool_call_age_s`` and
    ``last_heartbeat_age_s`` from the health monitor's anchors, which activates
    ``detect_heartbeat_stall()``.
    """
    try:
        from health_monitor import get_health_monitor

        health_monitor = get_health_monitor()
    except Exception:  # noqa: BLE001
        health_monitor = None

    live_ids = getattr(context, "live_container_ids", None) or set()
    if not live_ids:
        return ()

    # Build a lookup from container_id to agent role using the pipeline model.
    role_by_container: dict[str, str] = {}
    try:
        phases = getattr(pipeline, "phases", {}) or {}
        phase_exec = phases.get(phase_value)
        if phase_exec is not None:
            for agent_exec in getattr(phase_exec, "agents", []) or []:
                cid = getattr(agent_exec, "container_id", None)
                role = str(getattr(agent_exec, "role", ""))
                if cid and role:
                    role_by_container[cid] = role
            for ci in getattr(phase_exec, "containers", []) or []:
                cid = getattr(ci, "container_id", None)
                role = getattr(ci, "agent_role", None)
                if cid and role:
                    role_by_container[cid] = str(role)
    except Exception:  # noqa: BLE001 — defensive
        pass

    agents: list[RunningAgent] = []
    now = time.time()
    for cid in live_ids:
        role = role_by_container.get(cid, str(cid))
        # Try to get age fields from the health monitor
        last_tool_call_age: float | None = None
        last_heartbeat_age: float | None = None
        if health_monitor is not None:
            try:
                agent_state = getattr(health_monitor, "_agents", {}).get(role)
                if agent_state is not None:
                    last_hb = getattr(agent_state, "last_heartbeat", None)
                    last_progress = getattr(agent_state, "last_progress", None)
                    if last_hb is not None:
                        last_heartbeat_age = max(0.0, now - float(last_hb))
                    if last_progress is not None:
                        last_tool_call_age = max(0.0, now - float(last_progress))
            except Exception:  # noqa: BLE001 — defensive
                pass

        agents.append(
            RunningAgent(
                role=role,
                state="running",
                lifecycle_owner=lifecycle_owner,
                last_tool_call_age_s=last_tool_call_age,
                last_heartbeat_age_s=last_heartbeat_age,
            )
        )
    return tuple(agents)


__all__ = [
    "DetectionPlane",
    "Detector",
    "EventStreamSnapshot",
    "Finding",
    "FindingClass",
    "LifecycleOwner",
    "Severity",
    "PhaseStallDetector",
    "RunningAgent",
    "default_detection_plane",
    "detect_phase_stall",
    "escalate_findings",
    "snapshot_from_health_context",
]
