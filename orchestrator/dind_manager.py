"""
Docker-in-Docker (DinD) sidecar lifecycle manager.

Manages a rootless DinD container alongside a tester sandbox so the tester
can run full-stack integration tests without direct Docker socket access.
The orchestrator (which has Docker socket access) drives this module.
The sandbox never gets Docker socket access — it only talks to the DinD
daemon via ``DOCKER_HOST=tcp://<dind-ip>:2375``.

Architecture follows the same trust model as ``DevserverManager``:
the orchestrator provisions infrastructure, the sandbox only consumes it.
"""

import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    import docker
    import docker.errors

    DockerNotFound = docker.errors.NotFound
except ImportError:
    docker = None  # type: ignore[assignment]

    class DockerNotFound(Exception):  # type: ignore[no-redef]
        """Stub for docker.errors.NotFound when docker SDK is not installed."""

# Add shared directory to path for imports
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.dind")

# DinD configuration defaults
DIND_IMAGE = "docker:27-dind-rootless"
DIND_PORT = 2375
DIND_CPU_LIMIT = "2.0"
DIND_MEMORY_LIMIT = "2g"
DIND_STARTUP_TIMEOUT_SECONDS = 60
DIND_HEALTH_POLL_INTERVAL = 2


class DindError(Exception):
    """Base exception for DinD lifecycle errors."""


class DindStartupError(DindError):
    """DinD daemon failed to start or become healthy."""


class DindImageLoadError(DindError):
    """Failed to pre-load images into the DinD daemon."""


class DindCleanupError(DindError):
    """Failed to clean up DinD resources."""


