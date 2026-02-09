"""Shared orchestration logic for egg deployment.

This module provides common orchestration utilities used by both local CLI
and GitHub Actions workflows. It centralizes network creation, gateway startup,
health checking, and cleanup logic to ensure consistent behavior across
deployment modes.

The orchestration flow:
1. Create Docker networks (isolated + external for dual-homed gateway)
2. Start gateway container with proper network attachments
3. Wait for gateway health check
4. Return ready state for sandbox execution

Usage:
    from egg_lib.orchestration import EggOrchestrator

    orch = EggOrchestrator()
    if orch.start():
        # Gateway is ready, proceed with sandbox
        pass
    orch.cleanup()
"""

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from .context import get_context
from .docker import ensure_gateway_networks
from .gateway import cleanup_gateway as gateway_cleanup
from .gateway import start_gateway_container
from .output import info, success, warn


@dataclass
class OrchestrationResult:
    """Result of an orchestration operation."""

    success: bool
    gateway_ip: str | None = None
    gateway_port: int = 9848
    error_message: str | None = None


class EggOrchestrator:
    """Orchestrates the egg gateway stack.

    This class provides a unified interface for managing the gateway lifecycle
    across different deployment modes (local, GHA, compose).

    Attributes:
        started: Whether the orchestrator has successfully started the stack
        ephemeral: Whether to clean up resources on exit (for CI/GHA)
    """

    def __init__(self, ephemeral: bool = False):
        """Initialize the orchestrator.

        Args:
            ephemeral: If True, clean up all resources on exit (for CI)
        """
        self.started = False
        self.ephemeral = ephemeral
        self._gateway_started = False
        self._networks_created = False

    def start(
        self,
        on_network_create: Callable[[], None] | None = None,
        on_gateway_start: Callable[[], None] | None = None,
        health_timeout: int = 60,
    ) -> OrchestrationResult:
        """Start the egg gateway stack.

        This creates networks and starts the gateway container, waiting
        for the health check to pass.

        Args:
            on_network_create: Optional callback after networks are created
            on_gateway_start: Optional callback after gateway starts
            health_timeout: Seconds to wait for gateway health

        Returns:
            OrchestrationResult with success status and gateway info
        """
        ctx = get_context()

        # Step 1: Create networks
        info("Creating Docker networks...")
        if not ensure_gateway_networks():
            return OrchestrationResult(
                success=False, error_message="Failed to create gateway networks"
            )
        self._networks_created = True

        if on_network_create:
            on_network_create()

        # Step 2: Start gateway container
        info("Starting gateway container...")
        if not start_gateway_container():
            return OrchestrationResult(
                success=False, error_message="Failed to start gateway container"
            )
        self._gateway_started = True

        if on_gateway_start:
            on_gateway_start()

        # Step 3: Wait for health check
        info("Waiting for gateway health check...")
        if not self._wait_for_health(health_timeout):
            return OrchestrationResult(
                success=False,
                error_message=f"Gateway health check timed out after {health_timeout}s",
            )

        self.started = True
        success("Gateway is ready")

        return OrchestrationResult(
            success=True,
            gateway_ip=ctx.gateway_isolated_ip,
            gateway_port=ctx.gateway_port,
        )

    def _wait_for_health(self, timeout: int) -> bool:
        """Wait for gateway health check to pass.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if healthy, False on timeout
        """
        ctx = get_context()

        # Determine health check URL based on context
        if ctx.publish_ports:
            # Local mode: gateway ports are published
            health_url = f"http://localhost:{ctx.gateway_port}/api/v1/health"
        else:
            # GHA mode: use container inspection to get IP
            try:
                result = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        ctx.gateway_container_name,
                        "--format",
                        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                gateway_ip = result.stdout.strip().split()[0] if result.stdout.strip() else None
                if gateway_ip:
                    health_url = f"http://{gateway_ip}:{ctx.gateway_port}/api/v1/health"
                else:
                    # Fallback to container name (requires docker network access)
                    health_url = (
                        f"http://{ctx.gateway_container_name}:{ctx.gateway_port}/api/v1/health"
                    )
            except Exception:
                health_url = f"http://localhost:{ctx.gateway_port}/api/v1/health"

        elapsed = 0
        while elapsed < timeout:
            try:
                result = subprocess.run(
                    ["curl", "-sf", "--max-time", "5", health_url],
                    capture_output=True,
                    text=True,
                    timeout=10,  # subprocess timeout slightly higher than curl's
                    check=False,
                )
                if result.returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass

            time.sleep(2)
            elapsed += 2

            if elapsed % 10 == 0:
                info(f"Still waiting for gateway... ({elapsed}/{timeout}s)")

        return False

    def cleanup(self) -> None:
        """Clean up gateway resources.

        In ephemeral mode, this removes the gateway container and networks.
        In persistent mode, this is a no-op (gateway stays running).
        """
        if not self.ephemeral:
            return

        info("Cleaning up ephemeral resources...")

        try:
            gateway_cleanup()
        except Exception as e:
            warn(f"Gateway cleanup warning: {e}")

    def is_gateway_running(self) -> bool:
        """Check if gateway container is running.

        Returns:
            True if gateway is running, False otherwise
        """
        ctx = get_context()

        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", ctx.gateway_container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip() == "true"
        except Exception:
            return False

    def get_gateway_logs(self, lines: int = 50) -> str:
        """Get recent gateway container logs.

        Args:
            lines: Number of log lines to retrieve

        Returns:
            Log output as string
        """
        ctx = get_context()

        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(lines), ctx.gateway_container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout + result.stderr
        except Exception as e:
            return f"Failed to get logs: {e}"


def quick_start(ephemeral: bool = False, health_timeout: int = 60) -> OrchestrationResult:
    """Quick start helper for common orchestration pattern.

    This is a convenience function that creates an orchestrator, starts
    the stack, and returns the result. The orchestrator is stored in a
    module-level variable for later cleanup.

    Args:
        ephemeral: Whether to clean up on exit
        health_timeout: Seconds to wait for health check

    Returns:
        OrchestrationResult
    """
    global _active_orchestrator
    _active_orchestrator = EggOrchestrator(ephemeral=ephemeral)
    return _active_orchestrator.start(health_timeout=health_timeout)


def quick_cleanup() -> None:
    """Clean up the quick-started orchestrator."""
    global _active_orchestrator
    if _active_orchestrator:
        _active_orchestrator.cleanup()
        _active_orchestrator = None


# Module-level orchestrator for quick_start/quick_cleanup
_active_orchestrator: EggOrchestrator | None = None
