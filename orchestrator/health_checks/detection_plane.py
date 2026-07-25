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
    from health_checks.tier1.forward_progress import detect_forward_progress
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
        detect_forward_progress,
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
    doesn't expose yet stay empty (a detector simply won't fire on them).

    Enriches the snapshot with the data sources that starved detectors
    require (#3596): container-to-role mapping (fixing the ``role=str(cid)``
    defect), RunningAgent liveness fields, ``git_state``,
    ``container_transitions``, and ``decision_state``.
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
        "expected_duration_s": _query_expected_duration(pipeline, phase_value),
    }

    live_ids = getattr(context, "live_container_ids", None) or set()

    # Build container_id -> agent_role mapping from the pipeline's phase execution
    # state. Fixes the role=str(cid) defect (#3596): previously the role field
    # carried a container UUID, so any detector keying on role name was matching
    # the wrong thing. Falls back to str(cid) only when the pipeline model is
    # unavailable (best-effort).
    cid_to_role = _build_container_role_map(pipeline, phase_value)

    # Populate RunningAgent liveness fields from the progress store and
    # HealthMonitor. Null when unmeasurable — never 0 (#3596 operator directive).
    progress_ages = _query_progress_ages(pipeline_id)
    heartbeat_ages = _query_heartbeat_ages(pipeline_id)
    container_exit_info = _query_container_exit_info(pipeline, phase_value, live_ids)

    running_agents = tuple(
        _build_running_agent(
            cid,
            lifecycle_owner,
            cid_to_role,
            progress_ages,
            heartbeat_ages,
            container_exit_info,
        )
        for cid in live_ids
    )

    # Enrich with git_state, container_transitions, decision_state, raw.runtime,
    # consensus, and midturn_messages.
    git_state = _build_git_state(context, pipeline, pipeline_id, phase_value, cid_to_role)
    container_transitions = _build_container_transitions(pipeline_id)
    decision_state = _build_decision_state(pipeline_id, pipeline)
    raw = _build_raw_runtime(pipeline_id, pipeline, phase_value)
    consensus = _build_consensus_state(pipeline_id)
    midturn_messages = _build_midturn_messages(pipeline_id)

    # Merge consensus and midturn_messages into raw for forward compatibility.
    if consensus:
        raw.setdefault("consensus", {}).update(consensus)
    if midturn_messages:
        raw["midturn_messages"] = midturn_messages

    snapshot = EventStreamSnapshot(
        snapshot_id=f"{pipeline_id}:{phase_value}",
        pipeline_id=str(pipeline_id),
        phase=str(phase_value),
        running_agents=running_agents,
        phase_state=phase_state,
        consensus=consensus,
        git_state=git_state,
        container_transitions=container_transitions,
        decision_state=decision_state,
        midturn_messages=midturn_messages,
        raw=raw,
    )

    # Attach the pipeline reference for detectors that need to walk the
    # pipeline model (e.g. forward_progress no-commits-at-completion).
    # EventStreamSnapshot is frozen, so use object.__setattr__ to bypass.
    object.__setattr__(snapshot, "_pipeline_ref", pipeline)

    return snapshot


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
# Snapshot enrichment helpers (#3596)
#
# Each helper is best-effort: on any failure it returns an empty/default
# value so a detector simply won't fire on the missing field rather than
# crashing the event loop. The detection plane's "stop crying wolf"
# discipline (#2270 §2) means these must never raise.
# ---------------------------------------------------------------------------


