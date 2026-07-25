"""Bounded, thread-safe history of container state transitions (#3596).

The detection plane's container-lifecycle detectors — ``detect_container_death``,
``detect_container_oom_evicted``, ``detect_container_restart_loop``,
``detect_overseer_self_injection`` and the legacy fallback in
``detect_agent_restart_propagation`` — evaluate a *history* of ``from → to``
transitions. :class:`KubernetesMonitor` only keeps the **current** pod state in
``_pod_states``, so before this module the four detectors were structurally
starved: ``container_transitions`` was always an empty tuple.

This module is the in-process ring buffer between the two. ``KubernetesMonitor.
_check_pod`` calls :func:`record_transition` for every state change it observes,
and ``health_checks.detection_plane._build_container_transitions`` calls
:func:`transitions_for` with the pipeline id. It is module-level (mirroring
:mod:`driver_heartbeat`) so the snapshot builder does not have to reach into a
monitor singleton.

Two derived fields are computed at **read** time rather than stored, so they are
always consistent with the whole retained history:

``restart_count``
    How many times the record's container had already transitioned *into*
    ``Running`` before this record. ``detect_container_restart_loop`` groups on
    the ``container`` key, so that key is the agent **role** when known.

``recovered``
    Whether any later transition for the same container went back to
    ``Running``. ``detect_container_oom_evicted`` only fires when this is
    explicitly ``False``, which is the #2948 transient-eviction carve-out.

In-process only and bounded: an orchestrator restart loses the history, which
means the lifecycle detectors stay silent for a beat rather than firing on a
partial view. That is the intended "stop crying wolf" bias (#2270 §2).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

# Retained transitions across all pipelines. Agent pods transition a handful of
# times each and one-shot BRC agents are short-lived, so this holds well over an
# hour of history for a busy multi-pipeline orchestrator.
MAX_TRANSITIONS = 1000

_RUNNING_STATE = "Running"

_lock = threading.Lock()
_transitions: deque[dict[str, Any]] = deque(maxlen=MAX_TRANSITIONS)


def record_transition(
    *,
    pod_id: str,
    pipeline_id: str | None = None,
    role: str | None,
    from_state: str | None,
    to_state: str,
    reason: str = "",
    exit_code: int | None = None,
    transient: bool = False,
    timestamp: float | None = None,
) -> None:
    """Append one observed container state transition.

    Best-effort and non-raising: the caller is the monitor's hot path, and a
    failure to record history must never break pod reconciliation.

    Args:
        pod_id: Pod name or container ID the transition was observed on.
        pipeline_id: Owning pipeline, from the pod's ``egg.pipeline.id`` label.
            ``None`` when the pod carries no pipeline label; such records are
            never returned by a pipeline-filtered :func:`transitions_for`.
        role: Agent role, when the pod carries one. Becomes the ``container``
            key the detectors group and prefix-match on.
        from_state: Prior state name (``None`` for a first observation).
        to_state: New state name, in Kubernetes vocabulary (``Running``,
            ``Terminated``, ``Waiting``).
        reason: Kubernetes-style reason (``OOMKilled``, ``Error``, ...).
        exit_code: Container exit code when terminated.
        transient: Whether this transition is known-benign churn. Detectors skip
            transitions flagged transient.
        timestamp: Unix epoch seconds; defaults to now.
    """
    try:
        record = {
            "pod": str(pod_id),
            "pipeline_id": str(pipeline_id) if pipeline_id else None,
            "container": str(role) if role else str(pod_id),
            "role": str(role) if role else None,
            "from": str(from_state) if from_state else None,
            "to": str(to_state),
            "reason": str(reason or ""),
            "exit_code": exit_code,
            "transient": bool(transient),
            "timestamp": float(timestamp) if timestamp is not None else time.time(),
        }
        with _lock:
            _transitions.append(record)
    except Exception:  # noqa: BLE001 — history is best-effort, never fatal
        return


def transitions_for(
    pipeline_id: str | None = None,
    *,
    pod_ids: set[str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return retained transitions, oldest first, annotated for the detectors.

    Filtering is by pipeline rather than by live container id on purpose: the
    lifecycle detectors exist to reason about containers that have *died*, so a
    filter built from the currently-live set would drop exactly the records they
    need.

    Args:
        pipeline_id: When given, only transitions labelled with this pipeline are
            returned. ``None`` does not filter by pipeline.
        pod_ids: Optional additional restriction to specific pod identifiers.

    Returns:
        Tuple of transition dicts carrying the stored fields plus the derived
        ``restart_count`` and ``recovered``. Both derived fields are computed
        over the filtered set, so a per-pipeline read yields per-pipeline
        restart counts.
    """
    with _lock:
        records = list(_transitions)

    if pipeline_id is not None:
        wanted_pipeline = str(pipeline_id)
        records = [r for r in records if r.get("pipeline_id") == wanted_pipeline]

    if pod_ids is not None:
        wanted = {str(p) for p in pod_ids}
        records = [r for r in records if r["pod"] in wanted]

    # Derive restart_count (prior Running entries per container) and recovered
    # (a later Running entry for the same container) in one pass each.
    running_seen: dict[str, int] = {}
    annotated: list[dict[str, Any]] = []
    for record in records:
        key = record["container"]
        entry = dict(record)
        entry["restart_count"] = running_seen.get(key, 0)
        annotated.append(entry)
        if record["to"] == _RUNNING_STATE:
            running_seen[key] = running_seen.get(key, 0) + 1

    later_running: set[str] = set()
    for entry in reversed(annotated):
        entry["recovered"] = entry["container"] in later_running
        if entry["to"] == _RUNNING_STATE:
            later_running.add(entry["container"])

    return tuple(annotated)


def clear() -> None:
    """Drop all retained transitions (test isolation / monitor shutdown)."""
    with _lock:
        _transitions.clear()


__all__ = ["MAX_TRANSITIONS", "clear", "record_transition", "transitions_for"]
