"""Lifecycle management for egg-launcher.

This module handles the full lifecycle of the egg stack:
- Network creation
- Gateway container startup
- Sandbox container startup
- Health checking
- Cleanup on exit
"""

import os
import secrets
import subprocess
import time
from dataclasses import dataclass


@dataclass
class NetworkConfig:
    """Configuration for egg networks."""

    isolated_name: str = "egg-isolated"
    external_name: str = "egg-external"
    isolated_subnet: str = "172.32.0.0/24"
    external_subnet: str = "172.33.0.0/24"
    gateway_isolated_ip: str = "172.32.0.2"
    gateway_external_ip: str = "172.33.0.2"


@dataclass
class ContainerConfig:
    """Configuration for egg containers."""

    gateway_name: str = "egg-gateway"
    sandbox_prefix: str = "egg-sandbox"
    gateway_port: int = 9848
    proxy_port: int = 3129


class EggLifecycleManager:
    """Manages the lifecycle of the egg stack.

    This class handles creating networks, starting containers, health checking,
    and cleanup. It maintains state about what resources have been created
    so they can be properly cleaned up on exit.
    """

    def __init__(
        self,
        gateway_image: str,
        sandbox_image: str,
        config_dir: str,
        repos_dir: str,
        mode: str = "public",
        network_config: NetworkConfig | None = None,
        container_config: ContainerConfig | None = None,
    ):
        """Initialize the lifecycle manager.

        Args:
            gateway_image: Docker image for gateway container
            sandbox_image: Docker image for sandbox container
            config_dir: Path to configuration directory (mounted from host)
            repos_dir: Path to repositories directory (mounted from host)
            mode: Network mode ("public" or "private")
            network_config: Optional network configuration
            container_config: Optional container configuration
        """
        self.gateway_image = gateway_image
        self.sandbox_image = sandbox_image
        self.config_dir = config_dir
        self.repos_dir = repos_dir
        self.mode = mode
        self.network = network_config or NetworkConfig()
        self.container = container_config or ContainerConfig()

        # State tracking
        self._networks_created: list[str] = []
        self._containers_created: list[str] = []
        self._launcher_secret: str | None = None
        self._gateway_started = False

    def start(self) -> bool:
        """Start the egg stack.

        This creates networks, starts the gateway, and waits for health.

        Returns:
            True if successful, False otherwise
        """
        # Step 1: Create networks
        print("Creating Docker networks...")
        if not self._create_networks():
            return False

        # Step 2: Generate launcher secret if not provided
        if not self._launcher_secret:
            self._launcher_secret = self._get_or_generate_launcher_secret()

        # Step 3: Start gateway
        print("Starting gateway container...")
        if not self._start_gateway():
            return False

        # Step 4: Wait for health
        print("Waiting for gateway health check...")
        if not self._wait_for_gateway_health(timeout=60):
            print("Gateway health check failed")
            return False

        print("Gateway is healthy")
        self._gateway_started = True
        return True

    def run_interactive(self) -> int:
        """Run sandbox in interactive mode.

        Returns:
            Exit code from sandbox container
        """
        return self._run_sandbox(interactive=True)

    def run_print_mode(self, prompt: str) -> int:
        """Run sandbox in print (non-interactive) mode.

        Args:
            prompt: The prompt to execute

        Returns:
            Exit code from sandbox container
        """
        return self._run_sandbox(interactive=False, prompt=prompt)

    def cleanup(self) -> None:
        """Clean up all created resources.

        This removes containers and networks in reverse order of creation.
        """
        print("Cleaning up egg stack...")

        # Stop and remove containers
        for container_name in reversed(self._containers_created):
            print(f"  Removing container: {container_name}")
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        # Remove networks
        for network_name in reversed(self._networks_created):
            print(f"  Removing network: {network_name}")
            subprocess.run(
                ["docker", "network", "rm", network_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        self._containers_created.clear()
        self._networks_created.clear()
        self._gateway_started = False

    def get_status(self) -> dict:
        """Get current status of the egg stack.

        Returns:
            Dictionary with status information
        """
        gateway_running = self._is_container_running(self.container.gateway_name)
        gateway_healthy = self._check_gateway_health() if gateway_running else False

        return {
            "gateway": {
                "running": gateway_running,
                "healthy": gateway_healthy,
                "container_name": self.container.gateway_name,
                "image": self.gateway_image,
            },
            "networks": {
                "isolated": self.network.isolated_name,
                "external": self.network.external_name,
            },
            "mode": self.mode,
            "containers_created": self._containers_created.copy(),
        }

    def _create_networks(self) -> bool:
        """Create Docker networks for egg.

        Returns:
            True if successful, False otherwise
        """
        # Create isolated network (internal, no external route)
        if not self._network_exists(self.network.isolated_name):
            result = subprocess.run(
                [
                    "docker",
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--internal",
                    "--subnet",
                    self.network.isolated_subnet,
                    self.network.isolated_name,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"Failed to create isolated network: {result.stderr}")
                return False
            self._networks_created.append(self.network.isolated_name)
            print(f"  Created network: {self.network.isolated_name}")

        # Create external network
        if not self._network_exists(self.network.external_name):
            result = subprocess.run(
                [
                    "docker",
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--subnet",
                    self.network.external_subnet,
                    self.network.external_name,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"Failed to create external network: {result.stderr}")
                return False
            self._networks_created.append(self.network.external_name)
            print(f"  Created network: {self.network.external_name}")

        return True

    def _start_gateway(self) -> bool:
        """Start the gateway container.

        Returns:
            True if successful, False otherwise
        """
        # Remove existing gateway if present
        subprocess.run(
            ["docker", "rm", "-f", self.container.gateway_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        # Build gateway command
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.container.gateway_name,
            "--network",
            self.network.isolated_name,
            "--ip",
            self.network.gateway_isolated_ip,
            "-p",
            f"{self.container.gateway_port}:{self.container.gateway_port}",
            "-p",
            f"{self.container.proxy_port}:{self.container.proxy_port}",
            "-e",
            f"EGG_LAUNCHER_SECRET={self._launcher_secret}",
            "-e",
            "EGG_REPO_CONFIG=/config/repositories.yaml",
            "-e",
            f"HOST_UID={os.getuid()}",
            "-e",
            f"HOST_GID={os.getgid()}",
        ]

        # Add config volume mounts
        if os.path.exists(f"{self.config_dir}/repositories.yaml"):
            cmd.extend(["-v", f"{self.config_dir}/repositories.yaml:/config/repositories.yaml:ro"])
        if os.path.exists(self.config_dir):
            cmd.extend(["-v", f"{self.config_dir}:/secrets:ro"])
        if os.path.exists(self.repos_dir):
            cmd.extend(["-v", f"{self.repos_dir}:/home/egg/repos"])

        # Add shared volumes for worktrees, state, and certs
        cmd.extend(
            [
                "-v",
                "egg-worktrees:/home/egg/.egg-worktrees",
                "-v",
                "egg-state:/home/egg/.egg-state",
                "-v",
                "egg-certs:/shared/certs",
            ]
        )

        # Add GitHub token if available
        github_token = os.environ.get("GITHUB_USER_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if github_token:
            cmd.extend(["-e", f"GITHUB_USER_TOKEN={github_token}"])

        # Add image name
        cmd.append(self.gateway_image)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Failed to start gateway: {result.stderr}")
            return False

        self._containers_created.append(self.container.gateway_name)

        # Connect to external network
        result = subprocess.run(
            [
                "docker",
                "network",
                "connect",
                "--ip",
                self.network.gateway_external_ip,
                self.network.external_name,
                self.container.gateway_name,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Failed to connect gateway to external network: {result.stderr}")
            return False

        return True

    def _run_sandbox(
        self, interactive: bool = True, prompt: str | None = None
    ) -> int:
        """Run a sandbox container.

        Args:
            interactive: Whether to run interactively
            prompt: Prompt for print mode (required if not interactive)

        Returns:
            Exit code from the sandbox container
        """
        import datetime

        container_name = f"{self.container.sandbox_prefix}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"

        # Determine network based on mode
        if self.mode == "private":
            network = self.network.isolated_name
            gateway_ip = self.network.gateway_isolated_ip
        else:
            network = self.network.external_name
            gateway_ip = self.network.gateway_external_ip

        # Build command
        cmd = ["docker", "run", "--rm"]

        if interactive:
            cmd.extend(["-it"])

        cmd.extend(
            [
                "--name",
                container_name,
                "--network",
                network,
                "-e",
                f"EGG_GATEWAY_HOST={self.container.gateway_name}",
                "-e",
                f"EGG_GATEWAY_IP={gateway_ip}",
                "-e",
                f"EGG_GATEWAY_PORT={self.container.gateway_port}",
                "-e",
                f"EGG_LAUNCHER_SECRET={self._launcher_secret}",
                "-e",
                f"EGG_MODE={self.mode}",
            ]
        )

        # Add proxy URL for private mode
        if self.mode == "private":
            cmd.extend(
                [
                    "-e",
                    f"HTTP_PROXY=http://{self.container.gateway_name}:{self.container.proxy_port}",
                    "-e",
                    f"HTTPS_PROXY=http://{self.container.gateway_name}:{self.container.proxy_port}",
                ]
            )

        # Mount repositories
        if os.path.exists(self.repos_dir):
            cmd.extend(["-v", f"{self.repos_dir}:/home/egg/repos"])

        # Add shared certs volume
        cmd.extend(["-v", "egg-certs:/shared/certs:ro"])

        # Add image
        cmd.append(self.sandbox_image)

        # Add claude command for print mode
        if not interactive and prompt:
            cmd.extend(
                [
                    "claude",
                    "--dangerously-skip-permissions",
                    "--print",
                    "--verbose",
                    prompt,
                ]
            )

        # Run the container
        self._containers_created.append(container_name)
        result = subprocess.run(cmd)
        return result.returncode

    def _wait_for_gateway_health(self, timeout: int = 60) -> bool:
        """Wait for gateway health check to pass.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if healthy, False on timeout
        """
        elapsed = 0
        while elapsed < timeout:
            if self._check_gateway_health():
                return True
            time.sleep(2)
            elapsed += 2
            if elapsed % 10 == 0:
                print(f"  Still waiting... ({elapsed}/{timeout}s)")
        return False

    def _check_gateway_health(self) -> bool:
        """Check if gateway is healthy.

        Returns:
            True if healthy, False otherwise
        """
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sf",
                    f"http://localhost:{self.container.gateway_port}/api/v1/health",
                ],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _network_exists(self, name: str) -> bool:
        """Check if a Docker network exists.

        Args:
            name: Network name

        Returns:
            True if exists, False otherwise
        """
        result = subprocess.run(
            ["docker", "network", "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def _is_container_running(self, name: str) -> bool:
        """Check if a container is running.

        Args:
            name: Container name

        Returns:
            True if running, False otherwise
        """
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() == "true"

    def _get_or_generate_launcher_secret(self) -> str:
        """Get launcher secret from config or generate a new one.

        Returns:
            Launcher secret string
        """
        # Try to read from config directory
        secret_file = os.path.join(self.config_dir, "launcher-secret")
        if os.path.exists(secret_file):
            with open(secret_file) as f:
                return f.read().strip()

        # Try environment variable
        env_secret = os.environ.get("EGG_LAUNCHER_SECRET")
        if env_secret:
            return env_secret

        # Generate a new secret
        return secrets.token_hex(32)
