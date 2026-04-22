"""Backward-compatibility shim for container_spawner.

This module re-exports the KubernetesSpawner under the old
ContainerSpawner names so that existing imports continue to work
after the Docker-to-Kubernetes migration.
"""

from kubernetes_spawner import (
    WORKTREE_BASE_DIR,
    KubernetesSpawner,
    KubernetesSpawnError,
    SpawnedContainer,
    SpawnFailureError,
    _host_to_local_volumes,
    get_kubernetes_spawner,
)

# Alias Docker names to Kubernetes equivalents
ContainerSpawner = KubernetesSpawner
ContainerSpawnError = KubernetesSpawnError


def get_container_spawner(**kwargs):
    """Return a KubernetesSpawner instance (backward-compat alias)."""
    return get_kubernetes_spawner(**kwargs)


__all__ = [
    "ContainerSpawner",
    "ContainerSpawnError",
    "SpawnedContainer",
    "SpawnFailureError",
    "WORKTREE_BASE_DIR",
    "_host_to_local_volumes",
    "get_container_spawner",
]
