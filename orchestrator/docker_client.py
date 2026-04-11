"""Backward-compatibility shim for docker_client.

This module re-exports the Kubernetes client under the old Docker
client names so that existing imports continue to work after the
Docker-to-Kubernetes migration.

All Docker-specific classes and functions are aliased to their
Kubernetes equivalents.
"""

from kubernetes_client import (
    ImagePullError,
    JobOperationError,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
    get_kubernetes_client,
)

# Alias Docker names to Kubernetes equivalents
DockerClient = KubernetesClient
DockerClientError = KubernetesClientError
ContainerNotFoundError = PodNotFoundError
ContainerOperationError = JobOperationError
ImageNotFoundError = ImagePullError
InvalidContainerIdError = KubernetesClientError  # No direct equivalent


def get_docker_client(**kwargs):
    """Return a KubernetesClient instance (backward-compat alias)."""
    return get_kubernetes_client(**kwargs)


__all__ = [
    "DockerClient",
    "DockerClientError",
    "ContainerNotFoundError",
    "ContainerOperationError",
    "ImageNotFoundError",
    "InvalidContainerIdError",
    "get_docker_client",
]
