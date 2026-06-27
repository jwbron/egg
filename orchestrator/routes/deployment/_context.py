"""``get_deployment_context`` — runtime / cluster / image introspection (#3312).

Assembles the deployment-context response by probing the apiserver for
cluster version, node count, CNI, k3s flavor, and egg image tags, demoting
the runtime to ``unknown`` when every probe fails (#1850). The cluster
detectors are reached via ``_pkg`` so the route tests'
``patch("routes.deployment._detect_*")`` seams stay effective.
"""

from __future__ import annotations

import os
from typing import Any

import routes.deployment as _pkg
from flask import Response, jsonify

from . import logger
from ._runtime import _resolve_runtime


def _build_deployment_context_payload() -> dict[str, Any]:
    """Assemble the ``get_deployment_context`` response body.

    When ``runtime`` resolves to ``"kubernetes"`` but every cluster probe
    fails (apiserver unreachable, RBAC denial, missing kubeconfig), the
    runtime is demoted to ``"unknown"`` with a ``detection_error`` so
    downstream guards can distinguish "you're on docker" from "I couldn't
    tell what cluster I'm on" (#1850).
    """
    runtime, detection_source = _resolve_runtime()
    payload: dict[str, Any] = {
        "runtime": runtime,
        "detection_source": detection_source,
        "namespace": os.environ.get("EGG_K8S_NAMESPACE", "egg-system"),
    }

    if runtime != "kubernetes":
        # Degrade gracefully: return the Docker-analog fields so the
        # MCP caller still gets an actionable structure.
        payload.update(
            {
                "kubeconfig_context": None,
                "cluster_info": {
                    "server_version": None,
                    "nodes": 0,
                },
                "cni": None,
                "network_policy_enforcement": False,
                "images": {},
                "is_k3s": False,
                "k3s_flavor_hint": None,
            }
        )
        return payload

    try:
        from kubernetes_client import get_kubernetes_client
    except Exception as exc:  # pragma: no cover - environment wiring
        logger.warning("kubernetes_client import failed", error=str(exc))
        payload["runtime"] = "unknown"
        payload.update(
            {
                "detection_error": "kubernetes_client_unavailable",
                "detail": str(exc),
            }
        )
        return payload

    try:
        k8s = get_kubernetes_client()
    except Exception as exc:
        logger.warning("kubernetes client init failed", error=str(exc))
        payload["runtime"] = "unknown"
        payload.update(
            {
                "detection_error": "kubernetes_client_init_failed",
                "detail": str(exc),
            }
        )
        return payload

    namespace = payload["namespace"]

    # Cluster info — track whether each probe actually reached the
    # apiserver so we can tell "healthy cluster with zero nodes" (not a
    # thing) apart from "never got an answer."
    server_version = None
    version_ok = False
    try:
        from kubernetes import client as k8s_client_pkg

        version_api = k8s_client_pkg.VersionApi(k8s.batch_api.api_client)
        vinfo = version_api.get_code()
        server_version = getattr(vinfo, "git_version", None)
        version_ok = True
    except Exception as exc:
        logger.warning("kubernetes version probe failed", error=str(exc))

    nodes_count = 0
    nodes_ok = False
    try:
        nodes = k8s.core_api.list_node()
        nodes_count = len(getattr(nodes, "items", []) or [])
        nodes_ok = True
    except Exception as exc:
        logger.warning("kubernetes node list failed", error=str(exc))

    if not version_ok and not nodes_ok:
        # Neither probe reached the apiserver — we cannot claim the
        # runtime is kubernetes. Demote so rebuild_and_rollout and other
        # gated tools can refuse with an honest reason rather than
        # operating against a cluster that isn't really there.
        payload["runtime"] = "unknown"
        payload.update(
            {
                "detection_error": "cluster_unreachable",
                "detail": "neither VersionApi.get_code nor core_api.list_node succeeded",
                "cluster_info": {"server_version": None, "nodes": 0, "nodes_unavailable": True},
            }
        )
        return payload

    is_k3s, k3s_hint = _pkg._detect_k3s(k8s)
    cni, enforcement = _pkg._detect_cni(k8s)

    images = _pkg._collect_egg_image_tags(k8s, namespace)

    cluster_info: dict[str, Any] = {"server_version": server_version, "nodes": nodes_count}
    if not nodes_ok:
        # Distinguish "zero nodes" (not a real scenario) from "probe
        # failed, count unknown" — same pattern as images_unavailable.
        cluster_info["nodes_unavailable"] = True

    payload.update(
        {
            "kubeconfig_context": os.environ.get("KUBE_CONTEXT"),
            "cluster_info": cluster_info,
            "cni": cni,
            "network_policy_enforcement": bool(enforcement),
            "images": images,
            "is_k3s": bool(is_k3s),
            "k3s_flavor_hint": k3s_hint,
        }
    )
    if not images:
        # Empty images on an otherwise-reachable cluster is a partial
        # failure (RBAC, wrong namespace, empty ns). Flag it so the
        # operator isn't guessing why the tool's docstring promise of
        # "deployed image tags" came back empty.
        payload["images_unavailable"] = True

    return payload


def get_deployment_context() -> tuple[Response, int]:
    """Return runtime / cluster / image introspection."""
    try:
        payload = _build_deployment_context_payload()
    except Exception as exc:
        logger.error("get_deployment_context failed", error=str(exc))
        return jsonify({"success": False, "message": f"failed: {exc}"}), 500
    return jsonify({"success": True, "data": payload}), 200
