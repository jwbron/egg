"""Cluster-introspection detectors for the deployment blueprint (#3312).

k3s flavor detection, CNI / NetworkPolicy-enforcement inference, and egg
image-tag collection — all read-only probes against the apiserver. These
are the ``patch("routes.deployment._detect_*")`` seams the route tests
stub, so they are re-exported through the barrel and reached via ``_pkg``
from the route bodies.
"""

from __future__ import annotations

from typing import Any

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
