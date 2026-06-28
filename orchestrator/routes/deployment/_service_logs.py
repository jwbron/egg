"""``get_service_logs`` — gateway/orchestrator pod-log tail endpoint (#3312, #1853).

Tails logs from the pod(s) backing the gateway or orchestrator Deployment
with optional server-side ``pipeline_id`` / ``level`` / ``pattern`` filters
applied before truncation (#3032). The runtime guard is reached via ``_pkg``
so the ``patch("routes.deployment._current_runtime")`` test seam stays
effective.
"""

from __future__ import annotations

import os
import re

import routes.deployment as _pkg
from flask import Response, jsonify, request
from log_filter import filter_log_lines, known_severities

from . import logger
from ._runtime import _not_available_on_runtime

# Allowlist of services whose logs are readable via this endpoint.  Keeping
# the surface bounded avoids turning this into a generic kubectl-logs proxy:
# agent-pod logs live in the `egg-agents` namespace and are already exposed
# through the container-scoped `get_container_logs` tool.
_SERVICE_LOG_ALLOWLIST: frozenset[str] = frozenset({"gateway", "orchestrator"})

_MAX_LOG_LINES = 10_000


def get_service_logs() -> tuple[Response, int]:
    """Return logs from the pod(s) backing the gateway or orchestrator Deployment.

    Query params:
        service: one of _SERVICE_LOG_ALLOWLIST (required).
        lines: tail length, default 100, capped at _MAX_LOG_LINES. When a
            filter (``pipeline_id``/``level``/``pattern``) is active this caps
            the *matching* lines returned rather than the raw tail.
        since_seconds: only return logs newer than this many seconds. When
            filters are active this is the only knob that bounds the
            per-pod scan window — the backing fetch is widened to
            10 000 lines (``_MAX_LOG_LINES``) so the filter has material
            to match.
        pipeline_id: keep only lines whose pipeline/task id matches; checks
            ``context.task_id`` and the ``extra.pipeline_id`` /
            ``extra.task_id`` fallbacks the JsonFormatter lands kwargs in
            (#3032).
        level: minimum severity (case-sensitive; ``DEBUG`` … ``CRITICAL``);
            drops lower-severity and unstructured lines. The MCP ``level``
            enum is the source of truth — the HTTP route rejects lowercase
            for parity (#3032).
        pattern: Python regex; keep only lines it finds via ``re.search``.
            Compiled with no complexity guardrail — pathological patterns
            (catastrophic backtracking) can spin a request thread per pod
            line. This endpoint is gated behind ``require_lifecycle_secret``
            and called from trusted operator tooling, so the trust model
            here is "operator can hose their own request"; widen with a
            timeout if this surface ever opens up (#3032).

    Filters are applied server-side **before** truncation, so a targeted query
    returns the relevant lines instead of a raw tail that's mostly health-check
    noise. When any filter is set, the backing pod is scanned over a wider
    window (10 000 lines / ``_MAX_LOG_LINES``, still bounded by
    ``since_seconds``) so the filter has material to match, and ``lines``
    caps the matches returned — use ``since_seconds`` to keep the per-pod
    scan cost bounded.
    """
    if _pkg._current_runtime() != "kubernetes":
        return _not_available_on_runtime()

    service = (request.args.get("service") or "").strip()
    if not service:
        return jsonify({"success": False, "message": "service is required"}), 400
    if service not in _SERVICE_LOG_ALLOWLIST:
        return (
            jsonify(
                {
                    "success": False,
                    "message": (
                        f"service must be one of {sorted(_SERVICE_LOG_ALLOWLIST)}; got {service!r}"
                    ),
                }
            ),
            400,
        )

    try:
        lines = int(request.args.get("lines", 100))
    except ValueError, TypeError:
        lines = 100
    if lines <= 0:
        lines = 100
    lines = min(lines, _MAX_LOG_LINES)

    since_raw = request.args.get("since_seconds")
    since_seconds: int | None = None
    if since_raw is not None:
        try:
            since_seconds = int(since_raw)
        except ValueError, TypeError:
            return (
                jsonify({"success": False, "message": "since_seconds must be an integer"}),
                400,
            )
        if since_seconds <= 0:
            since_seconds = None

    # Server-side filters (#3032). Applied below, before truncation.
    pipeline_id = (request.args.get("pipeline_id") or "").strip() or None
    # Case-sensitive on purpose: the MCP `level` enum is uppercase-only, and
    # accepting lowercase here would diverge HTTP behavior from what the
    # schema advertises. ``severity_rank`` itself is case-insensitive so the
    # filter helper works for any direct caller; the strict check is the
    # route's contract with its operator-facing schema.
    level = (request.args.get("level") or "").strip() or None
    if level is not None and level not in known_severities():
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"level must be one of {known_severities()}; got {level!r}",
                }
            ),
            400,
        )
    pattern_raw = request.args.get("pattern") or None
    compiled_pattern: re.Pattern[str] | None = None
    if pattern_raw:
        try:
            compiled_pattern = re.compile(pattern_raw)
        except re.error as exc:
            return (
                jsonify({"success": False, "message": f"pattern is not a valid regex: {exc}"}),
                400,
            )
    filters_active = bool(pipeline_id) or level is not None or compiled_pattern is not None
    # With a filter active, scan a wider window so the filter has material to
    # match; ``lines`` then caps the matches returned, not the raw tail.
    fetch_lines = _MAX_LOG_LINES if filters_active else lines

    namespace = os.environ.get("EGG_K8S_NAMESPACE", "egg-system")

    try:
        from kubernetes_client import (
            JobOperationError,
            PodNotFoundError,
            get_kubernetes_client,
        )
    except Exception as exc:  # pragma: no cover - env wiring
        return jsonify({"success": False, "message": f"kubernetes unavailable: {exc}"}), 503

    try:
        k8s = get_kubernetes_client()
    except Exception as exc:
        return (
            jsonify({"success": False, "message": f"kubernetes init failed: {exc}"}),
            503,
        )

    try:
        payload = k8s.get_service_logs(
            service=service,
            namespace=namespace,
            tail_lines=fetch_lines,
            since_seconds=since_seconds,
        )
    except PodNotFoundError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    except JobOperationError as exc:
        return jsonify({"success": False, "message": str(exc)}), 500
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("get_service_logs failed", service=service, error=str(exc))
        return jsonify({"success": False, "message": f"failed: {exc}"}), 500

    if filters_active:
        for chunk in payload.get("pods", []):
            chunk["logs"] = filter_log_lines(
                chunk.get("logs", ""),
                pipeline_id=pipeline_id,
                min_level=level,
                pattern=compiled_pattern,
                limit=lines,
            )
        payload["filter"] = {
            "pipeline_id": pipeline_id,
            "level": level,
            "pattern": pattern_raw,
            "returned_line_cap": lines,
            "scanned_line_budget": fetch_lines,
        }

    return jsonify({"success": True, "data": payload}), 200
