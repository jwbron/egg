"""Orchestrator-runtime liveness detectors (#2270 §-core, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM; only a genuinely ambiguous + high-stakes finding
(an orchestrator-thread death needing human/LLM judgement) escalates.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``phase_state`` and ``container_transitions`` fields:

* :func:`detect_run_pipeline_thread_liveness` — the orchestrator ``_run_pipeline``
  driver thread is dead/hung while the phase is still RUNNING (#2234/#3233/#2219).
* :func:`detect_duration_drift` — a phase running far past its expected budget.
* :func:`detect_agent_restart_propagation` — a requested agent restart that never
  propagated to a respawned container.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.types import Finding, Severity

# Finding-class strings. Emitted as plain strings (the detection plane matches a
# detector's output structurally on the raw string, so slice-8 may name classes
# beyond the pinned ``FindingClass`` enum — see health_checks/types.py).
FINDING_RUN_PIPELINE_THREAD_DEAD = "runtime_thread_dead"
FINDING_DURATION_DRIFT = "duration_drift"
FINDING_AGENT_RESTART_PROPAGATION = "agent_restart_propagation"

# The phase-state status that means the orchestrator believes work is in flight.
_RUNNING_STATUS = "RUNNING"
_RUNNING_STATE = "Running"

# Default grace, in seconds, before a silent ``_run_pipeline`` driver thread is
# treated as dead/hung rather than merely between heartbeats.
_DEFAULT_RUN_LOOP_GRACE_S = 300.0
# Default multiple of a phase's expected duration before drift is worth noting:
# fire once a phase has run more than 2× its expected budget (drift_ratio > 2).
_DEFAULT_DURATION_DRIFT_FACTOR = 2.0
# Default deadline, in seconds, for a requested restart to propagate to a
# respawned container before it is treated as never having propagated.
_DEFAULT_RESTART_PROPAGATION_DEADLINE_S = 300.0


def _phase_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "phase_state", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _transitions(snapshot: Any) -> list[dict[str, Any]]:
    raw = getattr(snapshot, "container_transitions", ()) or ()
    return [t for t in raw if isinstance(t, dict)]


def _runtime(snapshot: Any) -> dict[str, Any]:
    """The ``runtime`` section of the snapshot (carried in ``raw``)."""
    raw = getattr(snapshot, "raw", {}) or {}
    section = raw.get("runtime", {}) if isinstance(raw, dict) else {}
    return section if isinstance(section, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce ``value`` to ``float`` defensively; ``None`` on non-numerics."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_run_pipeline_thread_liveness(
    snapshot: Any,
    *,
    grace_s: float = _DEFAULT_RUN_LOOP_GRACE_S,
) -> Finding | None:
    """Fire when the ``_run_pipeline`` driver thread is dead/hung mid-phase.

    The defect (#2234/#3233/#2219): the orchestrator's ``_run_pipeline`` driver
    thread dies or hangs while ``phase_state.status`` still reads ``RUNNING``,
    so the phase silently wedges — no agent makes progress and nothing tears the
    pipeline down. The signature is a RUNNING phase whose run-loop liveness flag
    has flipped false, or whose run-loop heartbeat has aged past ``grace_s``.

    This is the one detector in the module that escalates: a dead orchestrator
    thread is high-stakes and the false-positive cost (killing a healthy
    pipeline) is severe, so the verdict is genuinely ambiguous →
    ``requires_adjudication=True``.

    No-fire when ``run_loop_alive`` is truthy, the heartbeat is recent, the phase
    is not RUNNING, or no liveness signal is present at all.
    """
    alive = _runtime(snapshot).get("run_pipeline_thread_alive")
    tick_age_s = _as_float(_runtime(snapshot).get("thread_last_tick_age_s"))

    # Require an actual liveness signal — absence is not evidence of death.
    explicitly_dead = alive is False
    tick_stale = tick_age_s is not None and tick_age_s > grace_s
    if not (explicitly_dead or tick_stale):
        return None

    return Finding(
        finding_class=FINDING_RUN_PIPELINE_THREAD_DEAD,
        severity=Severity.HIGH,
        evidence={
            "phase": getattr(snapshot, "phase", None),
            "run_pipeline_thread_alive": alive,
            "thread_last_tick_age_s": tick_age_s,
            "grace_s": grace_s,
        },
        recommended_action=(
            "The orchestrator _run_pipeline driver thread appears dead or hung "
            "(#2234/#3233/#2219). Escalate to an operator/overseer to confirm the "
            "thread is gone before tearing down or restarting the pipeline — a "
            "false positive kills a healthy run."
        ),
        requires_adjudication=True,
        detector_key="runtime_thread_liveness",
    )


detect_run_pipeline_thread_liveness.detector_key = "runtime_thread_liveness"  # type: ignore[attr-defined]
detect_run_pipeline_thread_liveness.name = "run_pipeline_thread_liveness_detector"  # type: ignore[attr-defined]


def detect_duration_drift(
    snapshot: Any,
    *,
    factor: float = _DEFAULT_DURATION_DRIFT_FACTOR,
) -> Finding | None:
    """Fire when a RUNNING phase has run far past its expected budget.

    A phase whose elapsed runtime (``started_age_s``) exceeds its declared
    ``expected_duration_s`` by more than ``factor`` is drifting — likely wedged
    or pathologically slow. Conservative by design: it stays silent unless both
    a positive expected budget and an elapsed age are present and the multiple
    is clearly exceeded.

    Deterministic → ``requires_adjudication=False``.
    """
    state = _phase_state(snapshot)
    if str(state.get("status", "")) != _RUNNING_STATUS:
        return None

    started_age_s = _as_float(state.get("started_age_s"))
    expected_duration_s = _as_float(state.get("expected_duration_s"))
    # Prefer an explicit drift_ratio; else derive it from elapsed/expected.
    ratio = _as_float(state.get("drift_ratio"))
    if ratio is None:
        if (
            started_age_s is None
            or expected_duration_s is None
            or expected_duration_s <= 0
        ):
            return None
        ratio = started_age_s / expected_duration_s
    # Fire only when the phase is over budget by more than ``factor``×.
    if ratio <= factor:
        return None

    return Finding(
        finding_class=FINDING_DURATION_DRIFT,
        severity=Severity.LOW,
        evidence={
            "phase": getattr(snapshot, "phase", None),
            "started_age_s": started_age_s,
            "expected_duration_s": expected_duration_s,
            "drift_ratio": ratio,
            "factor": factor,
        },
        recommended_action=(
            "The phase has run well past its expected duration budget "
            "(started_age_s > expected_duration_s * factor). Inspect for a "
            "wedge or a pathologically slow agent before extending the budget."
        ),
        requires_adjudication=False,
        detector_key="duration_drift",
    )


detect_duration_drift.detector_key = "duration_drift"  # type: ignore[attr-defined]
detect_duration_drift.name = "duration_drift_detector"  # type: ignore[attr-defined]


def detect_agent_restart_propagation(
    snapshot: Any,
    *,
    deadline_s: float = _DEFAULT_RESTART_PROPAGATION_DEADLINE_S,
) -> Finding | None:
    """Fire when a requested agent restart never propagated to a respawn.

    A restart was requested (``restart_requested_age_s`` set) and has now aged
    past ``deadline_s``, yet no container transitioned back to ``Running`` for
    the restarted role — the restart request was dropped on the floor. When
    ``restart_role`` is present, only a transition whose ``container`` starts
    with that role counts as propagation; otherwise any ``to == "Running"``
    transition counts.

    Deterministic → ``requires_adjudication=False``.
    """
    # Primary signal: the orchestrator runtime reports the restart-propagation
    # deadline exceeded.
    prop = _runtime(snapshot).get("restart_propagation", {})
    prop = prop if isinstance(prop, dict) else {}
    deadline_exceeded = bool(prop.get("deadline_exceeded"))

    age_s = _as_float(prop.get("age_s"))
    runtime_deadline_s = _as_float(prop.get("deadline_s"))

    if not deadline_exceeded:
        # Legacy fallback: a phase-state restart-request age past the deadline
        # with no container transition back to Running for the restarted role.
        state = _phase_state(snapshot)
        requested_age_s = _as_float(state.get("restart_requested_age_s"))
        if requested_age_s is None or requested_age_s <= deadline_s:
            return None
        restart_role = state.get("restart_role")
        role_prefix = str(restart_role) if restart_role else None
        for t in _transitions(snapshot):
            if str(t.get("to", "")) != _RUNNING_STATE:
                continue
            if role_prefix is None or str(t.get("container", "") or "").startswith(
                role_prefix
            ):
                return None
        age_s = requested_age_s
        runtime_deadline_s = deadline_s

    return Finding(
        finding_class=FINDING_AGENT_RESTART_PROPAGATION,
        severity=Severity.MEDIUM,
        evidence={
            "phase": getattr(snapshot, "phase", None),
            "age_s": age_s,
            "deadline_s": runtime_deadline_s,
            "propagated": False,
        },
        recommended_action=(
            "A requested agent restart never propagated to a respawned "
            "container (no transition back to Running past the deadline). "
            "Re-issue the restart or respawn the role's container directly; "
            "the request was likely dropped."
        ),
        requires_adjudication=False,
        detector_key="agent_restart_propagation",
    )


detect_agent_restart_propagation.detector_key = "agent_restart_propagation"  # type: ignore[attr-defined]
detect_agent_restart_propagation.name = "agent_restart_propagation_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_agent_restart_propagation",
    "detect_duration_drift",
    "detect_run_pipeline_thread_liveness",
]