class DindStatusValue(StrEnum):
    """Status values for the DinD sidecar."""

    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class DindStatus:
    """Status of the DinD sidecar."""

    status: DindStatusValue = DindStatusValue.STOPPED
    container_id: str = ""
    daemon_url: str = ""
    error_message: str = ""
    preloaded_images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for API responses."""
        result: dict[str, Any] = {
            "status": self.status.value,
            "container_id": self.container_id,
            "daemon_url": self.daemon_url,
            "error_message": self.error_message,
        }
        if self.preloaded_images:
            result["preloaded_images"] = self.preloaded_images
        return result


class DindManager:
    """Manages a Docker-in-Docker sidecar for integration testing.

    The orchestrator creates one DindManager per tester sandbox. It handles:
    - Starting a ``docker:27-dind-rootless`` container with ``--privileged``
    - Waiting for the DinD daemon to become healthy via TCP probe
    - Pre-loading Docker images into the DinD daemon via ``docker save | load``
    - Returning the ``DOCKER_HOST`` URL for the tester to use
    - Tearing down the DinD container when the tester completes
    """

    def __init__(
        self,
        pipeline_id: str,
        docker_client: Any | None = None,
    ) -> None:
        """Initialize the DinD manager.

        Args:
            pipeline_id: Pipeline identifier (e.g. 'issue-647').
            docker_client: Optional Docker client instance.
        """
        self.pipeline_id = pipeline_id
        self.docker_client = docker_client or (docker.from_env() if docker else None)

        self._container_name = f"egg-dind-{pipeline_id}"
        self._container_id: str = ""
        self._status = DindStatus()
        self._started = False

    @property
    def status(self) -> DindStatus:
        """Current DinD status."""
        return self._status

    @property
    def container_name(self) -> str:
        """Docker container name for the DinD sidecar."""
        return self._container_name

    @property
    def daemon_url(self) -> str:
        """TCP URL for the DinD daemon, or empty if not started."""
        return self._status.daemon_url

    def _get_container_ip(self) -> str:
        """Get the IP address of the DinD container.

        Returns:
            IP address string.

        Raises:
            DindStartupError: If IP cannot be determined.
        """
        try:
            container = self.docker_client.containers.get(self._container_id)
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            # Use the first available network
            for net_info in networks.values():
                ip = net_info.get("IPAddress", "")
                if ip:
                    return ip
            # Fallback: try the global IP
            ip = container.attrs.get("NetworkSettings", {}).get("IPAddress", "")
            if ip:
                return ip
            raise DindStartupError(
                f"DinD container {self._container_id[:12]} has no IP address"
            )
        except DindStartupError:
            raise
        except Exception as e:
            raise DindStartupError(
                f"Failed to get DinD container IP: {e}"
            ) from e

    def _wait_for_healthy(self, timeout_seconds: int = DIND_STARTUP_TIMEOUT_SECONDS) -> bool:
        """Wait for the DinD daemon to accept TCP connections.

        Polls the daemon port until a TCP connection succeeds or timeout.

        Args:
            timeout_seconds: Maximum seconds to wait.

        Returns:
            True if daemon is healthy, False if timeout.
        """
        ip = self._get_container_ip()
        start = time.monotonic()

        while time.monotonic() - start < timeout_seconds:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((ip, DIND_PORT))
                sock.close()
                logger.info(
                    "DinD daemon healthy",
                    container_id=self._container_id[:12],
                    daemon_url=f"tcp://{ip}:{DIND_PORT}",
                )
                return True
            except OSError:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

            time.sleep(DIND_HEALTH_POLL_INTERVAL)

        return False

    def start(self, network_name: str | None = None) -> DindStatus:
        """Start the DinD sidecar container.

        Args:
            network_name: Optional Docker network to attach the DinD container to.

        Returns:
            DindStatus reflecting the sidecar state.

        Raises:
            DindStartupError: If startup fails.
        """
        if self._started:
            return self._status

        if docker is None:
            raise DindError(
                "docker SDK (pip install docker) is required for DinD support"
            )

        self._status = DindStatus(status=DindStatusValue.STARTING)

        try:
            client = self.docker_client

            # Remove existing container with same name (cleanup from failed runs)
            try:
                existing = client.containers.get(self._container_name)
                logger.warning(
                    "Removing stale DinD container",
                    container_name=self._container_name,
                    pipeline_id=self.pipeline_id,
                )
                existing.remove(force=True)
            except Exception:
                pass  # Container not found or removal failed — proceed with startup

            # Start DinD container
            logger.info(
                "Starting DinD sidecar",
                container_name=self._container_name,
                image=DIND_IMAGE,
                pipeline_id=self.pipeline_id,
            )

            container_kwargs: dict[str, Any] = {
                "image": DIND_IMAGE,
                "name": self._container_name,
                "detach": True,
                "privileged": True,
                "environment": {
                    "DOCKER_TLS_CERTDIR": "",  # Disable TLS for local TCP
                },
                "labels": {
                    "egg.dind": "true",
                    "egg.pipeline.id": self.pipeline_id,
                },
                "cpu_quota": int(float(DIND_CPU_LIMIT) * 100000),
                "cpu_period": 100000,
                "mem_limit": DIND_MEMORY_LIMIT,
            }

            if network_name:
                container_kwargs["network"] = network_name

            container = client.containers.run(**container_kwargs)
            self._container_id = container.id
            self._status.container_id = container.id
            self._started = True

            logger.info(
                "DinD container started",
                container_id=container.id[:12],
                pipeline_id=self.pipeline_id,
            )

            # Wait for daemon health
            if not self._wait_for_healthy():
                self._status.status = DindStatusValue.UNHEALTHY
                self._status.error_message = (
                    f"DinD daemon did not become healthy within {DIND_STARTUP_TIMEOUT_SECONDS}s"
                )
                logger.error(
                    "DinD health check timeout",
                    container_id=container.id[:12],
                    pipeline_id=self.pipeline_id,
                )
                return self._status

            # Set daemon URL
            ip = self._get_container_ip()
            self._status.daemon_url = f"tcp://{ip}:{DIND_PORT}"
            self._status.status = DindStatusValue.HEALTHY

            logger.info(
                "DinD sidecar ready",
                container_id=container.id[:12],
                daemon_url=self._status.daemon_url,
                pipeline_id=self.pipeline_id,
            )

            return self._status

        except DindError:
            self._status.status = DindStatusValue.ERROR
            raise
        except Exception as e:
            self._status.status = DindStatusValue.ERROR
            self._status.error_message = str(e)
            raise DindStartupError(f"Failed to start DinD sidecar: {e}") from e

    def preload_images(self, image_names: list[str]) -> list[str]:
        """Pre-load Docker images into the DinD daemon.

        Uses ``docker save`` on the host and ``docker load`` in the DinD
        container to transfer images without needing registry access.

        Args:
            image_names: List of image names to pre-load.

        Returns:
            List of successfully pre-loaded image names.

        Raises:
            DindImageLoadError: If image pre-loading fails completely.
        """
        if not self._started or not self._status.daemon_url:
            raise DindError("DinD sidecar is not running")

        loaded: list[str] = []

        for image_name in image_names:
            try:
                logger.info(
                    "Pre-loading image into DinD",
                    image=image_name,
                    pipeline_id=self.pipeline_id,
                )

                # docker save <image> | docker -H <dind-url> load
                save_proc = subprocess.Popen(
                    ["docker", "save", image_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                load_proc = subprocess.run(
                    ["docker", "-H", self._status.daemon_url, "load"],
                    stdin=save_proc.stdout,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

                save_proc.wait(timeout=300)

                if save_proc.returncode != 0:
                    stderr = save_proc.stderr.read().decode() if save_proc.stderr else ""
                    logger.warning(
                        "docker save failed",
                        image=image_name,
                        error=stderr,
                    )
                    continue

                if load_proc.returncode != 0:
                    logger.warning(
                        "docker load failed",
                        image=image_name,
                        error=load_proc.stderr,
                    )
                    continue

                loaded.append(image_name)
                logger.info(
                    "Image pre-loaded into DinD",
                    image=image_name,
                    output=load_proc.stdout.strip(),
                )

            except subprocess.TimeoutExpired:
                logger.warning(
                    "Image pre-load timed out",
                    image=image_name,
                    pipeline_id=self.pipeline_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to pre-load image",
                    image=image_name,
                    error=str(e),
                )

        self._status.preloaded_images = loaded

        if not loaded and image_names:
            raise DindImageLoadError(
                f"Failed to pre-load any images into DinD: {image_names}"
            )

        return loaded

    def build_preload_command(self, image_name: str) -> tuple[list[str], list[str]]:
        """Build the shell commands for pre-loading a single image.

        Useful for testing command construction without executing.

        Args:
            image_name: Image to pre-load.

        Returns:
            Tuple of (save_command, load_command).
        """
        save_cmd = ["docker", "save", image_name]
        load_cmd = ["docker", "-H", self._status.daemon_url, "load"]
        return save_cmd, load_cmd

    def teardown(self) -> None:
        """Tear down the DinD sidecar and clean up resources.

        Removes the DinD container. Idempotent — calling twice does not error.
        """
        logger.info(
            "Tearing down DinD sidecar",
            pipeline_id=self.pipeline_id,
            started=self._started,
        )

        if self._container_id:
            try:
                container = self.docker_client.containers.get(self._container_id)
                container.remove(force=True)
                logger.info(
                    "DinD container removed",
                    container_id=self._container_id[:12],
                    pipeline_id=self.pipeline_id,
                )
            except Exception as e:
                # Container not found (already removed) or removal failed.
                # Either way, teardown should not raise.
                if "NotFound" not in type(e).__name__ and "not found" not in str(e).lower():
                    logger.warning(
                        "Failed to remove DinD container",
                        container_id=self._container_id[:12],
                        error=str(e),
                    )

        self._container_id = ""
        self._started = False
        self._status = DindStatus(status=DindStatusValue.STOPPED)

        logger.info(
            "DinD teardown complete",
            pipeline_id=self.pipeline_id,
        )
