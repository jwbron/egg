"""Container / Kubernetes coverage-gap detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``container_transitions`` and ``running_agents``
fields of the snapshot:

* :func:`detect_container_death` — a genuinely dead producer container
  (CrashLoopBackOff / fatal exit with no successful reschedule), with the
  #2948 transient-eviction-vs-permanent-death disambiguation.
* :func:`detect_overseer_self_injection` — the §1 self-injection loop: an
  overseer that refuses its own bootstrap, exits, and is respawned each cycle.
* :func:`detect_repeated_role_restarts` — the same role restarting repeatedly
  without stabilising (a crash-loop that is not (yet) a single fatal death).
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
FINDING_CONTAINER_DEATH = "container_death"
FINDING_OVERSEER_SELF_INJECTION = "overseer_self_injection"
FINDING_CONTAINER_RESTART_LOOP = "container_restart_loop"
FINDING_CONTAINER_OOM_EVICTED = "container_oom_evicted"

# Reasons that indicate an OOM kill or kubelet eviction (the #2948 case).
_OOM_EVICT_REASONS = frozenset({"OOMKilled", "Evicted"})

# Container states that, paired with a crash reason, indicate a death rather
# than an orderly completion.
_DEATH_STATES = frozenset({"Terminated", "Waiting"})
# Reasons that indicate a crash / fatal death (NOT an orderly "Completed" or a
# transient "Evicted" that immediately reschedules).
_DEATH_REASONS = frozenset(
    {"OOMKilled", "CrashLoopBackOff", "Error", "DeadlineExceeded", "BackOff"}
)
# A non-zero, non-None exit code is fatal (exit 0 is a clean exit — the
# self-injection loop exits 0 and is caught by its own detector instead).
_RUNNING_STATE = "Running"

# Default number of restarts of one role before a non-fatal crash-loop is itself
# worth surfacing. Conservative so ordinary one-shot respawn churn stays silent.
_DEFAULT_RESTART_LOOP_THRESHOLD = 3


def _transitions(snapshot: Any) -> list[dict[str, Any]]:
    raw = getattr(snapshot, "container_transitions", ()) or ()
    return [t for t in raw if isinstance(t, dict)]


def _running_agents(snapshot: Any) -> list[Any]:
    return list(getattr(snapshot, "running_agents", ()) or ())


def detect_container_death(snapshot: Any) -> Finding | None:
    """Fire on a genuinely dead container; stay silent on transient eviction.

    The #2948 disambiguation is the whole point: a pod that is transiently
    evicted by the kubelet and immediately rescheduled back to ``Running`` is
    normal churn, **not** a death. A container is *dead* when it reaches a crash
    state (``Terminated`` / ``Waiting`` with a fatal reason such as
    ``OOMKilled`` / ``CrashLoopBackOff``) — or an agent exits with a non-zero
    code — and there is **no subsequent successful reschedule** back to
    ``Running``.

    Deterministic → ``requires_adjudication=False``.
    """
    transitions = _transitions(snapshot)

    # Index of the last fatal-death transition (non-transient crash state).
    last_fatal = -1
    fatal_reason = None
    fatal_container = None
    for idx, t in enumerate(transitions):
        if t.get("transient"):
            continue
        to_state = str(t.get("to", ""))
        reason = str(t.get("reason", ""))
        if to_state in _DEATH_STATES and reason in _DEATH_REASONS:
            last_fatal = idx
            fatal_reason = reason
            fatal_container = t.get("container")

    # An agent that has EXITED with a fatal (non-zero) code is corroborating
    # evidence — but a clean exit (code 0) is NOT a death here (the overseer
    # self-injection loop exits 0; that path has its own detector).
    fatal_exit_agent = None
    for agent in _running_agents(snapshot):
        code = getattr(agent, "exit_code", None)
        if code is not None and int(code) != 0:
            fatal_exit_agent = agent
            break

    if last_fatal < 0 and fatal_exit_agent is None:
        return None

    # A successful reschedule back to Running AFTER the last fatal transition
    # means the container recovered — not a permanent death.
    rescheduled = any(
        str(t.get("to", "")) == _RUNNING_STATE for t in transitions[last_fatal + 1 :]
    )
    if rescheduled:
        return None

    # If we only have a fatal exit code but no crash transition, still require
    # that nothing rescheduled the role back to Running.
    if last_fatal < 0 and fatal_exit_agent is not None:
        if any(str(t.get("to", "")) == _RUNNING_STATE for t in transitions):
            return None

    role = getattr(fatal_exit_agent, "role", None) if fatal_exit_agent else None
    exit_code = getattr(fatal_exit_agent, "exit_code", None) if fatal_exit_agent else None
    exit_reason = (
        getattr(fatal_exit_agent, "exit_reason", None) if fatal_exit_agent else None
    )
    restart_count = transitions[last_fatal].get("restart_count") if last_fatal >= 0 else None

    return Finding(
        finding_class=FINDING_CONTAINER_DEATH,
        severity=Severity.HIGH,
        evidence={
            "container": fatal_container,
            "role": role,
            "fatal_reason": fatal_reason or exit_reason,
            "exit_code": exit_code,
            "restart_count": restart_count,
            "rescheduled": False,
        },
        recommended_action=(
            "A producer container is genuinely dead (crash/fatal exit with no "
            "reschedule back to Running). Respawn the cohort, or open an "
            "operator HITL if the crash repeats — distinct from a transient "
            "eviction that reschedules (#2948)."
        ),
        requires_adjudication=False,
        detector_key="container_death",
    )


detect_container_death.detector_key = "container_death"  # type: ignore[attr-defined]
detect_container_death.name = "container_death_detector"  # type: ignore[attr-defined]


def detect_overseer_self_injection(snapshot: Any) -> Finding | None:
    """Fire on the §1 overseer self-injection refuse-exit-respawn loop.

    The defect (#2270 §1 / #3064): a Sonnet overseer mis-classifies its own
    legitimate bootstrap prompt as a prompt-injection attack, refuses, exits
    cleanly (code 0), is respawned, and broadcasts a security-flavoured alert
    each cycle. The signature is an overseer agent carrying
    ``exit_reason == "self_injection_refusal"`` together with a climbing
    restart count across the overseer's container transitions.

    A healthy overseer bootstrap (no refusal reason, flat restart count) yields
    ``None``. Deterministic → ``requires_adjudication=False``.
    """
    overseer = None
    for agent in _running_agents(snapshot):
        if str(getattr(agent, "role", "")) == "overseer":
            overseer = agent
            break
    if overseer is None:
        return None

    if str(getattr(overseer, "exit_reason", "") or "") != "self_injection_refusal":
        return None

    # Corroborate with the respawn signature: the highest restart_count across
    # overseer container transitions. Not strictly required (the refusal reason
    # is the discriminator) but recorded as evidence and used to stay silent on
    # a single clean bootstrap.
    transitions = _transitions(snapshot)
    overseer_restarts = [
        int(t.get("restart_count", 0) or 0)
        for t in transitions
        if str(t.get("container", "")).startswith("overseer")
    ]
    max_restarts = max(overseer_restarts) if overseer_restarts else 0

    return Finding(
        finding_class=FINDING_OVERSEER_SELF_INJECTION,
        severity=Severity.HIGH,
        evidence={
            "role": "overseer",
            "exit_reason": "self_injection_refusal",
            "exit_code": getattr(overseer, "exit_code", None),
            "max_restart_count": max_restarts,
        },
        recommended_action=(
            "The overseer is refusing its own bootstrap as a prompt-injection "
            "attack and looping refuse->exit->respawn (#2270 §1). Run the "
            "overseer decision tier on Opus and deliver its instructions via "
            "tools/prompt rather than a baked-in script; do not respawn blindly."
        ),
        requires_adjudication=False,
        detector_key="overseer_self_injection",
    )


detect_overseer_self_injection.detector_key = "overseer_self_injection"  # type: ignore[attr-defined]
detect_overseer_self_injection.name = "overseer_self_injection_detector"  # type: ignore[attr-defined]


def detect_container_restart_loop(
    snapshot: Any,
    *,
    threshold: int = _DEFAULT_RESTART_LOOP_THRESHOLD,
) -> Finding | None:
    """Fire when one role restarts repeatedly (crash-loop).

    Distinct from :func:`detect_container_death` (a single permanent death):
    this catches a *crash-loop* — the same role's container restart count
    climbing to/past ``threshold``. Single-shot respawn churn (restart_count
    below the threshold) stays silent. A crash-loop's right correction is not
    obvious from the snapshot (respawn-again can deepen the loop), so the verdict
    escalates → ``requires_adjudication=True``.
    """
    transitions = _transitions(snapshot)
    if not transitions:
        return None

    # Group restart counts by a stable role/container key.
    worst_key = None
    worst_restart = 0
    for t in transitions:
        if t.get("transient"):
            continue
        key = str(t.get("container", "") or "")
        restart = int(t.get("restart_count", 0) or 0)
        if restart > worst_restart:
            worst_restart = restart
            worst_key = key

    if worst_restart < threshold:
        return None

    return Finding(
        finding_class=FINDING_CONTAINER_RESTART_LOOP,
        severity=Severity.HIGH,
        evidence={
            "container": worst_key,
            "restart_count": worst_restart,
            "threshold": threshold,
        },
        recommended_action=(
            "A role's container is restarting repeatedly (crash-loop) at/past "
            "the threshold. Adjudicate the crash cause before respawning again; "
            "a blind respawn can deepen the loop."
        ),
        requires_adjudication=True,
        detector_key="container_restart_loop",
    )


detect_container_restart_loop.detector_key = "container_restart_loop"  # type: ignore[attr-defined]
detect_container_restart_loop.name = "container_restart_loop_detector"  # type: ignore[attr-defined]


def detect_container_oom_evicted(snapshot: Any) -> Finding | None:
    """Fire on an OOM-kill / kubelet eviction that did NOT recover.

    The #2948 disambiguation, lighter-weight than :func:`detect_container_death`:
    a transition whose reason is ``OOMKilled`` / ``Evicted`` and whose
    ``recovered`` flag is explicitly ``False`` is a real resource-pressure death.
    A recovered eviction (``recovered`` truthy) — or one with no explicit
    ``recovered`` flag — stays silent, so transient evict→reschedule churn does
    not cry wolf.

    Deterministic → ``requires_adjudication=False``.
    """
    for t in _transitions(snapshot):
        if str(t.get("reason", "")) not in _OOM_EVICT_REASONS:
            continue
        if t.get("recovered") is not False:
            continue
        return Finding(
            finding_class=FINDING_CONTAINER_OOM_EVICTED,
            severity=Severity.MEDIUM,
            evidence={
                "container": t.get("container"),
                "role": t.get("role"),
                "reason": t.get("reason"),
                "restart_count": t.get("restart_count"),
                "recovered": False,
            },
            recommended_action=(
                "A container was OOM-killed / evicted and did not recover "
                "(#2948). Increase the resource request or reduce memory "
                "pressure before respawning; distinct from a transient eviction "
                "that reschedules."
            ),
            requires_adjudication=False,
            detector_key="container_oom_evicted",
        )
    return None


detect_container_oom_evicted.detector_key = "container_oom_evicted"  # type: ignore[attr-defined]
detect_container_oom_evicted.name = "container_oom_evicted_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_container_death",
    "detect_container_oom_evicted",
    "detect_container_restart_loop",
    "detect_overseer_self_injection",
]
