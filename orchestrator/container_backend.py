"""
Container runtime backend protocol.

Defines the ContainerBackend Protocol that both DockerClient and
KubernetesClient must satisfy, enabling runtime-agnostic container
management.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from models import ContainerInfo, ContainerStatus

# Re-export ContainerStatus for convenience
__all__ = [
    "ContainerBackend",
    "ContainerBackendError",
    "ContainerStatus",
    "ImagePullError",
    "JobOperationError",
    "KubernetesClientError",
    "PodNotFoundError",
]


class ContainerBackendError(Exception):
    """Base exception for container backend errors."""

    pass


class KubernetesClientError(ContainerBackendError):
    """Base exception for Kubernetes client errors."""

    pass


class PodNotFoundError(KubernetesClientError):
    """Pod not found in the cluster."""

    pass


class JobOperationError(KubernetesClientError):
    """Kubernetes Job operation failed."""

    pass


class ImagePullError(KubernetesClientError):
    """Failed to pull container image."""

    pass


@runtime_checkable
class ContainerBackend(Protocol):
    """Protocol defining the container runtime interface.

    Both DockerClient and KubernetesClient must satisfy this protocol,
    enabling the orchestrator to manage containers without knowing
    which backend is in use.
    """

    def is_connected(self) -> bool:
        """Check if the container runtime is available.

        Returns:
            True if the backend is accessible.
        """
        ...

    def create_container(
        self,
        name: str,
        image: str | None = None,
        environment: dict[str, str] | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        network: str | None = None,
        command: list[str] | None = None,
        labels: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> ContainerInfo:
        """Create a new container.

        Args:
            name: Container name.
            image: Container image.
            environment: Environment variables.
            volumes: Volume mounts.
            network: Network to connect to.
            command: Command to run.
            labels: Container labels.
            **kwargs: Additional backend-specific arguments.

        Returns:
            ContainerInfo with container details.
        """
        ...

    def start_container(self, container_id: str) -> ContainerInfo:
        """Start a container.

        Args:
            container_id: Container ID or name.

        Returns:
            Updated ContainerInfo.
        """
        ...

    def stop_container(self, container_id: str, timeout: int = 10) -> ContainerInfo:
        """Stop a container.

        Args:
            container_id: Container ID or name.
            timeout: Seconds to wait before force-stopping.

        Returns:
            Updated ContainerInfo.
        """
        ...

    def remove_container(self, container_id: str, force: bool = False, v: bool = True) -> None:
        """Remove a container.

        Args:
            container_id: Container ID or name.
            force: Force removal of running container.
            v: Remove associated volumes.
        """
        ...

    def get_container_info(self, container_id: str) -> ContainerInfo:
        """Get container information.

        Args:
            container_id: Container ID or name.

        Returns:
            ContainerInfo with current state.
        """
        ...

    def list_containers(
        self,
        all: bool = True,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List containers matching filters.

        Args:
            all: Include stopped containers.
            labels: Label filters.

        Returns:
            List of ContainerInfo.
        """
        ...

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 100,
        since: datetime | None = None,
    ) -> str:
        """Get container logs.

        Args:
            container_id: Container ID or name.
            tail: Number of lines from the end.
            since: Only logs since this time.

        Returns:
            Log output as string.
        """
        ...

    def wait_for_container(self, container_id: str, timeout: int = 300) -> ContainerInfo:
        """Wait for container to exit.

        Args:
            container_id: Container ID or name.
            timeout: Max seconds to wait.

        Returns:
            ContainerInfo with exit status.
        """
        ...

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int:
        """Remove orphaned containers older than max_age_hours.

        Args:
            max_age_hours: Max age before considering orphaned.

        Returns:
            Number of containers removed.
        """
        ...