def _build_container_role_map(pipeline: Any, phase_value: str) -> dict[str, str]:
    """Build a ``container_id -> agent_role`` mapping from pipeline state.

    Walks ``pipeline.phases[phase].agents`` and indexes each agent's
    ``container_id`` (or ``container_info.container_id``) to its ``role``.
    Returns an empty dict when the pipeline model is unavailable.
    """
    try:
        phases = getattr(pipeline, "phases", {}) or {}
        phase_exec = phases.get(phase_value)
        if phase_exec is None:
            return {}
        agents = getattr(phase_exec, "agents", []) or []
        mapping: dict[str, str] = {}
        for agent in agents:
            cid = getattr(agent, "container_id", None)
            if cid is None:
                ci = getattr(agent, "container_info", None)
                if ci is not None:
                    cid = getattr(ci, "container_id", None)
            if cid:
                role = str(getattr(agent, "role", ""))
                if role:
                    mapping[str(cid)] = role
        return mapping
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _query_progress_ages(pipeline_id: str) -> dict[str, float]:
    """Return ``{agent_role: seconds_since_last_progress_event}``.

    Queries the in-memory ``ProgressStore`` for the most recent progress event
    per agent role. Returns an empty dict when the store is unavailable.
    """
    try:
        from progress_store import get_progress_store

        store = get_progress_store()
        events = store.get_latest_per_agent(pipeline_id)
        now = time.time()
        result: dict[str, float] = {}
        for event in events:
            ts = event.timestamp
            if ts is None:
                continue
            if hasattr(ts, "timestamp"):
                ts_float = ts.timestamp()
            else:
                ts_float = float(ts)
            result[event.agent_role] = now - ts_float
        return result
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _query_heartbeat_ages(pipeline_id: str) -> dict[str, float]:
    """Return ``{agent_role: seconds_since_last_heartbeat}``.

    Queries the HealthMonitor singleton's ``_last_heartbeat`` dict, but only
    when the singleton is tracking the requested pipeline (the monitor is
    per-pipeline). Returns an empty dict when the monitor is unavailable,
    tracking a different pipeline, or the dict is empty.
    """
    try:
        from health_monitor import get_health_monitor

        monitor = get_health_monitor()
        if monitor is None:
            return {}
        # The HealthMonitor is a per-pipeline singleton; only use it when
        # it's tracking the pipeline we're snapshotting.
        if getattr(monitor, "_pipeline_id", None) != pipeline_id:
            return {}
        now = time.time()
        result: dict[str, float] = {}
        for role, ts in getattr(monitor, "_last_heartbeat", {}).items():
            result[str(role)] = now - float(ts)
        return result
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _query_container_exit_info(
    pipeline: Any, phase_value: str, live_ids: set[str]
) -> dict[str, dict[str, Any]]:
    """Return ``{container_id: {exit_code, exit_reason}}`` for exited containers.

    Reads from the pipeline's phase execution state — agents that have exited
    carry their exit info in ``AgentExecution``. Returns an empty dict when
    unavailable.
    """
    try:
        phases = getattr(pipeline, "phases", {}) or {}
        phase_exec = phases.get(phase_value)
        if phase_exec is None:
            return {}
        agents = getattr(phase_exec, "agents", []) or []
        result: dict[str, dict[str, Any]] = {}
        for agent in agents:
            cid = getattr(agent, "container_id", None)
            if cid is None:
                ci = getattr(agent, "container_info", None)
                if ci is not None:
                    cid = getattr(ci, "container_id", None)
            if cid:
                exit_code = getattr(agent, "exit_code", None)
                exit_reason = getattr(agent, "error", None)
                if exit_code is not None or exit_reason is not None:
                    result[str(cid)] = {
                        "exit_code": exit_code,
                        "exit_reason": exit_reason,
                    }
        return result
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_running_agent(
    container_id: str,
    lifecycle_owner: str,
    cid_to_role: dict[str, str],
    progress_ages: dict[str, float],
    heartbeat_ages: dict[str, float],
    container_exit_info: dict[str, dict[str, Any]],
) -> RunningAgent:
    """Build a single RunningAgent with all available liveness fields.

    Fixes the ``role=str(cid)`` defect: maps the container ID to the agent
    role via the pipeline state. Liveness fields are null when unmeasurable.
    """
    role = cid_to_role.get(str(container_id), str(container_id))
    exit_info = container_exit_info.get(str(container_id), {})

    return RunningAgent(
        role=role,
        state="running",
        lifecycle_owner=lifecycle_owner,
        exit_code=exit_info.get("exit_code"),
        exit_reason=exit_info.get("exit_reason"),
        last_tool_call_age_s=progress_ages.get(role),
        last_heartbeat_age_s=heartbeat_ages.get(role),
    )


