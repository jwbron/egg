"""
Health and pipeline health check endpoints for egg-orchestrator.

Three probe-friendly endpoints (``/api/v1/live``, ``/api/v1/ready``,
``/api/v1/health``) plus an on-demand pipeline health check endpoint
that runs the full two-tier health check framework (see
``health_checks/`` package).

Probe-path contract (locked by ``test_health_routes.py``): ``/live``,
``/ready`` and ``/health`` MUST NOT invoke ``MessageStore``,
``state_store.get_state_store``, ``subprocess.*`` or any other I/O on
the request path. The state-store self-heal that ``/api/v1/health``
used to drive synchronously now runs in a background thread (see
:mod:`state_store_probe`); these endpoints serve cached values only.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request

# Add paths for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_health import HealthTracker
from state_store_probe import get_state_store_probe, probe_state_store_at

health_bp = Blueprint("health", __name__, url_prefix="/api/v1")

# Module-level health tracker — updated every time a cached health
# observation is read. Exposes readiness history so operators can tell
# "healthy since boot" from "just came up" (see issue #1855).
_health_tracker = HealthTracker()


def _probe_state_store() -> tuple[bool, str]:
    """Request-context-aware shim around :func:`probe_state_store_at`.

    Retained at this import path so callers/tests that patch
    ``routes.health._probe_state_store`` continue to work. The
    kubelet-probe path no longer calls this directly — the BG thread in
    :mod:`state_store_probe` does — but several wedge-propagation tests
    (``test_state_store_wedge_propagation.py``) exercise it as a unit.

    Returns the aggregate ``(healthy, summary)`` pair from
    :func:`probe_state_store_at`. The per-repo detail is dropped here;
    callers that need it should consume the snapshot directly.
    """
    from routes import get_repo_path

    try:
        base_path = get_repo_path()
    except Exception as exc:
        return True, f"probe-skipped: {exc}"
    healthy, summary, _repos = probe_state_store_at(base_path)
    return healthy, summary


@health_bp.route("/health", methods=["GET"])
def health_check() -> tuple[Response, int]:
    """
    Health check endpoint — operator/dashboard fast path.

    Reads the cached state-store status populated by the background
    probe (see :mod:`state_store_probe`). Always 200; the body's
    ``status`` field is ``"healthy"`` or ``"degraded"`` so JSON consumers
    (``mcp__egg__check_health``, dashboards) can branch on it without
    interpreting HTTP codes.

    Note: for kubelet liveness/readiness, point probes at ``/live`` and
    ``/ready`` instead. Those endpoints serve the same cache but with
    HTTP-code semantics (200 vs 503) suited to probe consumers.

    The ``components.state_store`` field is a per-repo map (#2176) so
    operators in multi-repo deployments see every wedged repo at once
    rather than just the first one the probe loop hit. Empty in
    skip cases (no ``EGG_REPO_PATH``, no repos discovered, or before
    the first probe completes); ``components.state_store_summary``
    carries the human-readable aggregate string in those cases.

    Response::

        {
            "status": "healthy" | "degraded",
            "service": "egg-orchestrator",
            "timestamp": "...",
            "components": {
                "state_store": {"<repo>": {"status": "ok"} | {"status": "error", "error": "..."}},
                "state_store_summary": "ok" | "<aggregate error>",
                "docker": "unknown"
            },
            "process_start_time": "...",
            "healthy_since": "...",
            "last_unhealthy_at": "...",
            "recent_transitions": [...],
            "probe": {"fresh": bool, "age_seconds": float | null}
        }
    """
    snap = get_state_store_probe().snapshot()
    healthy = bool(snap["healthy"])
    # Dual-write to _health_tracker: the BG probe's on_observation
    # callback records the raw probe result at probe-interval cadence;
    # this request-path record() captures the staleness-corrected value
    # (so a wedged BG thread surfaces as an unhealthy transition the BG
    # itself cannot observe). HealthTracker.record is thread-safe and
    # idempotent on no-state-change, so the overlap is harmless.
    _health_tracker.record(healthy)
    tracker_snapshot = _health_tracker.snapshot()

    response = {
        "status": "healthy" if healthy else "degraded",
        "service": "egg-orchestrator",
        "timestamp": datetime.now(UTC).isoformat(),
        "components": {
            "state_store": snap["repos"],
            "state_store_summary": snap["message"],
            "docker": "unknown",
        },
        "process_start_time": tracker_snapshot["process_start_time"],
        "healthy_since": tracker_snapshot["healthy_since"],
        "last_unhealthy_at": tracker_snapshot["last_unhealthy_at"],
        "recent_transitions": tracker_snapshot["recent_transitions"],
        "probe": {
            "fresh": snap["fresh"],
            "age_seconds": snap["age_seconds"],
        },
    }

    return jsonify(response), 200


@health_bp.route("/ready", methods=["GET"])
def readiness_check() -> tuple[Response, int]:
    """
    Readiness check — kubelet ``readinessProbe`` target.

    Returns 200 when the cached state-store probe is fresh and healthy,
    503 otherwise. No I/O on the request path: this is a dict read.

    ``state_store`` is the per-repo map (#2176), matching ``/api/v1/health``
    so operators see the same shape regardless of which probe they hit.
    ``state_store_summary`` carries the aggregate human string for
    skip/starting/stale cases where the per-repo map is empty.

    Response (200)::

        {"ready": true,
         "state_store": {"<repo>": {"status": "ok"}},
         "state_store_summary": "ok",
         "fresh": true, "age_seconds": 4.2}

    Response (503)::

        {"ready": false,
         "state_store": {"<repo>": {"status": "error", "error": "..."}},
         "state_store_summary": "<aggregate error or 'starting'>",
         "fresh": false, "age_seconds": null}
    """
    snap = get_state_store_probe().snapshot()
    ready = bool(snap["healthy"]) and bool(snap["fresh"])
    body = {
        "ready": ready,
        "state_store": snap["repos"],
        "state_store_summary": snap["message"],
        "fresh": snap["fresh"],
        "age_seconds": snap["age_seconds"],
    }
    return jsonify(body), (200 if ready else 503)


@health_bp.route("/live", methods=["GET"])
def liveness_check() -> tuple[Response, int]:
    """
    Liveness check — kubelet ``livenessProbe`` target.

    Pure JSON return: confirms the process is up and the WSGI listener
    can serve requests. Does not consult the state-store cache — a
    state-store wedge takes the pod out of *traffic rotation* via
    ``/ready``, but does not justify a pod *restart* via ``/live``.
    """
    return jsonify({"alive": True}), 200


@health_bp.route("/pipelines/<pipeline_id>/health", methods=["GET"])
def pipeline_health_check(pipeline_id: str) -> tuple[Response, int]:
    """
    On-demand health check for a specific pipeline.

    Runs all registered health checks (Tier 1 + Tier 2) and returns results.

    Response:
        {
            "pipeline_id": "issue-99",
            "status": "healthy",
            "results": [...],
            "timestamp": "2024-01-15T12:00:00Z"
        }
    """
    # Runner is registered on app.config at startup (see cli.py)
    runner = current_app.config.get("HEALTH_CHECK_RUNNER")
    if runner is None:
        # 503: health checking is unavailable (framework failed to init)
        return jsonify(
            {
                "pipeline_id": pipeline_id,
                "status": "unknown",
                "message": "Health check runner not initialized",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ), 503

    try:
        from state_store import PipelineNotFoundError, get_state_store

        repo_path = os.environ.get("EGG_REPO_PATH", str(Path.cwd()))
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
    except PipelineNotFoundError:
        return jsonify(
            {
                "error": f"Pipeline {pipeline_id} not found",
            }
        ), 404
    except Exception as e:
        return jsonify(
            {
                "error": f"Failed to load pipeline: {e}",
            }
        ), 500

    try:
        from health_checks.context import PipelineHealthContext
        from health_checks.types import HealthTrigger

        try:
            from docker_client import get_docker_client

            dc = get_docker_client()
        except Exception:
            dc = None

        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path(repo_path),
            trigger=HealthTrigger.ON_DEMAND.value,
            state_store=store,
            docker_client=dc,
        )
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Aggregate status: worst-of across all results
        # Priority: FAILED > DEGRADED > HEALTHY
        aggregate_status = "healthy"
        for r in results:
            if r.status.value == "failed":
                aggregate_status = "failed"
                break
            if r.status.value == "degraded":
                aggregate_status = "degraded"

        return jsonify(
            {
                "pipeline_id": pipeline_id,
                "status": aggregate_status,
                "results": [r.to_dict() for r in results],
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ), 200

    except Exception as e:
        return jsonify(
            {
                "error": f"Health check execution failed: {e}",
            }
        ), 500


@health_bp.route("/pipelines/<pipeline_id>/health/alerts", methods=["GET"])
def pipeline_health_alerts(pipeline_id: str) -> tuple[Response, int]:
    """
    Get active health alerts for a pipeline.

    Returns alerts generated by the deterministic tripwire processor
    (HealthMonitor), including heartbeat timeouts, container exits,
    repeated errors, message rate spikes, and progress stalls.

    Response:
        {
            "pipeline_id": "issue-99",
            "alerts": [...],
            "count": 3,
            "timestamp": "2024-01-15T12:00:00Z"
        }
    """
    try:
        from health_monitor import get_health_monitor
    except ImportError:
        return jsonify(
            {
                "pipeline_id": pipeline_id,
                "alerts": [],
                "count": 0,
                "message": "Health monitor not available",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ), 200

    monitor = get_health_monitor()
    if monitor is None:
        return jsonify(
            {
                "pipeline_id": pipeline_id,
                "alerts": [],
                "count": 0,
                "message": "Health monitor not initialized",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ), 200

    all_alerts = monitor.get_active_alerts()
    alerts = [a for a in all_alerts if a.get("pipeline_id") == pipeline_id]

    return jsonify(
        {
            "pipeline_id": pipeline_id,
            "alerts": alerts,
            "count": len(alerts),
            "timestamp": datetime.now(UTC).isoformat(),
        }
    ), 200


@health_bp.route("/pipelines/<pipeline_id>/health/alerts/resolve", methods=["POST"])
def resolve_pipeline_health_alerts(pipeline_id: str) -> tuple[Response, int]:
    """
    Resolve (remove) health alerts matching an agent and alert type.

    Request body:
        {
            "agent_id": "reviewer_code",
            "alert_type": "heartbeat_timeout"
        }

    Response:
        {"success": true, "resolved": true}
    """
    try:
        from health_monitor import get_health_monitor
    except ImportError:
        return jsonify({"success": False, "error": "Health monitor not available"}), 503

    monitor = get_health_monitor()
    if monitor is None:
        return jsonify({"success": False, "error": "Health monitor not initialized"}), 503

    data = request.get_json() or {}
    agent_id = data.get("agent_id")
    alert_type = data.get("alert_type")

    if not agent_id or not alert_type:
        return jsonify({"success": False, "error": "Missing agent_id or alert_type"}), 400

    monitor.resolve_alerts(agent_id, alert_type)
    return jsonify({"success": True, "resolved": True}), 200
