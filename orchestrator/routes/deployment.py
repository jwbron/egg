"""Deployment introspection and action endpoints.

Exposes read-only introspection tools
(``get_deployment_context``, ``validate_deployment_manifests``,
``get_service_logs``) and mutating actions (``prune_stale_worktrees`` —
proxied to the gateway, ``validate_network_isolation``,
``rebuild_and_rollout``) for operator diagnostics on the Kubernetes
runtime.

Every route requires the lifecycle bearer token (parity with the
fix made for #1769) because these are agent-visible via the MCP
server. Runtime-specific routes degrade gracefully on Docker by
returning a structured ``not_available_on_runtime`` payload.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import yaml
from flask import Blueprint, Response, jsonify, request

# Kubernetes label values must match this regex (RFC 1123-ish) or the
# apiserver rejects Job creation with a 422.
_K8S_LABEL_VALUE_RE = re.compile(r"^[a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?$")

# Add parent directory to path for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


from lifecycle_auth import require_lifecycle_secret

logger = get_logger("orchestrator.deployment")

deployment_bp = Blueprint("deployment", __name__, url_prefix="/api/v1/deployment")


# ---------------------------------------------------------------------------
# Runtime detection helpers
# ---------------------------------------------------------------------------


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
    runtime = _current_runtime()
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


# ---------------------------------------------------------------------------
# get_deployment_context
# ---------------------------------------------------------------------------

_NETWORK_POLICY_CNIS: frozenset[str] = frozenset(
    {
        "calico",
        "cilium",
        "weave",
        "antrea",
        "kube-router",
    }
)


def _detect_k3s(k8s_client: Any) -> tuple[bool, str | None]:
    """Heuristic detection of a k3s cluster.

    Order: (a) node kubeletVersion ends with ``+k3s<N>``; (b) a
    DaemonSet in ``kube-system`` runs an image matching
    ``rancher/k3s`` or ``rancher/mirrored-k3s-*``.  Returns
    ``(is_k3s, flavor_hint)``. The hint is the first matching
    version string or image name so callers can surface why a rule
    did / didn't fire.
    """
    try:
        nodes = k8s_client.core_api.list_node()
    except Exception:
        nodes = None

    if nodes and getattr(nodes, "items", None):
        for node in nodes.items:
            ni = getattr(getattr(node, "status", None), "node_info", None)
            kubelet = getattr(ni, "kubelet_version", None) if ni else None
            if kubelet and "+k3s" in kubelet:
                return True, kubelet

    try:
        from kubernetes import client as k8s_client_pkg

        apps = k8s_client_pkg.AppsV1Api(k8s_client.batch_api.api_client)
        ds_list = apps.list_namespaced_daemon_set("kube-system")
    except Exception:
        return False, None

    for ds in getattr(ds_list, "items", []) or []:
        containers = (
            getattr(getattr(getattr(ds, "spec", None), "template", None), "spec", None)
            and ds.spec.template.spec.containers
        ) or []
        for c in containers:
            image = getattr(c, "image", "") or ""
            if "rancher/k3s" in image or "rancher/mirrored-k3s-" in image:
                return True, image

    return False, None


def _detect_cni(k8s_client: Any) -> tuple[str | None, bool]:
    """Return ``(cni_name, network_policy_enforcement)``.

    The CNI name is inferred from DaemonSets in ``kube-system`` — most
    NetworkPolicy-capable CNIs advertise themselves there.  The
    enforcement flag is True when the detected CNI is in the
    ``_NETWORK_POLICY_CNIS`` allowlist.
    """
    try:
        from kubernetes import client as k8s_client_pkg

        apps = k8s_client_pkg.AppsV1Api(k8s_client.batch_api.api_client)
        ds_list = apps.list_namespaced_daemon_set("kube-system")
    except Exception:
        return None, False

    detected: str | None = None
    items = getattr(ds_list, "items", []) or []
    for ds in items:
        name = (getattr(ds, "metadata", None) and ds.metadata.name) or ""
        if not name:
            continue
        for token, label in (
            ("calico", "calico"),
            ("cilium", "cilium"),
            ("weave", "weave"),
            ("antrea", "antrea"),
            ("kube-router", "kube-router"),
            ("flannel", "flannel"),
        ):
            if token in name.lower():
                detected = label
                break
        if detected:
            break

    enforcement = detected in _NETWORK_POLICY_CNIS if detected else False
    return detected, enforcement


def _collect_egg_image_tags(k8s_client: Any, namespace: str) -> dict[str, str]:
    """Return image tags for orchestrator/gateway/sandbox deployments."""
    out: dict[str, str] = {}
    try:
        from kubernetes import client as k8s_client_pkg

        apps = k8s_client_pkg.AppsV1Api(k8s_client.batch_api.api_client)
        deps = apps.list_namespaced_deployment(namespace)
    except Exception:
        return out

    for dep in getattr(deps, "items", []) or []:
        dep_name = (getattr(dep, "metadata", None) and dep.metadata.name) or ""
        containers = (
            getattr(getattr(getattr(dep, "spec", None), "template", None), "spec", None)
            and dep.spec.template.spec.containers
        ) or []
        for c in containers:
            image = getattr(c, "image", "") or ""
            if not image:
                continue
            if "orchestrator" in dep_name and "orchestrator" not in out:
                out["orchestrator"] = image
            elif "gateway" in dep_name and "gateway" not in out:
                out["gateway"] = image
            elif ("sandbox" in dep_name or "agent" in dep_name) and "agents" not in out:
                out["agents"] = image

    return out


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

    is_k3s, k3s_hint = _detect_k3s(k8s)
    cni, enforcement = _detect_cni(k8s)

    images = _collect_egg_image_tags(k8s, namespace)

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


@deployment_bp.route("/context", methods=["GET"])
@require_lifecycle_secret
def get_deployment_context() -> tuple[Response, int]:
    """Return runtime / cluster / image introspection."""
    try:
        payload = _build_deployment_context_payload()
    except Exception as exc:
        logger.error("get_deployment_context failed", error=str(exc))
        return jsonify({"success": False, "message": f"failed: {exc}"}), 500
    return jsonify({"success": True, "data": payload}), 200


# ---------------------------------------------------------------------------
# get_service_logs (#1853)
# ---------------------------------------------------------------------------

# Allowlist of services whose logs are readable via this endpoint.  Keeping
# the surface bounded avoids turning this into a generic kubectl-logs proxy:
# agent-pod logs live in the `egg-agents` namespace and are already exposed
# through the container-scoped `get_container_logs` tool.
_SERVICE_LOG_ALLOWLIST: frozenset[str] = frozenset({"gateway", "orchestrator"})

_MAX_LOG_LINES = 10_000


@deployment_bp.route("/logs", methods=["GET"])
@require_lifecycle_secret
def get_service_logs() -> tuple[Response, int]:
    """Return logs from the pod(s) backing the gateway or orchestrator Deployment.

    Query params:
        service: one of _SERVICE_LOG_ALLOWLIST (required).
        lines: tail length, default 100, capped at _MAX_LOG_LINES.
        since_seconds: only return logs newer than this many seconds.
    """
    if _current_runtime() != "kubernetes":
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
            tail_lines=lines,
            since_seconds=since_seconds,
        )
    except PodNotFoundError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    except JobOperationError as exc:
        return jsonify({"success": False, "message": str(exc)}), 500
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("get_service_logs failed", service=service, error=str(exc))
        return jsonify({"success": False, "message": f"failed: {exc}"}), 500

    return jsonify({"success": True, "data": payload}), 200


# ---------------------------------------------------------------------------
# validate_deployment_manifests
# ---------------------------------------------------------------------------

_DEFAULT_OVERLAY = "k8s/overlays/local"


def _run_kustomize(overlay_path: Path) -> list[dict[str, Any]]:
    """Render the overlay with ``kustomize build`` and return docs.

    Raises RuntimeError if kustomize fails or returns empty output.
    """
    exe = os.environ.get("EGG_KUSTOMIZE_BIN", "kustomize")
    try:
        proc = subprocess.run(
            [exe, "build", str(overlay_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        # Fall back to ``kubectl kustomize``; some environments only
        # ship kubectl. If kubectl is also missing, surface a
        # structured error rather than a 500.
        try:
            proc = subprocess.run(
                ["kubectl", "kustomize", str(overlay_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "kustomize_unavailable: neither kustomize nor kubectl is on PATH"
            ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"kustomize build timed out: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"kustomize build failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )

    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    if not docs:
        raise RuntimeError("kustomize build produced no documents")
    return docs


def _warn(
    warnings: list[dict[str, Any]],
    rule: str,
    severity: str,
    resource: str,
    message: str,
    **extra: Any,
) -> None:
    entry: dict[str, Any] = {
        "rule": rule,
        "severity": severity,
        "resource": resource,
        "message": message,
    }
    if extra:
        entry["extra"] = extra
    warnings.append(entry)


def _deployment_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = (doc.get("spec") or {}).get("template", {}).get("spec") or {}
    return list(spec.get("containers") or [])


def _deployment_volumes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = (doc.get("spec") or {}).get("template", {}).get("spec") or {}
    return list(spec.get("volumes") or [])


def _validate_deployment_docs(docs: list[dict[str, Any]], *, is_k3s: bool) -> list[dict[str, Any]]:
    """Apply the five warning rules from the #1759 validation session.

    Rules:

    1. Missing ``Secret`` reference (secretName in a volume with no
       matching Secret resource in the overlay).
    2. Missing ``hostPath`` for gateway / orchestrator volumes on local
       overlays (skipped when we cannot detect an overlay with hostPath
       mounts at all — matching non-local deploys).
    3. Missing container image tag (``image:`` without ``:`` or with a
       placeholder ``:latest`` tag — k3s-gated because only k3s relies
       on locally-imported image tags).
    4. Service selector labels not matching deployment template labels.
    5. Env-var name collision: same name declared twice in a single
       container's ``env`` list.
    """
    warnings: list[dict[str, Any]] = []

    # Build lookup tables
    secrets = {d.get("metadata", {}).get("name") for d in docs if d.get("kind") == "Secret"}
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    services = [d for d in docs if d.get("kind") == "Service"]

    any_hostpath = any(
        any("hostPath" in (v or {}) for v in _deployment_volumes(d)) for d in deployments
    )

    # Rule 1: Secret references
    for dep in deployments:
        name = dep.get("metadata", {}).get("name", "<unknown>")
        for vol in _deployment_volumes(dep):
            secret_cfg = vol.get("secret")
            if not secret_cfg:
                continue
            secret_name = secret_cfg.get("secretName")
            if secret_name and secret_name not in secrets:
                _warn(
                    warnings,
                    rule="secret-missing",
                    severity="error",
                    resource=f"Deployment/{name}",
                    message=(
                        f"volume references Secret '{secret_name}' which is not "
                        "declared in the overlay"
                    ),
                )

    # Rule 2: hostPath volume presence on local overlays.
    if any_hostpath:
        for dep in deployments:
            name = dep.get("metadata", {}).get("name", "<unknown>")
            if "gateway" not in name and "orchestrator" not in name:
                continue
            has_hostpath = any("hostPath" in (v or {}) for v in _deployment_volumes(dep))
            if not has_hostpath:
                _warn(
                    warnings,
                    rule="hostpath-missing",
                    severity="warn",
                    resource=f"Deployment/{name}",
                    message=(
                        "local overlay declares hostPath mounts elsewhere but this "
                        "Deployment has none — worktrees/repos will not be visible"
                    ),
                )

    # Rule 3: image tag presence (k3s-gated)
    if is_k3s:
        for dep in deployments:
            name = dep.get("metadata", {}).get("name", "<unknown>")
            for c in _deployment_containers(dep):
                image = (c or {}).get("image", "")
                if not image:
                    _warn(
                        warnings,
                        rule="image-missing",
                        severity="error",
                        resource=f"Deployment/{name}",
                        message=f"container '{c.get('name', '<unnamed>')}' has no image field",
                    )
                    continue
                if ":" not in image:
                    _warn(
                        warnings,
                        rule="image-missing-tag",
                        severity="warn",
                        resource=f"Deployment/{name}",
                        message=(
                            f"container image '{image}' has no tag — k3s "
                            "containerd will not find the locally-imported image"
                        ),
                    )
    else:
        warnings.append(
            {
                "skipped": "not_k3s",
                "rule": "image-missing-tag",
                "detected_runtime": None,
            }
        )

    # Rule 4: Service selector labels vs Deployment template labels
    for svc in services:
        svc_name = svc.get("metadata", {}).get("name", "<unknown>")
        selector = ((svc.get("spec") or {}).get("selector")) or {}
        if not selector:
            continue
        matched = False
        for dep in deployments:
            labels = (
                (dep.get("spec") or {}).get("template", {}).get("metadata", {}).get("labels", {})
            ) or {}
            if labels and all(labels.get(k) == v for k, v in selector.items()):
                matched = True
                break
        if not matched:
            _warn(
                warnings,
                rule="selector-label-mismatch",
                severity="warn",
                resource=f"Service/{svc_name}",
                message=(
                    f"service selector {selector!r} does not match any Deployment "
                    "template labels in the overlay"
                ),
            )

    # Rule 5: env-var name collision within a container
    for dep in deployments:
        name = dep.get("metadata", {}).get("name", "<unknown>")
        for c in _deployment_containers(dep):
            seen: dict[str, int] = {}
            for entry in (c or {}).get("env") or []:
                env_name = (entry or {}).get("name")
                if not env_name:
                    continue
                seen[env_name] = seen.get(env_name, 0) + 1
            dupes = [k for k, v in seen.items() if v > 1]
            for d in dupes:
                _warn(
                    warnings,
                    rule="env-var-collision",
                    severity="error",
                    resource=f"Deployment/{name}",
                    message=(
                        f"container '{c.get('name', '<unnamed>')}' declares env '{d}' "
                        "more than once"
                    ),
                )

    return warnings


@deployment_bp.route("/validate-manifests", methods=["POST"])
@require_lifecycle_secret
def validate_deployment_manifests() -> tuple[Response, int]:
    """Static validation of the committed kustomize overlay."""
    runtime = _current_runtime()
    if runtime != "kubernetes":
        return _not_available_on_runtime()

    body = request.get_json(silent=True) or {}
    overlay = body.get("overlay_path") or _DEFAULT_OVERLAY

    # Resolve overlay relative to the repo root if a relative path was
    # passed.  The orchestrator container has the repo mounted at
    # /home/egg/repos/egg by default.  The final resolved path MUST
    # stay inside one of the recognised repo roots — otherwise an
    # authenticated caller could probe arbitrary filesystem paths via
    # 200/404 differentiation.
    repo_root_candidates = [
        p
        for p in (
            Path(os.environ.get("EGG_REPO_PATH") or ""),
            Path("/home/egg/repos/egg"),
            Path.cwd(),
        )
        if str(p)
    ]
    overlay_path = Path(overlay)
    if not overlay_path.is_absolute():
        for root in repo_root_candidates:
            if root and (root / overlay).exists():
                overlay_path = root / overlay
                break

    # Guard against path traversal — the resolved overlay must sit
    # under a known repo root.
    try:
        resolved = overlay_path.resolve()
        in_scope = any(
            resolved.is_relative_to(root.resolve()) for root in repo_root_candidates if root
        )
    except OSError, RuntimeError:
        in_scope = False
    if not in_scope:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "overlay_path must resolve under a known repo root",
                }
            ),
            400,
        )

    if not overlay_path.exists():
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"overlay not found: {overlay}",
                }
            ),
            404,
        )

    # Detect k3s so we know whether to apply k3s-specific rules.
    try:
        from kubernetes_client import get_kubernetes_client

        k8s = get_kubernetes_client()
        is_k3s, _hint = _detect_k3s(k8s)
    except Exception:
        is_k3s = False

    try:
        docs = _run_kustomize(overlay_path)
    except RuntimeError as exc:
        return (
            jsonify({"success": False, "message": str(exc)}),
            500,
        )

    warnings = _validate_deployment_docs(docs, is_k3s=is_k3s)
    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "overlay_path": str(overlay_path),
                    "is_k3s": bool(is_k3s),
                    "warnings": warnings,
                },
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# prune_stale_worktrees (orchestrator proxy)
# ---------------------------------------------------------------------------


@deployment_bp.route("/prune-worktrees", methods=["POST"])
@require_lifecycle_secret
def prune_worktrees_proxy() -> tuple[Response, int]:
    """Proxy to the gateway's worktree-prune endpoint.

    The gateway owns the filesystem mutation and its in-process mutex.
    The orchestrator layer is kept to enforce
    ``@require_lifecycle_secret`` (parity with #1769) and to shield
    agents from the launcher-secret needed to call the gateway
    directly.
    """
    try:
        from gateway_client import GatewayError, get_gateway_client
    except Exception as exc:  # pragma: no cover - wiring guard
        return jsonify({"success": False, "message": f"gateway unavailable: {exc}"}), 503

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))

    client = get_gateway_client()
    try:
        # _make_request is private but the simplest integration point;
        # all other gateway methods go through it.
        result = client._make_request(  # noqa: SLF001
            "/api/v1/worktrees/prune",
            method="POST",
            data={"dry_run": dry_run},
            use_launcher_auth=True,
            timeout=120,
        )
    except GatewayError as exc:
        status = getattr(exc, "status_code", 502) or 502
        return jsonify({"success": False, "message": str(exc)}), status
    except Exception as exc:
        return jsonify({"success": False, "message": f"gateway error: {exc}"}), 502

    return jsonify({"success": True, "data": result.get("data", result)}), 200


# ---------------------------------------------------------------------------
# validate_network_isolation
# ---------------------------------------------------------------------------

PROBE_COMMAND_TEMPLATE = r"""
set -u
gateway_url="${GATEWAY_URL:-http://gateway.egg-system.svc.cluster.local:9848}" # noqa: EGG002
orchestrator_url="${EGG_ORCHESTRATOR_URL:-http://orchestrator.egg-system.svc.cluster.local:9849}"

probe() {
  local url="$1"
  curl --silent --show-error --max-time 3 -o /dev/null -w '%{http_code}' "$url" || echo 000
}

gw=$(probe "$gateway_url/api/v1/health")
internet=$(probe "https://example.com/")
orch=$(probe "$orchestrator_url/api/v1/health")
peer=$(probe "http://1.2.3.4:80/")

python3 - <<PY
import json
print(json.dumps({
    "gateway_reachable": "$gw".startswith("2") or "$gw".startswith("3"),
    "internet_blocked": "$internet" == "000",
    "agent_pods_unreachable": "$peer" == "000",
    "orchestrator_direct_blocked": "$orch" == "000",
    "raw": {
        "gateway_status": "$gw",
        "internet_status": "$internet",
        "orchestrator_status": "$orch",
        "peer_status": "$peer",
    },
}))
PY
"""


def _build_probe_env() -> dict[str, str]:
    """Build the env dict for the throwaway probe Job.

    Explicitly omits secrets: no lifecycle secret, no session token,
    no gateway bearer.  Uses ``PROTECTED_ENV_KEYS`` from ``redaction``
    as the single source of truth for the denylist.
    """
    from redaction import PROTECTED_ENV_KEYS as _PROTECTED_ENV_KEYS

    # We only expose the two URLs the probe script needs. Both happen to
    # also appear in _PROTECTED_ENV_KEYS (they're locked against agent
    # override in production); here the probe legitimately needs them.
    # All other environment is discarded.
    safe: dict[str, str] = {
        "GATEWAY_URL": os.environ.get("GATEWAY_URL", ""),
        "EGG_ORCHESTRATOR_URL": os.environ.get("EGG_ORCHESTRATOR_URL", ""),
    }
    # Double-check: nothing else sensitive sneaks in.
    for key in list(safe.keys()):
        if key in _PROTECTED_ENV_KEYS and key not in {
            "GATEWAY_URL",
            "EGG_ORCHESTRATOR_URL",
        }:
            safe.pop(key, None)
    return safe


def _build_probe_job_manifest(
    pipeline_id: str,
    role: str,
    probe_id: str,
    image: str,
) -> dict[str, Any]:
    """Construct the V1Job body for the isolation probe as a plain dict.

    Returning a dict rather than a ``V1Job`` keeps the function unit-testable
    without depending on the kubernetes SDK.  The caller converts to a
    V1 object when submitting.
    """
    env = _build_probe_env()
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"egg-probe-{probe_id}",
            "labels": {
                "app.kubernetes.io/component": "agent",
                "egg.probe": "true",
                "egg.io/probe-id": probe_id,
                "egg.pipeline.id": pipeline_id,
                "egg.agent.role": role,
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 0,
            "activeDeadlineSeconds": 30,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/component": "agent",
                        "egg.probe": "true",
                        "egg.io/probe-id": probe_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c", PROBE_COMMAND_TEMPLATE],
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def _submit_probe_job(k8s: Any, namespace: str, probe_id: str, manifest: dict[str, Any]) -> None:
    from kubernetes import client as k8s_client_pkg

    body = k8s_client_pkg.V1Job(
        api_version=manifest["apiVersion"],
        kind=manifest["kind"],
        metadata=k8s_client_pkg.V1ObjectMeta(
            name=manifest["metadata"]["name"],
            labels=manifest["metadata"]["labels"],
        ),
        spec=k8s_client_pkg.V1JobSpec(
            ttl_seconds_after_finished=manifest["spec"]["ttlSecondsAfterFinished"],
            active_deadline_seconds=manifest["spec"]["activeDeadlineSeconds"],
            backoff_limit=manifest["spec"]["backoffLimit"],
            template=k8s_client_pkg.V1PodTemplateSpec(
                metadata=k8s_client_pkg.V1ObjectMeta(
                    labels=manifest["spec"]["template"]["metadata"]["labels"],
                ),
                spec=k8s_client_pkg.V1PodSpec(
                    restart_policy="Never",
                    automount_service_account_token=False,
                    containers=[
                        k8s_client_pkg.V1Container(
                            name="probe",
                            image=manifest["spec"]["template"]["spec"]["containers"][0]["image"],
                            image_pull_policy="IfNotPresent",
                            command=manifest["spec"]["template"]["spec"]["containers"][0][
                                "command"
                            ],
                            env=[
                                k8s_client_pkg.V1EnvVar(name=e["name"], value=e["value"])
                                for e in manifest["spec"]["template"]["spec"]["containers"][0][
                                    "env"
                                ]
                            ],
                            security_context=k8s_client_pkg.V1SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s_client_pkg.V1Capabilities(drop=["ALL"]),
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    k8s.batch_api.create_namespaced_job(namespace=namespace, body=body)


def _wait_for_probe_pod(k8s: Any, namespace: str, probe_id: str, *, timeout: float) -> Any:
    """Return the probe pod once it is Succeeded/Failed or None on timeout."""
    selector = f"egg.io/probe-id={probe_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pods = k8s.core_api.list_namespaced_pod(namespace=namespace, label_selector=selector)
        except Exception:
            time.sleep(1.0)
            continue
        for pod in getattr(pods, "items", []) or []:
            phase = (getattr(pod, "status", None) and pod.status.phase) or ""
            if phase in {"Succeeded", "Failed"}:
                return pod
        time.sleep(1.0)
    return None


def _read_probe_log(k8s: Any, namespace: str, pod_name: str) -> str:
    try:
        raw = k8s.core_api.read_namespaced_pod_log(name=pod_name, namespace=namespace)
    except Exception as exc:
        logger.warning("probe log read failed", pod=pod_name, error=str(exc))
        return ""
    return str(raw) if raw is not None else ""


def _delete_probe_job(k8s: Any, namespace: str, probe_id: str) -> None:
    """Best-effort cleanup. Never raises."""
    name = f"egg-probe-{probe_id}"
    try:
        from kubernetes import client as k8s_client_pkg

        k8s.batch_api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body=k8s_client_pkg.V1DeleteOptions(
                propagation_policy="Background", grace_period_seconds=0
            ),
        )
    except Exception as exc:
        logger.info("probe job cleanup skipped", probe=name, error=str(exc))


def _parse_probe_output(raw_log: str) -> dict[str, Any]:
    """Extract the JSON emitted by the probe pod.

    The probe script prints a single JSON object.  Log drivers
    sometimes add a trailing newline; tolerate that.
    """
    import json

    if not raw_log:
        return {"error": "no_probe_output"}
    for line in reversed(raw_log.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"error": "probe_output_unparseable", "raw": raw_log[-512:]}


@deployment_bp.route("/validate-network-isolation", methods=["POST"])
@require_lifecycle_secret
def validate_network_isolation() -> tuple[Response, int]:
    """Spawn a throwaway probe Job and report isolation results."""
    if _current_runtime() != "kubernetes":
        return _not_available_on_runtime()

    body = request.get_json(silent=True) or {}
    pipeline_id = str(body.get("pipeline_id") or "manual")
    role = str(body.get("role") or "coder")
    namespace = os.environ.get("EGG_AGENTS_NAMESPACE", "egg-agents")

    # Guard against invalid K8s label values.  Labels must match
    # ^[a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?$ — failing Job
    # creation otherwise returns an opaque 400 from the apiserver.
    if not _K8S_LABEL_VALUE_RE.match(pipeline_id):
        return (
            jsonify(
                {
                    "success": False,
                    "message": (
                        "pipeline_id is not a valid Kubernetes label value "
                        "(must match [a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?)"
                    ),
                }
            ),
            400,
        )
    if not _K8S_LABEL_VALUE_RE.match(role):
        return (
            jsonify(
                {
                    "success": False,
                    "message": "role is not a valid Kubernetes label value",
                }
            ),
            400,
        )

    try:
        from kubernetes_client import get_kubernetes_client
    except Exception as exc:
        return jsonify({"success": False, "message": f"kubernetes unavailable: {exc}"}), 503

    try:
        k8s = get_kubernetes_client()
    except Exception as exc:
        return jsonify({"success": False, "message": f"kubernetes init failed: {exc}"}), 503

    # CNI gating per DEP-3: refuse to run the probe when enforcement is
    # not detected so we don't return misleading results.
    _cni, enforcement = _detect_cni(k8s)
    if not enforcement:
        return (
            jsonify(
                {
                    "success": True,
                    "data": {
                        "error": "network_policy_enforcement_not_detected",
                        "cni": _cni,
                    },
                }
            ),
            200,
        )

    probe_id = uuid.uuid4().hex[:12]
    image = os.environ.get("EGG_SANDBOX_IMAGE", "egg:latest")
    manifest = _build_probe_job_manifest(
        pipeline_id=pipeline_id, role=role, probe_id=probe_id, image=image
    )

    try:
        _submit_probe_job(k8s, namespace, probe_id, manifest)
    except Exception as exc:
        logger.error("probe submit failed", error=str(exc))
        return jsonify({"success": False, "message": f"probe submit failed: {exc}"}), 500

    try:
        pod = _wait_for_probe_pod(k8s, namespace, probe_id, timeout=30.0)
        if pod is None:
            return (
                jsonify(
                    {
                        "success": True,
                        "data": {
                            "error": "probe_timeout",
                            "probe_id": probe_id,
                        },
                    }
                ),
                200,
            )

        pod_name = (getattr(pod, "metadata", None) and pod.metadata.name) or ""
        log = _read_probe_log(k8s, namespace, pod_name)
        parsed = _parse_probe_output(log)

        return (
            jsonify(
                {
                    "success": True,
                    "data": {
                        "probe_id": probe_id,
                        "namespace": namespace,
                        "result": parsed,
                    },
                }
            ),
            200,
        )
    finally:
        _delete_probe_job(k8s, namespace, probe_id)


# ---------------------------------------------------------------------------
# rebuild_and_rollout
# ---------------------------------------------------------------------------

# Single in-process guard. The rebuild writes images to the node's
# containerd cache; running two at once is never what the caller
# wanted.
_REBUILD_LOCK = threading.Lock()
_REBUILD_IN_PROGRESS = False
_REBUILD_ACTIVE_STREAM_ID: str | None = None

# Per-stream buffer.  Each stream is a deque of events that the consumer
# reads via GET /api/v1/deployment/rebuild-and-rollout/streams/<id>.
# Unbounded within a single stream — the retention reaper caps total
# stream count and streams are short-lived (bounded by
# _REDEPLOY_SUBPROCESS_TIMEOUT_SEC).  A maxlen here would silently
# evict old events while the cursor (next_since) keeps incrementing,
# causing positional slices to return empty batches.
_STREAM_BUFFERS: dict[str, deque[dict[str, Any]]] = {}
_STREAM_TERMINATION_TS: dict[str, float] = {}
_STREAM_LOCK = threading.Lock()
_STREAM_TERMINATED: set[str] = set()

# How many finished streams we keep around so a wait=true consumer can
# still fetch the terminal events after the worker exited.  Older
# entries are evicted FIFO — _STREAM_BUFFERS must not grow unbounded
# across a long-running orchestrator (see #1759 review MEDIUM-3).
_STREAM_RETENTION = 16

# Hard timeout for the redeploy subprocess.  If ``make redeploy`` hangs
# (stuck docker build, hung k3s ctr import, network wedge) the
# watchdog kills it, emits a structured ``phase: "timeout"`` event, and
# clears _REBUILD_IN_PROGRESS so the next caller is not wedged at 409
# forever.  30 min is a generous upper bound for a clean rebuild.
_REDEPLOY_SUBPROCESS_TIMEOUT_SEC = 1800


def _stream_append(stream_id: str, event: dict[str, Any]) -> None:
    with _STREAM_LOCK:
        buf = _STREAM_BUFFERS.setdefault(stream_id, deque())
        buf.append(event)


def _stream_mark_done(stream_id: str) -> None:
    import time as _time

    with _STREAM_LOCK:
        _STREAM_TERMINATED.add(stream_id)
        _STREAM_TERMINATION_TS[stream_id] = _time.monotonic()
        _reap_stale_streams_locked()


def _reap_stale_streams_locked() -> None:
    """Evict terminated streams beyond the retention cap. Lock-held."""
    if len(_STREAM_TERMINATION_TS) <= _STREAM_RETENTION:
        return
    # Oldest first.
    ordered = sorted(_STREAM_TERMINATION_TS.items(), key=lambda kv: kv[1])
    overflow = len(ordered) - _STREAM_RETENTION
    for stream_id, _ts in ordered[:overflow]:
        _STREAM_BUFFERS.pop(stream_id, None)
        _STREAM_TERMINATED.discard(stream_id)
        _STREAM_TERMINATION_TS.pop(stream_id, None)


def _stream_is_done(stream_id: str) -> bool:
    with _STREAM_LOCK:
        return stream_id in _STREAM_TERMINATED


def _stream_snapshot(stream_id: str, since: int = 0) -> tuple[list[dict[str, Any]], bool]:
    with _STREAM_LOCK:
        buf = _STREAM_BUFFERS.get(stream_id)
        if buf is None:
            return [], stream_id in _STREAM_TERMINATED
        events = list(buf)[since:]
        done = stream_id in _STREAM_TERMINATED
        return events, done


def _run_redeploy_subprocess(
    stream_id: str,
    cwd: str,
    *,
    runner: Any = None,
    timeout_sec: int | None = None,
) -> None:
    """Execute ``make redeploy`` and pipe progress events to the stream.

    Emits events of shape::

        {"ts": "<isoformat>", "phase": "line", "line": "..."}

    and terminates with a ``{"phase": "done", "exit_code": N,
    "rolled_out_images": {...}}`` record.

    A watchdog kills the subprocess after *timeout_sec* seconds (default
    :data:`_REDEPLOY_SUBPROCESS_TIMEOUT_SEC`) so a wedged
    ``make redeploy`` never leaves ``_REBUILD_IN_PROGRESS`` pinned true
    — review MEDIUM-1 in #1759.

    *runner* may be overridden for testing — any callable with the
    signature of :func:`subprocess.Popen`.
    """
    global _REBUILD_IN_PROGRESS, _REBUILD_ACTIVE_STREAM_ID

    from datetime import UTC, datetime

    popen = runner or subprocess.Popen
    effective_timeout = timeout_sec if timeout_sec is not None else _REDEPLOY_SUBPROCESS_TIMEOUT_SEC
    deadline = time.monotonic() + effective_timeout
    exit_code = -1
    rolled_out: dict[str, str] = {}
    timed_out = False
    proc: Any = None

    def _watchdog() -> None:
        # Sleep until the deadline, then kill the process if still alive.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if proc is None or proc.poll() is not None:
            return
        nonlocal timed_out
        timed_out = True
        _stream_append(
            stream_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "phase": "timeout",
                "message": (f"make redeploy exceeded {effective_timeout}s, killing subprocess"),
            },
        )
        try:
            proc.kill()
        except Exception:  # pragma: no cover - defensive
            pass

    watchdog_thread: threading.Thread | None = None

    try:
        proc = popen(
            ["make", "redeploy"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        watchdog_thread = threading.Thread(
            target=_watchdog,
            daemon=True,
            name=f"rebuild-watchdog-{stream_id}",
        )
        watchdog_thread.start()

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            _stream_append(
                stream_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "phase": "line",
                    "line": line,
                },
            )
            # Detect image import lines for the final summary. Matches
            # lines emitted by `k3s ctr images import`.
            if "unpacking" in line and "sha256:" in line:
                # best-effort: parse "unpacking docker.io/library/egg-xxx:tag"
                parts = line.split()
                for p in parts:
                    if ":" in p and ("egg-" in p or "egg:" in p):
                        rolled_out[p] = "imported"
                        break

        exit_code = proc.wait()
    except Exception as exc:  # pragma: no cover - very defensive
        _stream_append(
            stream_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "phase": "error",
                "message": str(exc),
            },
        )
    finally:
        _stream_append(
            stream_id,
            {
                "ts": datetime.now(UTC).isoformat(),
                "phase": "done",
                "exit_code": exit_code,
                "rolled_out_images": rolled_out,
                "timed_out": timed_out,
            },
        )
        _stream_mark_done(stream_id)
        with _REBUILD_LOCK:
            _REBUILD_IN_PROGRESS = False
            _REBUILD_ACTIVE_STREAM_ID = None


@deployment_bp.route("/rebuild-and-rollout", methods=["POST"])
@require_lifecycle_secret
def rebuild_and_rollout() -> tuple[Response, int]:
    """Kick off ``make redeploy`` asynchronously and return a stream handle.

    Safeties:
    - Gated on ``EGG_RUNTIME=kubernetes`` (docker returns ``not_available_on_runtime``).
    - Refuses with ``runtime_detection_failed`` when the process claims
      kubernetes but can't reach the apiserver — kicking off
      ``make redeploy`` against a nonexistent cluster just wastes cycles
      and produces confusing output (#1850).
    - Rejects concurrent invocations while a rollout is live
      (returns 409 with the existing stream id).
    - Actual subprocess runs in a background thread so the HTTP
      request returns immediately; the MCP tool call stays inside
      FastMCP's ~60 s budget.
    """
    global _REBUILD_IN_PROGRESS, _REBUILD_ACTIVE_STREAM_ID

    if _current_runtime() != "kubernetes":
        return _not_available_on_runtime()

    reachable, reason = _probe_kubernetes_reachable()
    if not reachable:
        return _runtime_detection_failed(reason or "apiserver unreachable")

    cwd = os.environ.get("EGG_REPO_PATH") or "/home/egg/repos/egg"
    if not Path(cwd).exists():
        return (
            jsonify({"success": False, "message": f"EGG_REPO_PATH not found: {cwd}"}),
            500,
        )

    with _REBUILD_LOCK:
        if _REBUILD_IN_PROGRESS:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "rollout_already_in_progress",
                        "data": {
                            "error": "rollout_already_in_progress",
                            "progress_stream_id": _REBUILD_ACTIVE_STREAM_ID,
                        },
                    }
                ),
                409,
            )
        stream_id = uuid.uuid4().hex[:16]
        _REBUILD_IN_PROGRESS = True
        _REBUILD_ACTIVE_STREAM_ID = stream_id

    # Start the worker thread. The subprocess runs in the orchestrator
    # container which owns the repo bind mount.
    thread = threading.Thread(
        target=_run_redeploy_subprocess,
        args=(stream_id, cwd),
        daemon=True,
        name=f"rebuild-{stream_id}",
    )
    thread.start()

    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "progress_stream_id": stream_id,
                    "started_at": time.time(),
                },
            }
        ),
        202,
    )


@deployment_bp.route("/rebuild-and-rollout/streams/<stream_id>", methods=["GET"])
@require_lifecycle_secret
def rebuild_stream_read(stream_id: str) -> tuple[Response, int]:
    """Return buffered progress events for *stream_id*.

    Query ``since`` (integer index, default 0) lets callers fetch only
    new events.  The ``done`` flag tells the caller whether the worker
    has terminated — useful for the MCP ``wait=true`` mode.
    """
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0

    events, done = _stream_snapshot(stream_id, since=since)
    if not events and not done and stream_id not in _STREAM_BUFFERS:
        return jsonify({"success": False, "message": "stream not found"}), 404

    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "stream_id": stream_id,
                    "events": events,
                    "next_since": since + len(events),
                    "done": done,
                },
            }
        ),
        200,
    )


__all__ = [
    "deployment_bp",
    "PROBE_COMMAND_TEMPLATE",
    "_build_probe_env",
    "_build_probe_job_manifest",
    "_validate_deployment_docs",
    "_build_deployment_context_payload",
    "_run_redeploy_subprocess",
    "_stream_snapshot",
    "_stream_append",
    "_stream_mark_done",
    "_STREAM_BUFFERS",
    "_STREAM_TERMINATED",
    "_SERVICE_LOG_ALLOWLIST",
    "_MAX_LOG_LINES",
]
