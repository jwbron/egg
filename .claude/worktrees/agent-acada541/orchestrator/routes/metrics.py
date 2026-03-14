"""
Metrics endpoint for the orchestrator.
"""

import sys
from pathlib import Path

from flask import Blueprint, Response, jsonify

# Add parent directory to path for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

from metrics import get_metrics_registry

metrics_bp = Blueprint("metrics", __name__, url_prefix="/api/v1")


@metrics_bp.route("/metrics", methods=["GET"])
def get_metrics() -> tuple[Response, int]:
    """
    Get all orchestrator metrics.

    Response:
        {
            "uptime_seconds": 3600.0,
            "counters": {...},
            "gauges": {...},
            "histograms": {...}
        }
    """
    registry = get_metrics_registry()
    return jsonify(registry.get_all()), 200


@metrics_bp.route("/metrics/prometheus", methods=["GET"])
def get_prometheus_metrics() -> tuple[Response, int]:
    """
    Get metrics in Prometheus text format.

    Response:
        # HELP orchestrator_pipelines_created_total Total pipelines created
        # TYPE orchestrator_pipelines_created_total counter
        orchestrator_pipelines_created_total 10
    """
    registry = get_metrics_registry()
    metrics = registry.get_all()

    lines = []

    # Counters
    for key, data in metrics.get("counters", {}).items():
        name = key.split("{")[0]  # Remove labels from key
        lines.append(f"# HELP {name} Counter metric")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{key} {data['value']}")

    # Gauges
    for key, data in metrics.get("gauges", {}).items():
        name = key.split("{")[0]
        lines.append(f"# HELP {name} Gauge metric")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{key} {data['value']}")

    # Histograms
    for key, data in metrics.get("histograms", {}).items():
        name = key.split("{")[0]
        lines.append(f"# HELP {name} Histogram metric")
        lines.append(f"# TYPE {name} histogram")
        lines.append(f"{name}_count {data['count']}")
        lines.append(f"{name}_sum {data['sum']}")
        for bucket, count in data.get("buckets", {}).items():
            if bucket == float("inf"):
                lines.append(f'{name}_bucket{{le="+Inf"}} {count}')
            else:
                lines.append(f'{name}_bucket{{le="{bucket}"}} {count}')

    output = "\n".join(lines)
    return Response(output, mimetype="text/plain"), 200
