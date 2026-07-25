"""In-process pipeline-driver heartbeat registry (#3540).

The ``_run_pipeline`` driver and its per-phase work loops are in-memory
threads with no monitored surface: when one wedges, the pipeline stays
RUNNING while nothing spawns, and the per-pipeline overseer that should
notice is never spawned because the wedged driver itself owns that spawn
(#3540 observed an 11-hour silent wedge with zero detection).

This module gives the orchestrator's own poll loop something deterministic
to check. Driver work loops stamp :func:`record_tick` once per iteration;
agent spawn paths stamp :func:`record_spawn` on every successful spawn.
``DriverLivenessCheck`` (``health_checks/tier1/driver_liveness.py``) reads
the ages from the kubernetes-monitor RUNTIME_TICK sweep, which runs
independently of any driver thread.

State is process-local by design: driver threads live in this process, so
after an orchestrator restart the registry is empty and the reader starts a
fresh observation clock rather than treating "no stamp" as evidence of
death. All stamps use the monotonic clock; wall-clock jumps cannot fake or
mask a stall.
"""

from __future__ import annotations

import threading
import time

# Captured at import so the registry keeps real wall-progress semantics even
# when a test patches the shared ``time.monotonic`` module attribute with a
# scripted sequence (e.g. ``patch("routes.pipelines.time.monotonic")``, which
# mutates the one stdlib module object every module shares). Heartbeat stamps
# are incidental to those scripted timelines and must never consume their
# side-effect steps.
_monotonic = time.monotonic

_lock = threading.Lock()
_last_tick: dict[str, float] = {}
_last_spawn: dict[str, float] = {}


def record_tick(pipeline_id: str) -> None:
    """Stamp driver-loop liveness for ``pipeline_id``.

    Called once per iteration from the driver's work loops (the
    ``_run_pipeline`` phase loop, the implement-phase slice-wave loop, and
    the concurrent-phase consensus poll loops). Cheap enough for a 5s poll
    cadence; never raises.
    """
    now = _monotonic()
    with _lock:
        _last_tick[pipeline_id] = now


def record_spawn(pipeline_id: str) -> None:
    """Stamp agent-spawn progress for ``pipeline_id``.

    Called from the Kubernetes spawner on every successful agent/event Job
    spawn. Distinct from :func:`record_tick`: a driver can tick forever
    without ever spawning (the #3540 wedge), and that gap is exactly what
    the liveness check measures.
    """
    now = _monotonic()
    with _lock:
        _last_spawn[pipeline_id] = now


def tick_age_seconds(pipeline_id: str) -> float | None:
    """Seconds since the last driver-loop tick, or ``None`` if never stamped."""
    with _lock:
        stamp = _last_tick.get(pipeline_id)
    if stamp is None:
        return None
    return max(0.0, _monotonic() - stamp)


def spawn_age_seconds(pipeline_id: str) -> float | None:
    """Seconds since the last successful spawn, or ``None`` if never stamped."""
    with _lock:
        stamp = _last_spawn.get(pipeline_id)
    if stamp is None:
        return None
    return max(0.0, _monotonic() - stamp)


def clear(pipeline_id: str) -> None:
    """Drop both stamps for ``pipeline_id`` (terminal-pipeline cleanup)."""
    with _lock:
        _last_tick.pop(pipeline_id, None)
        _last_spawn.pop(pipeline_id, None)
