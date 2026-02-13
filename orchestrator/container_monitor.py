"""
Container health monitoring and cleanup.

Monitors sandbox container health and removes orphaned containers.
Detects unhealthy/exited containers and triggers appropriate actions.
"""

import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

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


from docker_client import DockerClient, ContainerNotFoundError, get_docker_client
from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.monitor")


class ContainerEvent:
    """Event representing a container state change."""

    STARTED = "started"
    STOPPED = "stopped"
    EXITED = "exited"
    FAILED = "failed"
    REMOVED = "removed"
    UNHEALTHY = "unhealthy"

    def __init__(
        self,
        event_type: str,
        container_info: ContainerInfo,
        timestamp: datetime | None = None,
        data: dict[str, Any] | None = None,
    ):
        self.event_type = event_type
        self.container_info = container_info
        self.timestamp = timestamp or datetime.utcnow()
        self.data = data or {}


EventHandler = Callable[[ContainerEvent], None]


class ContainerMonitor:
    """Monitors container health and lifecycle.

    Periodically checks container status and invokes handlers
    for state changes. Automatically cleans up orphaned containers.
    """

    def __init__(
        self,
        docker_client: DockerClient | None = None,
        check_interval: int = 10,
        orphan_age_hours: int = 24,
    ):
        """Initialize monitor.

        Args:
            docker_client: Docker client (default: singleton)
            check_interval: Seconds between health checks
            orphan_age_hours: Hours before container is considered orphaned
        """
        self.docker_client = docker_client or get_docker_client()
        self.check_interval = check_interval
        self.orphan_age_hours = orphan_age_hours

        self._handlers: list[EventHandler] = []
        self._container_states: dict[str, ContainerStatus] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def add_handler(self, handler: EventHandler) -> None:
        """Add an event handler.

        Args:
            handler: Function to call on container events
        """
        with self._lock:
            self._handlers.append(handler)

    def remove_handler(self, handler: EventHandler) -> None:
        """Remove an event handler.

        Args:
            handler: Handler to remove
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _emit_event(self, event: ContainerEvent) -> None:
        """Emit an event to all handlers.

        Args:
            event: Event to emit
        """
        with self._lock:
            handlers = self._handlers.copy()

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(
                    "Event handler error",
                    event_type=event.event_type,
                    container_id=event.container_info.container_id[:12],
                    error=str(e),
                )

    def _check_container(self, container: ContainerInfo) -> None:
        """Check a single container and emit events for changes.

        Args:
            container: Container to check
        """
        container_id = container.container_id
        old_status = self._container_states.get(container_id)
        new_status = container.status

        if old_status != new_status:
            self._container_states[container_id] = new_status

            # Emit appropriate event
            if new_status == ContainerStatus.RUNNING:
                if old_status is None:
                    self._emit_event(ContainerEvent(ContainerEvent.STARTED, container))
            elif new_status == ContainerStatus.EXITED:
                if container.exit_code == 0:
                    self._emit_event(ContainerEvent(ContainerEvent.STOPPED, container))
                else:
                    self._emit_event(
                        ContainerEvent(
                            ContainerEvent.FAILED,
                            container,
                            data={"exit_code": container.exit_code},
                        )
                    )
            elif new_status == ContainerStatus.FAILED:
                self._emit_event(ContainerEvent(ContainerEvent.FAILED, container))

    def _check_all_containers(self) -> None:
        """Check all orchestrator containers."""
        try:
            containers = self.docker_client.list_containers(all=True)
            current_ids = set()

            for container in containers:
                current_ids.add(container.container_id)
                self._check_container(container)

            # Check for removed containers
            removed_ids = set(self._container_states.keys()) - current_ids
            for container_id in removed_ids:
                del self._container_states[container_id]
                # Can't emit event without ContainerInfo - just log
                logger.info("Container removed", container_id=container_id[:12])

        except Exception as e:
            logger.error("Container check failed", error=str(e))

    def _cleanup_orphaned(self) -> int:
        """Remove orphaned containers.

        Returns:
            Number of containers removed
        """
        try:
            return self.docker_client.cleanup_orphaned_containers(
                max_age_hours=self.orphan_age_hours
            )
        except Exception as e:
            logger.error("Orphan cleanup failed", error=str(e))
            return 0

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        cleanup_counter = 0
        cleanup_interval = 60  # Check for orphans every 60 iterations

        while self._running:
            self._check_all_containers()

            cleanup_counter += 1
            if cleanup_counter >= cleanup_interval:
                self._cleanup_orphaned()
                cleanup_counter = 0

            time.sleep(self.check_interval)

    def start(self) -> None:
        """Start the monitor in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

        logger.info(
            "Container monitor started",
            check_interval=self.check_interval,
        )

    def stop(self) -> None:
        """Stop the monitor."""
        if not self._running:
            return

        self._running = False
        if self._thread:
            self._thread.join(timeout=self.check_interval + 1)
            self._thread = None

        logger.info("Container monitor stopped")

    def is_running(self) -> bool:
        """Check if monitor is running.

        Returns:
            True if monitor is active
        """
        return self._running

    def get_container_status(self, container_id: str) -> ContainerStatus | None:
        """Get cached container status.

        Args:
            container_id: Container ID

        Returns:
            Cached status or None if not tracked
        """
        return self._container_states.get(container_id)

    def check_container_health(self, container_id: str) -> dict[str, Any]:
        """Check health of a specific container.

        Args:
            container_id: Container ID

        Returns:
            Health status dictionary
        """
        try:
            info = self.docker_client.get_container_info(container_id)
            return {
                "healthy": info.status == ContainerStatus.RUNNING,
                "status": info.status.value,
                "exit_code": info.exit_code,
                "started_at": info.started_at.isoformat() if info.started_at else None,
                "exited_at": info.exited_at.isoformat() if info.exited_at else None,
            }
        except ContainerNotFoundError:
            return {
                "healthy": False,
                "status": "not_found",
                "error": "Container not found",
            }


# Singleton monitor instance
_container_monitor: ContainerMonitor | None = None


def get_container_monitor() -> ContainerMonitor:
    """Get the singleton container monitor.

    Returns:
        ContainerMonitor instance
    """
    global _container_monitor
    if _container_monitor is None:
        _container_monitor = ContainerMonitor()
    return _container_monitor