def _build_git_state(
    context: Any, pipeline: Any, pipeline_id: str, phase_value: str, cid_to_role: dict[str, str]
) -> dict[str, Any]:
    """Populate ``git_state`` with commit counts and branch info.

    Runs ``git rev-list --count`` per agent worktree to get per-agent commit
    counts, plus branch-level info for divergence detection. Best-effort:
    any git failure degrades to an empty dict.

    Also populates ``agent_prev_commit_counts`` from the module-level
    ``_prev_commit_counts`` tracking dict, which stores the last-seen commit
    count per agent per pipeline across snapshot evaluations.
    """
    try:
        repo_path = getattr(context, "repo_path", None)
        if repo_path is None:
            return {}

        git_state: dict[str, Any] = {}

        # Per-agent commit counts (for forward-progress detector)
        agent_commit_counts: dict[str, int] = {}
        agent_last_commit_age_s: dict[str, float] = {}
        for _cid, role in cid_to_role.items():
            count, last_commit_ts = _count_commits_and_last_age(repo_path, pipeline, phase_value, role)
            if count is not None:
                agent_commit_counts[role] = count
            if last_commit_ts is not None:
                agent_last_commit_age_s[role] = time.time() - last_commit_ts
        if agent_commit_counts:
            git_state["agent_commit_counts"] = agent_commit_counts
        if agent_last_commit_age_s:
            git_state["agent_last_commit_age_s"] = agent_last_commit_age_s

        # Previous commit counts (for forward-progress reset detection)
        prev_counts = _prev_commit_counts.get(pipeline_id, {})
        if prev_counts:
            git_state["agent_prev_commit_counts"] = dict(prev_counts)

        # Update the tracking dict for the next snapshot
        _prev_commit_counts[pipeline_id] = dict(agent_commit_counts)

        # Branch-level info (for worktree_branch detectors)
        branch_info = _query_branch_git_state(repo_path, pipeline, phase_value)
        git_state.update(branch_info)

        return git_state
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _count_commits_and_last_age(
    repo_path: Any, pipeline: Any, phase_value: str, role: str
) -> tuple[int | None, float | None]:
    """Count commits and get the age of the last commit for an agent's worktree.

    Returns ``(commit_count, last_commit_ts)`` where ``last_commit_ts`` is the
    Unix epoch timestamp of the most recent commit. Either value may be None
    if it cannot be determined.
    """
    try:
        import subprocess

        # Resolve the worktree path for this agent's role
        worktree_path = _resolve_agent_worktree(repo_path, pipeline, phase_value, role)
        if worktree_path is None:
            return None, None

        base_ref = _resolve_base_ref(pipeline, worktree_path)

        # Commit count
        count_result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        count = None
        if count_result.returncode == 0:
            count = int(count_result.stdout.strip())

        # Last commit timestamp (Unix epoch)
        ts_result = subprocess.run(
            ["git", "log", "-1", "--format=%at"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        last_commit_ts = None
        if ts_result.returncode == 0 and ts_result.stdout.strip():
            last_commit_ts = float(ts_result.stdout.strip())

        return count, last_commit_ts
    except Exception:  # noqa: BLE001 — defensive
        pass
    return None, None


def _resolve_agent_worktree(
    repo_path: Any, pipeline: Any, phase_value: str, role: str
) -> Any:
    """Resolve the worktree path for a specific agent role.

    Under orchestrator-owned spawning, each agent gets a worktree under
    ``<repo_path>/.egg-state/worktrees/<role>``. Falls back to the main
    repo path when the worktree doesn't exist.
    """
    try:
        from pathlib import Path

        rp = Path(str(repo_path))
        # Try the standard worktree path
        worktree = rp / ".egg-state" / "worktrees" / role
        if worktree.exists():
            return worktree
        # Try repo-name-prefixed path (for multi-repo pipelines)
        repo_name = getattr(pipeline, "repo", "")
        if repo_name and "/" in repo_name:
            repo_short = repo_name.split("/")[-1]
            worktree = rp / repo_short / ".egg-state" / "worktrees" / role
            if worktree.exists():
                return worktree
        # Fall back to the repo path itself
        if rp.exists():
            return rp
    except Exception:  # noqa: BLE001 — defensive
        pass
    return None


def _resolve_base_ref(pipeline: Any, worktree_path: Any) -> str:
    """Resolve the ``origin/<branch>`` ref for commit counting."""
    try:
        import subprocess

        base = getattr(pipeline, "base_branch", None)
        if isinstance(base, str) and base.strip():
            return f"origin/{base.strip()}"

        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        ref = result.stdout.strip() if result.returncode == 0 else ""
        if ref:
            return ref
    except Exception:  # noqa: BLE001 — defensive
        pass
    return "origin/main"


def _query_branch_git_state(
    repo_path: Any, pipeline: Any, phase_value: str
) -> dict[str, Any]:
    """Query branch-level git state for divergence/corruption detectors."""
    try:
        import subprocess

        # Resolve the worktree path
        worktree = _resolve_agent_worktree(repo_path, pipeline, phase_value, "")
        if worktree is None:
            return {}

        result: dict[str, Any] = {}

        # Branch name
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if branch.returncode == 0:
            result["branch"] = branch.stdout.strip()

        # Commit count beyond base
        base_ref = _resolve_base_ref(pipeline, worktree)
        count = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if count.returncode == 0:
            result["commit_count"] = int(count.stdout.strip())

        # Last commit SHA and timestamp
        last_commit = subprocess.run(
            ["git", "log", "-1", "--format=%H|%aI"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if last_commit.returncode == 0 and "|" in last_commit.stdout:
            parts = last_commit.stdout.strip().split("|", 1)
            result["last_commit_sha"] = parts[0]
            result["last_commit_at"] = parts[1] if len(parts) > 1 else None

        # fsck errors (worktree corruption)
        fsck = subprocess.run(
            ["git", "fsck", "--full"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if fsck.returncode != 0:
            # Count error lines
            errors = [line for line in fsck.stderr.splitlines() if line.strip()]
            result["fsck_errors"] = len(errors)

        # Index lock check
        import os

        index_lock = os.path.join(str(worktree), ".git", "index.lock")
        if os.path.exists(index_lock):
            result["index_lock_present"] = True
            result["lock_age_s"] = time.time() - os.path.getmtime(index_lock)
        else:
            result["index_lock_present"] = False

        return result
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_container_transitions(pipeline_id: str) -> tuple[dict[str, Any], ...]:
    """Populate ``container_transitions`` from kubernetes_monitor's event history.

    Returns a tuple of transition dicts:
    ``{container, role, from, to, reason, exit_code, restart_count, transient, timestamp}``

    NOTE: The kubernetes_monitor currently tracks only the *current* pod state
    (``_pod_states``), not a transition history. The container death / restart-loop
    detectors require a history of from→to transitions to function. Until the
    monitor is enhanced to track transitions, this returns an empty tuple — the
    detectors degrade gracefully to "no finding" rather than crashing.
    """
    # The kubernetes_monitor does not currently maintain a transition history.
    # Returning () is the correct best-effort result: detectors that read
    # container_transitions simply won't fire on this snapshot.
    return ()


def _build_decision_state(pipeline_id: str, pipeline: Any) -> dict[str, Any]:
    """Populate ``decision_state`` from the pipeline's decision list.

    Returns a dict with: ``pending_hitl``, ``open_decisions``,
    ``approved_unapplied``, ``oldest_open_age_s``, ``replay_pending``,
    ``replay_count``.

    Reads from ``pipeline.decisions`` — the same source the PhaseStallDetector
    and other checks consult. Best-effort: any failure degrades to empty dict.
    """
    try:
        decisions = getattr(pipeline, "decisions", []) or []
        if not decisions:
            return {}

        now = time.time()

        # Pending HITL decisions (status == PENDING)
        pending_hitl = [d for d in decisions if str(getattr(d, "status", "")).upper() == "PENDING"]
        open_decisions = list(pending_hitl)

        # Oldest open decision age
        oldest_age_s = None
        for d in open_decisions:
            created = getattr(d, "created_at", None)
            if created is None:
                continue
            if hasattr(created, "timestamp"):
                age = now - created.timestamp()
            elif isinstance(created, (int, float)):
                age = now - float(created)
            else:
                continue
            if oldest_age_s is None or age > oldest_age_s:
                oldest_age_s = age

        # Approved but unapplied (resolved decisions)
        approved_unapplied = []
        for d in decisions:
            status = str(getattr(d, "status", "")).upper()
            if status == "RESOLVED":
                resolved_at = getattr(d, "resolved_at", None)
                if resolved_at is not None:
                    if hasattr(resolved_at, "timestamp"):
                        age = now - resolved_at.timestamp()
                    elif isinstance(resolved_at, (int, float)):
                        age = now - float(resolved_at)
                    else:
                        age = 0.0
                    approved_unapplied.append({
                        "id": str(getattr(d, "id", "")),
                        "age_s": age,
                    })

        return {
            "pending_hitl": len(pending_hitl) > 0,
            "open_decisions": len(open_decisions),
            "approved_unapplied": approved_unapplied,
            "oldest_open_age_s": oldest_age_s,
            "replay_pending": False,  # Not tracked on pipeline model
            "replay_count": 0,  # Not tracked on pipeline model
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_consensus_state(pipeline_id: str) -> dict[str, Any]:
    """Populate ``consensus`` from the PeerConsensusTracker.

    Returns a dict with: ``blocking_agents``, ``is_complete``,
    ``nack_cycles``, ``latest_proposal_age_s``, ``has_proposed``,
    ``has_confirmed``. Best-effort: failures degrade to empty dict.
    """
    try:
        from peer_consensus import get_peer_consensus_tracker

        tracker = get_peer_consensus_tracker(pipeline_id)
        if tracker is None:
            return {}

        evaluation = tracker.evaluate()
        result: dict[str, Any] = {
            "blocking_agents": evaluation.get("blocking_agents", []),
            "is_complete": evaluation.get("is_complete", False),
            "nack_cycles": evaluation.get("nack_cycles", 0),
        }

        # Latest proposal age
        latest_proposal = tracker.get_latest_proposal_timestamp()
        if latest_proposal is not None:
            from datetime import UTC, datetime

            if isinstance(latest_proposal, datetime):
                if latest_proposal.tzinfo is None:
                    latest_proposal = latest_proposal.replace(tzinfo=UTC)
                result["latest_proposal_age_s"] = (
                    datetime.now(UTC) - latest_proposal
                ).total_seconds()

        # Has proposed / has confirmed
        result["has_proposed"] = len(evaluation.get("blocking_agents", [])) > 0 or result["is_complete"]
        result["has_confirmed"] = result["is_complete"]

        return result
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_midturn_messages(pipeline_id: str) -> tuple[dict[str, Any], ...]:
    """Populate ``midturn_messages`` from the message store.

    Returns a tuple of message dicts with ``from_role``, ``message_type``,
    ``subject``, and ``timestamp``. Limited to the most recent 100 messages
    to keep the snapshot size bounded. Best-effort: failures degrade to
    empty tuple.
    """
    try:
        from message_store import get_message_store

        store = get_message_store()
        if store is None:
            return ()

        messages = store.get_messages(pipeline_id, limit=100)
        result: list[dict[str, Any]] = []
        for msg in messages:
            result.append({
                "from_role": str(getattr(msg, "from_role", "")),
                "to_role": str(getattr(msg, "to_role", "")),
                "message_type": str(getattr(msg, "message_type", "")),
                "subject": str(getattr(msg, "subject", "")),
                "timestamp": getattr(msg, "timestamp", None),
            })
        return tuple(result)
    except Exception:  # noqa: BLE001 — defensive
        return ()


# Module-level tracking for previous commit counts across snapshots.
# Keyed by pipeline_id, stores {role: commit_count} from the last evaluation.
_prev_commit_counts: dict[str, dict[str, int]] = {}


def _reset_prev_commit_counts(pipeline_id: str) -> None:
    """Reset previous commit count tracking for a pipeline."""
    _prev_commit_counts.pop(pipeline_id, None)


# Default expected phase durations (seconds) when no config is available.
# Used by detect_duration_drift to compute drift_ratio.
_DEFAULT_PHASE_DURATIONS_S: dict[str, float] = {
    "refine": 600.0,       # 10 minutes
    "plan": 900.0,         # 15 minutes
    "apply": 300.0,        # 5 minutes
    "implement": 3600.0,   # 1 hour
}


def _query_expected_duration(pipeline: Any, phase_value: str) -> float | None:
    """Query the expected duration for a phase from the pipeline config.

    Falls back to phase-specific defaults when the config is unavailable.
    Returns None when the phase is unknown.
    """
    try:
        config = getattr(pipeline, "config", None)
        if config is not None:
            # Check for a per-phase duration override
            duration = getattr(config, f"{phase_value}_expected_duration_s", None)
            if duration is not None:
                return float(duration)
        # Fall back to defaults
        return _DEFAULT_PHASE_DURATIONS_S.get(phase_value)
    except Exception:  # noqa: BLE001 — defensive
        return _DEFAULT_PHASE_DURATIONS_S.get(phase_value)


def _build_raw_runtime(pipeline_id: str, pipeline: Any, phase_value: str) -> dict[str, Any]:
    """Populate ``raw.runtime`` with driver-liveness signals (#3540).

    Fields:
    - ``run_pipeline_thread_alive``: whether the driver thread is registered
    - ``thread_last_tick_age_s``: seconds since the last driver heartbeat
    - ``spawn_age_s``: seconds since the last agent spawn
    """
    try:
        import driver_heartbeat

        raw: dict[str, Any] = {}

        tick_age = driver_heartbeat.tick_age_seconds(pipeline_id)
        if tick_age is not None:
            raw["run_pipeline_thread_alive"] = True
            raw["thread_last_tick_age_s"] = tick_age
        else:
            raw["run_pipeline_thread_alive"] = False
            raw["thread_last_tick_age_s"] = None

        spawn_age = driver_heartbeat.spawn_age_seconds(pipeline_id)
        if spawn_age is not None:
            raw["spawn_age_s"] = spawn_age

        return {"runtime": raw}
    except Exception:  # noqa: BLE001 — defensive
        return {}


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
