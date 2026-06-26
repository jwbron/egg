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

    def register(self, detector: Detector) -> None:
        """Register a detector. Later registration order is preserved."""
        self._detectors.append(detector)
        logger.debug("Detector registered", detector_key=detector.detector_key)

    @property
    def detectors(self) -> list[Detector]:
        """All registered detectors, in registration order."""
        return list(self._detectors)

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


# ---------------------------------------------------------------------------
# Plane factory + live-state snapshot builder.
# ---------------------------------------------------------------------------


def default_detection_plane() -> DetectionPlane:
    """Build the detection plane with the detectors delivered so far.

    Slice-4 registers :class:`PhaseStallDetector`. Slices 7 and 8 append their
    detectors here as they land.
    """
    plane = DetectionPlane()
    plane.register(PhaseStallDetector())
    return plane


def snapshot_from_health_context(context: Any) -> EventStreamSnapshot:
    """Build an :class:`EventStreamSnapshot` from a ``PipelineHealthContext``.

    Best-effort and defensive: the detection plane runs on the event loop and
    must never crash on a partially-populated context. Fields the live state
    doesn't expose yet stay empty (a detector simply won't fire on them); slices
    7/8 enrich this builder as their detectors need more signal.
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

    live_ids = getattr(context, "live_container_ids", None) or set()
    running_agents = tuple(
        RunningAgent(role=str(cid), state="running", lifecycle_owner=lifecycle_owner)
        for cid in live_ids
    )

    return EventStreamSnapshot(
        snapshot_id=f"{pipeline_id}:{phase_value}",
        pipeline_id=str(pipeline_id),
        phase=str(phase_value),
        running_agents=running_agents,
        phase_state=phase_state,
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


__all__ = [
    "DetectionPlane",
    "Detector",
    "EventStreamSnapshot",
    "LifecycleOwner",
    "PhaseStallDetector",
    "RunningAgent",
    "default_detection_plane",
    "snapshot_from_health_context",
]
