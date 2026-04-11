"""Backward-compatibility shim for container_monitor.

This module re-exports the KubernetesMonitor under the old
ContainerMonitor names so that existing imports continue to work
after the Docker-to-Kubernetes migration.
"""

from kubernetes_monitor import (
    ContainerEvent,
    EventHandler,
    KubernetesMonitor,
    _reconcile_pod_state,
    create_pipeline_reconciliation_handler,
    get_kubernetes_monitor,
)

# Alias Docker names to Kubernetes equivalents
ContainerMonitor = KubernetesMonitor

# Map old name to new implementation
_reconcile_container_state = _reconcile_pod_state


def get_container_monitor(**kwargs):
    """Return a KubernetesMonitor instance (backward-compat alias)."""
    return get_kubernetes_monitor(**kwargs)


__all__ = [
    "ContainerMonitor",
    "ContainerEvent",
    "EventHandler",
    "_reconcile_container_state",
    "create_pipeline_reconciliation_handler",
    "get_container_monitor",
]
