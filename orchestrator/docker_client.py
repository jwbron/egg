"""Backward-compatibility shim for docker_client.

This module re-exports the Kubernetes client under the old Docker
client names so that existing imports continue to work after the
Docker-to-Kubernetes migration.

All Docker-specific classes and functions are aliased to their
Kubernetes equivalents.
"""

import re

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

# Regex for valid container/job identifiers (alphanumeric, hyphens, underscores, dots)
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _validate_container_id(container_id: str | None) -> None:
    """Validate a container/job identifier string.

    Backward-compat shim: the original Docker client validated Docker
    container IDs; this version validates Kubernetes-compatible names.

    Raises:
        InvalidContainerIdError: If the ID is empty, None, or contains
            shell-unsafe characters.
    """
    if not container_id:
        raise InvalidContainerIdError("Container ID must not be empty or None")
    if not isinstance(container_id, str):
        raise InvalidContainerIdError(f"Container ID must be a string, got {type(container_id)}")
    if not _VALID_ID_RE.match(container_id):
        raise InvalidContainerIdError(
            f"Invalid container ID: {container_id!r} — "
            "must contain only alphanumeric characters, hyphens, underscores, and dots"
        )


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
    "_validate_container_id",
    "get_docker_client",
]
