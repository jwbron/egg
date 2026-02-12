"""
Docker API client for container operations.

Provides container lifecycle management (create, start, stop, remove)
for sandbox containers spawned by the orchestrator.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.docker")


class DockerClientError(Exception):
    """Base exception for Docker client errors."""

    pass


class ContainerNotFoundError(DockerClientError):
    """Container not found."""

    pass


class ContainerOperationError(DockerClientError):
    """Container operation failed."""

    pass


class ImageNotFoundError(DockerClientError):
    """Docker image not found."""

    pass


class InvalidContainerIdError(DockerClientError):
    """Invalid container ID format."""

    pass


# Valid container ID pattern: 64-char hex (full) or 12-char hex (short), or container name
# Container names: alphanumeric, underscore, hyphen, period (cannot start with hyphen/period)
CONTAINER_ID_PATTERN = re.compile(r"^[a-fA-F0-9]{12,64}$|^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _validate_container_id(container_id: str) -> None:
    """Validate container ID format to prevent injection attacks.

    Args:
        container_id: Container ID or name to validate

    Raises:
        InvalidContainerIdError: If container ID format is invalid
    """
    if not container_id or not CONTAINER_ID_PATTERN.match(container_id):
        raise InvalidContainerIdError(f"Invalid container ID format: {container_id}")


class DockerClient:
    """Docker API client for sandbox container management.

    Wraps the Docker SDK to provide simplified container operations
    for the orchestrator.
    """

    DEFAULT_SANDBOX_IMAGE = "egg:latest"
    CONTAINER_PREFIX = "egg-sandbox-"

    def __init__(self, docker_host: str | None = None):
        """Initialize Docker client.

        Args:
            docker_host: Docker host URL (default: from environment or unix socket)
        """
        self.docker_host = docker_host or os.environ.get("DOCKER_HOST")

        try:
            if self.docker_host:
                self.client = docker.DockerClient(base_url=self.docker_host)
            else:
                self.client = docker.from_env()
        except DockerException as e:
            raise DockerClientError(f"Failed to connect to Docker: {e}") from e

    def is_connected(self) -> bool:
        """Check if Docker is available.

        Returns:
            True if Docker daemon is accessible
        """
        try:
            self.client.ping()
            return True
        except DockerException:
            return False

    def get_image(self, image_name: str) -> Any | None:
        """Get a Docker image.

        Args:
            image_name: Image name with tag

        Returns:
            Image object or None if not found
        """
        try:
            return self.client.images.get(image_name)
        except ImageNotFound:
            return None

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
            name: Container name
            image: Docker image (default: egg-sandbox:latest)
            environment: Environment variables
            volumes: Volume mounts
            network: Network to connect to
            command: Command to run
            labels: Container labels
            **kwargs: Additional docker run arguments

        Returns:
            ContainerInfo with container details

        Raises:
            ImageNotFoundError: If image doesn't exist
            ContainerOperationError: If creation fails
        """
        image = image or self.DEFAULT_SANDBOX_IMAGE
        container_name = f"{self.CONTAINER_PREFIX}{name}"

        # Merge default labels with provided labels
        container_labels = {
            "egg.orchestrator": "true",
            "egg.container.name": name,
            "egg.created_at": datetime.utcnow().isoformat(),
        }
        if labels:
            container_labels.update(labels)

        try:
            # Check if image exists
            if not self.get_image(image):
                raise ImageNotFoundError(f"Image {image} not found")

            container = self.client.containers.create(
                image=image,
                name=container_name,
                environment=environment or {},
                volumes=volumes or {},
                network=network,
                command=command,
                labels=container_labels,
                detach=True,
                **kwargs,
            )

            logger.info(
                "Container created",
                container_id=container.id[:12],
                container_name=container_name,
                image=image,
            )

            return ContainerInfo(
                container_id=container.id,
                container_name=container_name,
                status=ContainerStatus.PENDING,
            )

        except ImageNotFound as e:
            raise ImageNotFoundError(f"Image {image} not found") from e
        except APIError as e:
            raise ContainerOperationError(f"Failed to create container: {e}") from e

    def start_container(self, container_id: str) -> ContainerInfo:
        """Start a container.

        Args:
            container_id: Container ID

        Returns:
            Updated ContainerInfo

        Raises:
            InvalidContainerIdError: If container ID format is invalid
            ContainerNotFoundError: If container doesn't exist
            ContainerOperationError: If start fails
        """
        _validate_container_id(container_id)
        try:
            container = self.client.containers.get(container_id)
            container.start()

            logger.info("Container started", container_id=container_id[:12])

            return ContainerInfo(
                container_id=container.id,
                container_name=container.name,
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
            )

        except NotFound as e:
            raise ContainerNotFoundError(f"Container {container_id} not found") from e
        except APIError as e:
            raise ContainerOperationError(f"Failed to start container: {e}") from e

    def stop_container(
        self,
        container_id: str,
        timeout: int = 10,
    ) -> ContainerInfo:
        """Stop a container.

        Args:
            container_id: Container ID
            timeout: Seconds to wait before killing

        Returns:
            Updated ContainerInfo

        Raises:
            InvalidContainerIdError: If container ID format is invalid
            ContainerNotFoundError: If container doesn't exist
            ContainerOperationError: If stop fails
        """
        _validate_container_id(container_id)
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=timeout)

            # Reload to get updated state
            container.reload()
            exit_code = container.attrs.get("State", {}).get("ExitCode")

            logger.info(
                "Container stopped",
                container_id=container_id[:12],
                exit_code=exit_code,
            )

            return ContainerInfo(
                container_id=container.id,
                container_name=container.name,
                status=ContainerStatus.EXITED,
                exit_code=exit_code,
                exited_at=datetime.utcnow(),
            )

        except NotFound as e:
            raise ContainerNotFoundError(f"Container {container_id} not found") from e
        except APIError as e:
            raise ContainerOperationError(f"Failed to stop container: {e}") from e

    def remove_container(
        self,
        container_id: str,
        force: bool = False,
        v: bool = True,
    ) -> None:
        """Remove a container.

        Args:
            container_id: Container ID
            force: Force removal of running container
            v: Remove associated volumes

        Raises:
            InvalidContainerIdError: If container ID format is invalid
            ContainerNotFoundError: If container doesn't exist
            ContainerOperationError: If removal fails
        """
        _validate_container_id(container_id)
        try:
            container = self.client.containers.get(container_id)
            container.remove(force=force, v=v)

            logger.info("Container removed", container_id=container_id[:12])

        except NotFound as e:
            raise ContainerNotFoundError(f"Container {container_id} not found") from e
        except APIError as e:
            raise ContainerOperationError(f"Failed to remove container: {e}") from e

    def get_container_info(self, container_id: str) -> ContainerInfo:
        """Get container information.

        Args:
            container_id: Container ID

        Returns:
            ContainerInfo with current state

        Raises:
            InvalidContainerIdError: If container ID format is invalid
            ContainerNotFoundError: If container doesn't exist
        """
        _validate_container_id(container_id)
        try:
            container = self.client.containers.get(container_id)
            container.reload()

            state = container.attrs.get("State", {})
            status_str = state.get("Status", "unknown")

            # Map Docker status to our status enum
            if status_str == "running":
                status = ContainerStatus.RUNNING
            elif status_str == "exited":
                status = ContainerStatus.EXITED
            elif status_str == "created":
                status = ContainerStatus.PENDING
            else:
                status = ContainerStatus.FAILED

            # Parse timestamps
            started_at = None
            exited_at = None
            if state.get("StartedAt"):
                try:
                    started_at = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
                except ValueError:
                    pass
            if state.get("FinishedAt") and state["FinishedAt"] != "0001-01-01T00:00:00Z":
                try:
                    exited_at = datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Get agent role from labels
            labels = container.attrs.get("Config", {}).get("Labels", {})
            agent_role_str = labels.get("egg.agent.role")

            from models import AgentRole

            agent_role = None
            if agent_role_str:
                try:
                    agent_role = AgentRole(agent_role_str)
                except ValueError:
                    pass

            return ContainerInfo(
                container_id=container.id,
                container_name=container.name,
                status=status,
                started_at=started_at,
                exited_at=exited_at,
                exit_code=state.get("ExitCode"),
                agent_role=agent_role,
            )

        except NotFound as e:
            raise ContainerNotFoundError(f"Container {container_id} not found") from e

    def list_containers(
        self,
        all: bool = True,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List containers matching filters.

        Args:
            all: Include stopped containers
            labels: Label filters

        Returns:
            List of ContainerInfo
        """
        filters: dict[str, Any] = {"label": ["egg.orchestrator=true"]}
        if labels:
            for key, value in labels.items():
                filters["label"].append(f"{key}={value}")

        containers = self.client.containers.list(all=all, filters=filters)

        return [self.get_container_info(c.id) for c in containers]

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 100,
        since: datetime | None = None,
    ) -> str:
        """Get container logs.

        Args:
            container_id: Container ID
            tail: Number of lines from the end
            since: Only logs since this time

        Returns:
            Log output as string

        Raises:
            InvalidContainerIdError: If container ID format is invalid
            ContainerNotFoundError: If container doesn't exist
        """
        _validate_container_id(container_id)
        try:
            container = self.client.containers.get(container_id)
            logs = container.logs(tail=tail, since=since, timestamps=True)
            return logs.decode("utf-8", errors="replace")

        except NotFound as e:
            raise ContainerNotFoundError(f"Container {container_id} not found") from e

    def wait_for_container(
        self,
        container_id: str,
        timeout: int = 300,
    ) -> ContainerInfo:
        """Wait for container to exit.

        Args:
            container_id: Container ID
            timeout: Max seconds to wait

        Returns:
            ContainerInfo with exit status

        Raises:
            InvalidContainerIdError: If container ID format is invalid
            ContainerNotFoundError: If container doesn't exist
            ContainerOperationError: If timeout exceeded
        """
        _validate_container_id(container_id)
        try:
            container = self.client.containers.get(container_id)

            # Wait with timeout
            result = container.wait(timeout=timeout)

            return ContainerInfo(
                container_id=container.id,
                container_name=container.name,
                status=ContainerStatus.EXITED,
                exit_code=result.get("StatusCode"),
                exited_at=datetime.utcnow(),
            )

        except NotFound as e:
            raise ContainerNotFoundError(f"Container {container_id} not found") from e
        except Exception as e:
            raise ContainerOperationError(f"Wait failed: {e}") from e

    def cleanup_orphaned_containers(
        self,
        max_age_hours: int = 24,
    ) -> int:
        """Remove orphaned orchestrator containers.

        Args:
            max_age_hours: Max age before considering orphaned

        Returns:
            Number of containers removed
        """
        removed = 0
        cutoff = datetime.utcnow()

        for container in self.list_containers(all=True):
            # Check if exited and old enough
            if container.status == ContainerStatus.EXITED:
                if container.exited_at:
                    age_hours = (cutoff - container.exited_at).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        try:
                            self.remove_container(container.container_id)
                            removed += 1
                        except ContainerOperationError:
                            pass

        if removed:
            logger.info("Cleaned up orphaned containers", count=removed)

        return removed


_docker_client: DockerClient | None = None


def get_docker_client() -> DockerClient:
    """Get the singleton Docker client.

    Returns:
        DockerClient instance
    """
    global _docker_client
    if _docker_client is None:
        _docker_client = DockerClient()
    return _docker_client
