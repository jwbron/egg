"""
Health and pipeline health check endpoints for egg-orchestrator.

Includes standard service health/readiness/liveness probes and an
on-demand pipeline health check endpoint that runs the full two-tier
health check framework (see ``health_checks/`` package).
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

health_bp = Blueprint("health", __name__, url_prefix="/api/v1")

# Module-level health tracker — updated every time /api/v1/health is
# evaluated. Exposes readiness history so operators can tell "healthy
# since boot" from "just came up" (see issue #1855).
_health_tracker = HealthTracker()


def _probe_state_store() -> tuple[bool, str]:
    """Probe whether the state-store worktree is loadable.

    Walks the configured ``EGG_REPO_PATH`` (or each child repo in
    multi-repo mode) and accesses ``store.worktree`` on each.  Reports
    degraded if any probe raises — typically ``GitOperationError`` from
    ``git worktree add`` contention or a stale ``prunable`` admin dir
    (#2167).  Returns the actual error message so the operator sees it
    in the health payload instead of having to grep orchestrator logs.

    Probe is cheap when healthy: ``_ensure_worktree`` short-circuits
    on a valid worktree via ``git rev-parse --is-inside-work-tree``.

    NOTE: This is not a side-effect-free GET.  ``store.worktree``
    triggers ``_add_worktree_with_branch_recovery`` on a wedged repo,
    which may ``shutil.rmtree`` a stale admin dir and retry the
    ``git worktree add``.  Frequent pollers of ``/api/v1/health``
    (Prometheus, operator dashboards, ``deployment.py``) will therefore
    drive recovery attempts during a wedge — the desired behavior, but
    operators should be aware the probe is curative, not just observational.
    """
    from routes import get_repo_path
    from state_store import discover_repo_paths, get_state_store

    try:
        base_path = get_repo_path()
    except Exception as exc:
        # Request context missing or repo lookup failed — don't flap
        # the health check on configuration issues unrelated to the
        # state-store wedge we're trying to detect.
        return True, f"probe-skipped: {exc}"

    if (base_path / ".git").exists():
        repos = [base_path]
    else:
        repos = list(discover_repo_paths(base_path))
        if not repos:
            return True, "probe-skipped: no git repos under base_path"

    for repo_path in repos:
        try:
            store = get_state_store(repo_path)
            _ = store.worktree
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    return True, "ok"


@health_bp.route("/health", methods=["GET"])
def health_check() -> tuple[Response, int]:
    """
    Health check endpoint.

    Returns basic service status and component health.  The state-store
    component is probed live: if ``_ensure_worktree`` fails (e.g. the
    ``egg/pipeline-state`` branch is pinned by a prunable worktree —
    #2167), the top-level ``status`` flips to ``degraded`` and the
    failing error message is reported in ``components.state_store``.

    The HTTP status stays 200 regardless so kubernetes liveness/readiness
    probes (``/live``, ``/ready``) are unaffected.  Clients reading the
    health JSON should branch on the ``status`` field, not the HTTP code.

    Response:
        {
            "status": "healthy" | "degraded",
            "service": "egg-orchestrator",
            "timestamp": "2024-01-15T12:00:00Z",
            "components": {
                "state_store": "ok" | "<error message>",
                "docker": "unknown"
            }
        }
    """
    state_store_healthy, state_store_status = _probe_state_store()
    _health_tracker.record(state_store_healthy)
    tracker_snapshot = _health_tracker.snapshot()

    response = {
        "status": "healthy" if state_store_healthy else "degraded",
        "service": "egg-orchestrator",
        "timestamp": datetime.now(UTC).isoformat(),
        "components": {
            "state_store": state_store_status,
            "docker": "unknown",  # Will be updated when docker client is available
        },
        "process_start_time": tracker_snapshot["process_start_time"],
        "healthy_since": tracker_snapshot["healthy_since"],
        "last_unhealthy_at": tracker_snapshot["last_unhealthy_at"],
        "recent_transitions": tracker_snapshot["recent_transitions"],
    }

    return jsonify(response), 200


@health_bp.route("/ready", methods=["GET"])
def readiness_check() -> tuple[Response, int]:
    """
    Readiness check endpoint.

    Indicates if the service is ready to accept requests.
    Used by container orchestrators for traffic routing.

    Response:
        {"ready": true}
    """
    return jsonify({"ready": True}), 200


@health_bp.route("/live", methods=["GET"])
def liveness_check() -> tuple[Response, int]:
    """
    Liveness check endpoint.

    Indicates if the service is alive and should not be restarted.
    Used by container orchestrators for health monitoring.

    Response:
        {"alive": true}
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
