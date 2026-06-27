"""Runtime detection helpers for the deployment blueprint (#3312).

Resolves the orchestrator's container runtime (kubernetes vs docker) and
builds the structured degrade-gracefully payloads the routes return when a
k8s-only tool is invoked on docker or when cluster detection fails.
"""

from __future__ import annotations

import os

import routes.deployment as _pkg
from flask import Response, jsonify

from . import logger


def _resolve_runtime() -> tuple[str, str]:
    """Return ``(runtime, detection_source)``.

    Resolution order:

    1. ``EGG_RUNTIME`` env var (``"env"`` source) — operator-configured.
    2. ``KUBERNETES_SERVICE_HOST`` is injected into every pod by the
       apiserver; treat its presence as a strong in-cluster signal and
       infer ``"kubernetes"`` (``"auto:k8s-service-host"`` source).
    3. Otherwise fall back to ``"docker"`` (``"auto:default"`` source).

    Issue #1850: previously defaulted to ``"docker"`` unconditionally, so
    in-cluster orchestrators whose manifests forgot to set ``EGG_RUNTIME``
    silently misreported themselves as Docker and masked cluster-access
    failures downstream. Auto-detection closes that gap without changing
    behavior when ``EGG_RUNTIME`` is set explicitly.
    """
    _KNOWN_RUNTIMES = frozenset({"kubernetes", "docker"})

    explicit = os.environ.get("EGG_RUNTIME")
    if explicit:
        normalized = explicit.lower()
        if normalized not in _KNOWN_RUNTIMES:
            logger.warning(
                "unrecognized EGG_RUNTIME value",
                value=explicit,
                known=sorted(_KNOWN_RUNTIMES),
            )
        return normalized, "env"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes", "auto:k8s-service-host"
    return "docker", "auto:default"


def _current_runtime() -> str:
    """Return the resolved runtime label (back-compat shim)."""
    runtime, _source = _resolve_runtime()
    return runtime


def _not_available_on_runtime() -> tuple[Response, int]:
    """Return the structured 200 payload used on Docker-only builds."""
    runtime = _pkg._current_runtime()
    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "error": "not_available_on_runtime",
                    "runtime": runtime,
                },
            }
        ),
        200,
    )


def _runtime_detection_failed(detail: str) -> tuple[Response, int]:
    """Return a 200 payload used when runtime detection came back ``unknown``.

    Distinct from ``not_available_on_runtime`` (which means "you explicitly
    asked for docker, this tool is k8s-only") so operators can tell
    "apiserver unreachable / detection failed" apart from "tool doesn't
    apply to docker" (#1850).
    """
    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "error": "runtime_detection_failed",
                    "runtime": "unknown",
                    "detail": detail,
                },
            }
        ),
        200,
    )


def _probe_kubernetes_reachable() -> tuple[bool, str | None]:
    """Return ``(reachable, reason_if_not)`` for the k8s apiserver.

    Used by gated routes so they refuse cleanly with
    ``runtime_detection_failed`` when the process claims ``kubernetes``
    but can't actually reach the cluster (#1850). Cheap: one VersionApi
    call; on failure the caller's downstream logic is bypassed.
    """
    try:
        from kubernetes_client import get_kubernetes_client
    except Exception as exc:  # pragma: no cover - environment wiring
        return False, f"kubernetes_client_unavailable: {exc}"
    try:
        k8s = get_kubernetes_client()
    except Exception as exc:
        return False, f"kubernetes_client_init_failed: {exc}"
    try:
        from kubernetes import client as k8s_client_pkg

        k8s_client_pkg.VersionApi(k8s.batch_api.api_client).get_code()
    except Exception as exc:
        return False, f"apiserver_unreachable: {exc}"
    return True, None
