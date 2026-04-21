"""
Container backend protocol for runtime-agnostic container management.

Defines the ContainerBackend protocol that both DockerClient and
KubernetesClient implement, enabling testable and swappable container runtimes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from models import ContainerInfo


@runtime_checkable
class ContainerBackend(Protocol):
    """Protocol for container runtime backends.

    Both DockerClient and KubernetesClient implement this protocol,
    allowing the orchestrator to be runtime-agnostic.
    """

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
    ) -> ContainerInfo: ...

    def start_container(self, container_id: str) -> ContainerInfo: ...

    def stop_container(self, container_id: str, timeout: int = 10) -> ContainerInfo: ...

    def remove_container(self, container_id: str, force: bool = False, v: bool = True) -> None: ...

    def get_container_info(self, container_id: str) -> ContainerInfo: ...

    def list_containers(
        self, all: bool = True, labels: dict[str, str] | None = None
    ) -> list[ContainerInfo]: ...

    def get_container_logs(
        self, container_id: str, tail: int = 100, since: datetime | None = None
    ) -> str: ...

    def wait_for_container(self, container_id: str, timeout: int = 300) -> ContainerInfo: ...

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int: ...

    def is_connected(self) -> bool: ...
