"""
Devserver lifecycle manager for deployment validation.

Manages the full lifecycle of a target application's devserver stack
during the check phase: extracting compose config from committed state,
generating override mounts for agent-modified code, creating an air-gapped
network, starting/stopping the stack, and attaching the sandbox checker.

The orchestrator (which has Docker socket access) drives this module.
The sandbox never gets Docker socket access — it only makes HTTP requests
to the running devserver services.
"""

import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

try:
    import docker
except ImportError:
    docker = None  # type: ignore[assignment]

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


from egg_config.constants import (
    DEVSERVER_CPU_LIMIT,
    DEVSERVER_HARD_TIMEOUT_SECONDS,
    DEVSERVER_MEMORY_LIMIT,
    DEVSERVER_PIDS_LIMIT,
    EGG_CHECK_NETWORK_PREFIX,
)
from egg_contracts.deployment import (
    DeploymentConfig,
    ServiceMapping,
    check_suspicious_env_vars,
    load_deployment_config,
)

logger = get_logger("orchestrator.devserver")


class DevserverError(Exception):
    """Base exception for devserver lifecycle errors."""


class ComposeExtractionError(DevserverError):
    """Failed to extract compose config from committed state."""


class NetworkError(DevserverError):
    """Failed to create or manage the check network."""


class StackLifecycleError(DevserverError):
    """Failed to start or stop the devserver stack."""


class DevserverStatusValue(StrEnum):
    """Status values for the devserver stack."""

    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ServiceStatus:
    """Status of an individual devserver service."""

    name: str
    healthy: bool = False
    ip: str = ""
    port: int = 0
    container_id: str = ""


@dataclass
class DevserverStatus:
    """Status of the entire devserver stack."""

    status: DevserverStatusValue = DevserverStatusValue.STOPPED
    services: dict[str, ServiceStatus] = field(default_factory=dict)
    network_id: str = ""
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for API responses."""
        result: dict[str, Any] = {
            "status": self.status.value,
            "services": {
                name: {
                    "name": svc.name,
                    "healthy": svc.healthy,
                    "ip": svc.ip,
                    "port": svc.port,
                    "container_id": svc.container_id,
                }
                for name, svc in self.services.items()
            },
            "network_id": self.network_id,
            "error_message": self.error_message,
        }
        if self.warnings:
            result["warnings"] = self.warnings
        return result


class DevserverManager:
    """Manages the devserver stack lifecycle for deployment validation.

    The orchestrator creates one DevserverManager per pipeline. It handles:
    - Extracting compose config from committed state (HEAD)
    - Generating a compose override with read-only agent code mounts
    - Creating an air-gapped egg-check network
    - Starting and stopping the docker-compose stack
    - Attaching the sandbox (checker) to the network
    """

    def __init__(
        self,
        pipeline_id: str,
        repo_path: Path,
        worktree_path: Path,
        docker_client: Any | None = None,
    ) -> None:
        """Initialize the devserver manager.

        Args:
            pipeline_id: Pipeline identifier (e.g. 'issue-645').
            repo_path: Path to the main repository.
            worktree_path: Path to the pipeline's worktree (where agent code lives).
            docker_client: Optional DockerClient instance (for container operations).
        """
        self.pipeline_id = pipeline_id
        self.repo_path = repo_path
        self.worktree_path = worktree_path
        self.docker_client = docker_client or (docker.from_env() if docker else None)

        self._network_name = f"{EGG_CHECK_NETWORK_PREFIX}-{pipeline_id}"
        self._network_id: str = ""
        self._temp_dir: Path | None = None
        self._status = DevserverStatus()
        self._started = False
        self._attached_containers: list[str] = []
        self._scoped_networks: dict[str, str] = {}  # service_name -> network_id

    @property
    def network_name(self) -> str:
        """The Docker network name for this pipeline's devserver."""
        return self._network_name

    @property
    def status(self) -> DevserverStatus:
        """Current devserver status."""
        return self._status

    def _extract_compose_config(self, compose_path: str) -> str:
        """Extract compose file content from committed state (HEAD).

        Uses `git show HEAD:<path>` against the worktree to ensure we read
        the committed version, not any working-tree modifications the agent
        may have made.

        Args:
            compose_path: Path to the compose file relative to repo root.

        Returns:
            Compose file content as a string.

        Raises:
            ComposeExtractionError: If extraction fails.
        """
        try:
            result = subprocess.run(
                ["git", "show", f"HEAD:{compose_path}"],
                cwd=str(self.worktree_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise ComposeExtractionError(
                    f"Failed to extract {compose_path} from HEAD: {result.stderr.strip()}"
                )

            content = result.stdout
            if not content.strip():
                raise ComposeExtractionError(f"Compose file {compose_path} at HEAD is empty")

            # Validate it's valid YAML
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                raise ComposeExtractionError(
                    f"Compose file {compose_path} at HEAD is not valid YAML: {e}"
                ) from e

            return content

        except subprocess.TimeoutExpired as e:
            raise ComposeExtractionError(f"Timed out extracting {compose_path} from HEAD") from e
        except FileNotFoundError as e:
            raise ComposeExtractionError("git not found — cannot extract compose config") from e

    def _resolve_affected_services(
        self,
        changed_files: list[str],
        service_mappings: list[ServiceMapping],
    ) -> list[ServiceMapping]:
        """Determine which services are affected by the agent's changes.

        Args:
            changed_files: List of changed file paths relative to repo root.
            service_mappings: Service-to-source mappings from DeploymentConfig.

        Returns:
            Subset of service_mappings for services affected by the changes.
        """
        affected = []
        for mapping in service_mappings:
            source_dir = mapping.source_dir.rstrip("/") + "/"
            for changed_file in changed_files:
                if changed_file.startswith(source_dir) or changed_file == mapping.source_dir.rstrip(
                    "/"
                ):
                    affected.append(mapping)
                    break
        return affected

    def _generate_compose_override(
        self,
        affected_services: list[ServiceMapping],
        worktree_path: Path,
        all_service_names: list[str],
    ) -> str:
        """Generate a docker-compose override YAML.

        Adds read-only volume mounts for agent code, resource limits,
        security options, and the egg-check network to all services.

        Args:
            affected_services: Services that need agent code mounted.
            worktree_path: Path to the worktree with agent's code.
            all_service_names: All service names from the base compose file.

        Returns:
            Docker compose override YAML string.
        """
        services: dict[str, Any] = {}

        for service_name in all_service_names:
            service_config: dict[str, Any] = {
                "networks": [self._network_name],
                "deploy": {
                    "resources": {
                        "limits": {
                            "cpus": DEVSERVER_CPU_LIMIT,
                            "memory": DEVSERVER_MEMORY_LIMIT,
                            "pids": DEVSERVER_PIDS_LIMIT,
                        },
                    },
                },
                "security_opt": [
                    "no-new-privileges:true",
                    # Docker applies its default seccomp profile automatically
                    # when no seccomp option is specified — no override needed.
                ],
                "cap_drop": ["ALL"],
                "read_only": False,
                "privileged": False,
            }

            # Add read-only volume mounts for affected services
            for mapping in affected_services:
                if mapping.service_name == service_name:
                    host_path = str(worktree_path / mapping.source_dir)
                    container_path = mapping.container_mount_path
                    service_config.setdefault("volumes", [])
                    service_config["volumes"].append(f"{host_path}:{container_path}:ro")

            services[service_name] = service_config

        override = {
            "services": services,
            "networks": {
                self._network_name: {
                    "external": True,
                },
            },
        }

        return yaml.dump(override, default_flow_style=False, sort_keys=False)

    def _create_check_network(self) -> str:
        """Create the air-gapped egg-check Docker network.

        Creates a bridge network with `internal=True` (no default gateway,
        no DNS, no route to internet).

        Returns:
            Network ID.

        Raises:
            NetworkError: If network creation fails.
        """
        try:
            client = self.docker_client
            # Remove existing network with same name (cleanup from failed runs)
            try:
                existing = client.networks.get(self._network_name)
                logger.warning(
                    "Removing stale check network",
                    network=self._network_name,
                    pipeline_id=self.pipeline_id,
                )
                existing.remove()
            except docker.errors.NotFound:
                pass

            network = client.networks.create(
                name=self._network_name,
                driver="bridge",
                internal=True,  # No default gateway — air-gapped
                labels={
                    "egg.check-network": "true",
                    "egg.pipeline-id": self.pipeline_id,
                },
                # Let Docker auto-assign subnets to avoid collisions when
                # multiple pipelines run deployment checks concurrently.
            )

            logger.info(
                "Created check network",
                network_name=self._network_name,
                network_id=network.id[:12],
                pipeline_id=self.pipeline_id,
            )

            return network.id

        except Exception as e:
            raise NetworkError(f"Failed to create check network '{self._network_name}': {e}") from e

    def _create_scoped_network(self, service_name: str) -> str:
        """Create a per-service scoped network for inter-container isolation.

        Each service gets its own internal bridge so the checker can only
        reach services under test, not database emulators or caches directly.

        Args:
            service_name: Name of the service to scope.

        Returns:
            Network ID.
        """
        try:
            client = self.docker_client
            network_name = f"{self._network_name}-{service_name}"

            try:
                existing = client.networks.get(network_name)
                existing.remove()
            except docker.errors.NotFound:
                pass

            network = client.networks.create(
                name=network_name,
                driver="bridge",
                internal=True,
                labels={
                    "egg.check-network": "true",
                    "egg.pipeline-id": self.pipeline_id,
                    "egg.service": service_name,
                },
            )

            self._scoped_networks[service_name] = network.id
            return network.id

        except Exception as e:
            logger.warning(
                "Failed to create scoped network",
                service=service_name,
                error=str(e),
            )
            return ""

    def _remove_check_network(self) -> None:
        """Remove the egg-check network and any scoped networks.

        Force-removes even if containers are still attached.
        """
        client = self.docker_client

        # Remove scoped networks first
        for service_name, network_id in self._scoped_networks.items():
            try:
                network = client.networks.get(network_id)
                # Disconnect any containers first
                network.reload()
                for container in network.containers:
                    try:
                        network.disconnect(container, force=True)
                    except Exception:
                        pass
                network.remove()
                logger.info(
                    "Removed scoped network",
                    service=service_name,
                    network_id=network_id[:12],
                )
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.warning(
                    "Failed to remove scoped network",
                    service=service_name,
                    error=str(e),
                )

        self._scoped_networks.clear()

        # Remove main check network
        if not self._network_id:
            return

        try:
            network = client.networks.get(self._network_id)
            # Disconnect any containers first
            network.reload()
            for container in network.containers:
                try:
                    network.disconnect(container, force=True)
                except Exception:
                    pass
            network.remove()
            logger.info(
                "Removed check network",
                network_name=self._network_name,
                network_id=self._network_id[:12],
            )
        except docker.errors.NotFound:
            pass
        except Exception as e:
            logger.warning(
                "Failed to remove check network",
                network_name=self._network_name,
                error=str(e),
            )
        finally:
            self._network_id = ""

    def _get_changed_files(self) -> list[str]:
        """Get list of files changed by the agent in the worktree.

        Compares worktree HEAD against origin/main to find agent changes.

        Returns:
            List of changed file paths relative to repo root.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "origin/main...HEAD"],
                cwd=str(self.worktree_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                # Fallback: diff against HEAD~1 — this only captures the last
                # commit, not all agent changes if multiple commits were made.
                logger.warning(
                    "origin/main diff failed, falling back to HEAD~1 "
                    "(may return partial changed-file list)",
                    pipeline_id=self.pipeline_id,
                    stderr=result.stderr.strip(),
                )
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                    cwd=str(self.worktree_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        except Exception as e:
            logger.warning("Failed to get changed files", error=str(e))
            return []

    def _get_compose_service_names(self, compose_content: str) -> list[str]:
        """Extract service names from compose file content.

        Args:
            compose_content: YAML content of the compose file.

        Returns:
            List of service names.
        """
        try:
            data = yaml.safe_load(compose_content)
            if isinstance(data, dict) and "services" in data:
                return list(data["services"].keys())
        except yaml.YAMLError:
            pass
        return []

    def _check_suspicious_env_vars_in_compose(self, compose_content: str) -> list[str]:
        """Pre-flight check for suspicious credential env vars in compose.

        Args:
            compose_content: YAML content of the compose file.

        Returns:
            List of warning messages for suspicious env vars.
        """
        warnings: list[str] = []
        try:
            data = yaml.safe_load(compose_content)
            if not isinstance(data, dict) or "services" not in data:
                return warnings
            for svc_name, svc_config in data["services"].items():
                if not isinstance(svc_config, dict):
                    continue
                env = svc_config.get("environment", {})
                if isinstance(env, dict):
                    suspicious = check_suspicious_env_vars(env)
                elif isinstance(env, list):
                    env_dict = {}
                    for item in env:
                        if "=" in str(item):
                            key = str(item).split("=", 1)[0]
                            env_dict[key] = ""
                    suspicious = check_suspicious_env_vars(env_dict)
                else:
                    suspicious = []
                for var_name in suspicious:
                    warnings.append(
                        f"Service '{svc_name}' has suspicious env var '{var_name}' "
                        f"— ensure this uses a local emulator default, not real credentials"
                    )
        except yaml.YAMLError:
            pass
        return warnings

    def _get_container_endpoint(self, service_name: str) -> tuple[str, int]:
        """Get the IP address and exposed port of a service container.

        Looks up the container on the check network and extracts the first
        exposed port from the container's configuration.

        Args:
            service_name: Docker compose service name.

        Returns:
            Tuple of (ip_address, port). IP is empty string and port is 0
            if not found.
        """
        try:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self._temp_dir / "docker-compose.yml"),
                    "-f",
                    str(self._temp_dir / "docker-compose.override.yml"),
                    "--project-name",
                    self._network_name,
                    "ps",
                    "-q",
                    service_name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            container_id = result.stdout.strip()
            if not container_id:
                return ("", 0)

            client = self.docker_client
            container = client.containers.get(container_id)
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            net_info = networks.get(self._network_name, {})
            ip = net_info.get("IPAddress", "")

            # Extract the first exposed port from the container config.
            # ExposedPorts is a dict like {"8080/tcp": {}, "443/tcp": {}}.
            # NOTE: For multi-port containers, this picks the first key in
            # insertion order (CPython 3.7+ dict ordering = Dockerfile EXPOSE
            # order).  If the health endpoint is on a non-first port, this
            # will probe the wrong port.  For single-port containers (the
            # expected case for devserver services) this is unambiguous.  A
            # future enhancement could add an optional port field to
            # DeploymentConfig.health_endpoints to remove the ambiguity.
            port = 0
            exposed = container.attrs.get("Config", {}).get("ExposedPorts", {})
            if exposed:
                first_port_key = next(iter(exposed))  # e.g. "8080/tcp"
                try:
                    port = int(first_port_key.split("/")[0])
                except (ValueError, IndexError):
                    pass

            return (ip, port)
        except Exception:
            logger.debug(
                "Failed to get container endpoint",
                service=service_name,
                exc_info=True,
            )
            return ("", 0)

    def _wait_for_health(
        self,
        deployment_config: DeploymentConfig,
        timeout_seconds: int,
    ) -> bool:
        """Wait for all services with health endpoints to become healthy.

        Makes HTTP requests directly from the orchestrator to the container
        IPs on the check network, avoiding reliance on tools (wget/curl)
        being present inside containers.

        Args:
            deployment_config: Configuration with health endpoint paths.
            timeout_seconds: Maximum seconds to wait.

        Returns:
            True if all services are healthy, False if timeout.
        """
        if not deployment_config.health_endpoints:
            logger.info("No health endpoints configured, skipping health wait")
            return True

        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            all_healthy = True
            for service_name, health_path in deployment_config.health_endpoints.items():
                svc_status = self._status.services.get(service_name)
                if svc_status and svc_status.healthy:
                    continue

                # Get the container IP and exposed port on the check network
                # and probe from the orchestrator side — no dependency on
                # tools inside the container (wget, curl, etc.).
                try:
                    ip, port = self._get_container_endpoint(service_name)
                    if not ip:
                        all_healthy = False
                        continue

                    if port:
                        url = f"http://{ip}:{port}{health_path}"
                    else:
                        url = f"http://{ip}{health_path}"
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status == 200:
                            if svc_status:
                                svc_status.healthy = True
                                svc_status.ip = ip
                                svc_status.port = port
                            logger.info(
                                "Service healthy",
                                service=service_name,
                                health_path=health_path,
                                port=port,
                            )
                        else:
                            all_healthy = False
                except Exception:
                    all_healthy = False

            if all_healthy:
                return True

            time.sleep(2)

        return False

    def pre_pull_images(self, deployment_config: DeploymentConfig) -> None:
        """Pre-pull container images to reduce startup latency.

        Pulls all images referenced in the compose file. Errors are logged
        but do not fail the pre-pull — images may already exist locally.

        Args:
            deployment_config: Configuration with compose file reference.
        """
        try:
            compose_content = self._extract_compose_config(deployment_config.compose_file)
            data = yaml.safe_load(compose_content)
            if not isinstance(data, dict) or "services" not in data:
                return

            client = self.docker_client

            for svc_name, svc_config in data["services"].items():
                if not isinstance(svc_config, dict):
                    continue
                image = svc_config.get("image")
                if not image:
                    continue

                # Prepend registry prefix if configured
                if deployment_config.image_registry and "/" not in image:
                    image = f"{deployment_config.image_registry}/{image}"

                try:
                    logger.info("Pre-pulling image", image=image, service=svc_name)
                    client.images.pull(image)
                except Exception as e:
                    logger.warning(
                        "Failed to pre-pull image",
                        image=image,
                        service=svc_name,
                        error=str(e),
                    )

        except ComposeExtractionError as e:
            logger.warning("Cannot pre-pull: compose extraction failed", error=str(e))

    def start(
        self,
        deployment_config: DeploymentConfig,
        changed_files: list[str] | None = None,
    ) -> DevserverStatus:
        """Start the devserver stack for deployment validation.

        Full lifecycle:
        1. Extract compose config from committed state (HEAD)
        2. Resolve which services are affected by agent changes
        3. Generate compose override with RO mounts and resource limits
        4. Create the air-gapped egg-check network
        5. Run docker compose up
        6. Wait for health checks

        Args:
            deployment_config: Target application's deployment configuration.
            changed_files: Override list of changed files (auto-detected if None).

        Returns:
            DevserverStatus reflecting the stack state.

        Raises:
            StackLifecycleError: If startup fails.
        """
        if self._started:
            return self._status

        if docker is None:
            raise DevserverError(
                "docker SDK (pip install docker) is required for deployment validation"
            )

        self._status = DevserverStatus(status=DevserverStatusValue.STARTING)

        try:
            # Step 1: Extract compose from committed state
            logger.info(
                "Extracting compose config from HEAD",
                compose_file=deployment_config.compose_file,
                pipeline_id=self.pipeline_id,
            )
            compose_content = self._extract_compose_config(deployment_config.compose_file)

            # Pre-flight: check for suspicious credentials
            cred_warnings = self._check_suspicious_env_vars_in_compose(compose_content)
            for warning in cred_warnings:
                logger.warning("Credential check", message=warning)
            self._status.warnings = cred_warnings

            # Step 2: Resolve affected services
            if changed_files is None:
                changed_files = self._get_changed_files()

            affected_services = self._resolve_affected_services(
                changed_files, deployment_config.services
            )
            all_service_names = self._get_compose_service_names(compose_content)

            if not all_service_names:
                raise StackLifecycleError("No services found in compose file")

            logger.info(
                "Resolved affected services",
                affected=[m.service_name for m in affected_services],
                all_services=all_service_names,
                changed_files_count=len(changed_files),
            )

            # Step 3: Generate compose override
            override_content = self._generate_compose_override(
                affected_services, self.worktree_path, all_service_names
            )

            # Step 4: Write compose files to temp directory
            self._temp_dir = Path(tempfile.mkdtemp(prefix=f"egg-devserver-{self.pipeline_id}-"))
            base_compose_path = self._temp_dir / "docker-compose.yml"
            override_path = self._temp_dir / "docker-compose.override.yml"
            base_compose_path.write_text(compose_content, encoding="utf-8")
            override_path.write_text(override_content, encoding="utf-8")

            # Step 5: Create air-gapped network
            self._network_id = self._create_check_network()
            self._status.network_id = self._network_id

            # Create per-service scoped networks for services under test
            for mapping in affected_services:
                self._create_scoped_network(mapping.service_name)

            # Step 6: docker compose up
            logger.info(
                "Starting devserver stack",
                pipeline_id=self.pipeline_id,
                temp_dir=str(self._temp_dir),
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(base_compose_path),
                    "-f",
                    str(override_path),
                    "--project-name",
                    self._network_name,
                    "up",
                    "-d",
                    "--no-build",
                ],
                capture_output=True,
                text=True,
                timeout=DEVSERVER_HARD_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise StackLifecycleError(f"docker compose up failed: {result.stderr.strip()}")

            self._started = True

            # Initialize service status
            for svc_name in all_service_names:
                self._status.services[svc_name] = ServiceStatus(name=svc_name)

            # Step 7: Wait for health checks
            timeout = deployment_config.startup_timeout_seconds
            healthy = self._wait_for_health(deployment_config, timeout)

            if healthy:
                self._status.status = DevserverStatusValue.HEALTHY
                logger.info(
                    "Devserver stack healthy",
                    pipeline_id=self.pipeline_id,
                    services=list(self._status.services.keys()),
                )
            else:
                self._status.status = DevserverStatusValue.UNHEALTHY
                unhealthy = [name for name, svc in self._status.services.items() if not svc.healthy]
                self._status.error_message = (
                    f"Timeout waiting for services to become healthy: {unhealthy}"
                )
                logger.warning(
                    "Devserver health check timeout",
                    pipeline_id=self.pipeline_id,
                    unhealthy_services=unhealthy,
                )

            return self._status

        except DevserverError:
            self._status.status = DevserverStatusValue.ERROR
            raise
        except Exception as e:
            self._status.status = DevserverStatusValue.ERROR
            self._status.error_message = str(e)
            raise StackLifecycleError(f"Failed to start devserver: {e}") from e

    def attach_checker(
        self,
        sandbox_container_id: str,
        service_names: list[str] | None = None,
    ) -> None:
        """Attach the sandbox (checker) container to the egg-check network.

        After attachment, the sandbox can reach devserver services by
        container name on the egg-check network.

        Args:
            sandbox_container_id: Docker container ID of the sandbox.
            service_names: Optional list of specific services the checker
                should reach. Currently attaches to the main egg-check
                network (full access); per-service scoping is available
                via scoped networks.
        """
        try:
            client = self.docker_client
            network = client.networks.get(self._network_id)
            network.connect(sandbox_container_id)
            self._attached_containers.append(sandbox_container_id)

            logger.info(
                "Attached checker to check network",
                container_id=sandbox_container_id[:12],
                network=self._network_name,
            )

        except Exception as e:
            raise NetworkError(
                f"Failed to attach checker {sandbox_container_id[:12]} "
                f"to network '{self._network_name}': {e}"
            ) from e

    def teardown(self) -> None:
        """Tear down the devserver stack and clean up all resources.

        Runs docker compose down, removes the network, and cleans up
        temp files. Idempotent — calling twice does not error.
        """
        logger.info(
            "Tearing down devserver",
            pipeline_id=self.pipeline_id,
            started=self._started,
        )

        # Step 1: docker compose down
        if self._started and self._temp_dir and self._temp_dir.exists():
            try:
                base_compose_path = self._temp_dir / "docker-compose.yml"
                override_path = self._temp_dir / "docker-compose.override.yml"
                if base_compose_path.exists():
                    subprocess.run(
                        [
                            "docker",
                            "compose",
                            "-f",
                            str(base_compose_path),
                            "-f",
                            str(override_path),
                            "--project-name",
                            self._network_name,
                            "down",
                            "--volumes",
                            "--remove-orphans",
                            "--timeout",
                            "10",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
            except Exception as e:
                logger.warning(
                    "Error during docker compose down",
                    error=str(e),
                    pipeline_id=self.pipeline_id,
                )

        self._started = False

        # Step 2: Detach any attached containers and remove networks
        try:
            self._remove_check_network()
        except Exception as e:
            logger.warning(
                "Error removing check network during teardown",
                error=str(e),
            )

        self._attached_containers.clear()

        # Step 3: Clean up temp directory
        if self._temp_dir and self._temp_dir.exists():
            try:
                shutil.rmtree(self._temp_dir)
                logger.info(
                    "Cleaned up temp directory",
                    temp_dir=str(self._temp_dir),
                )
            except Exception as e:
                logger.warning(
                    "Failed to clean up temp directory",
                    temp_dir=str(self._temp_dir),
                    error=str(e),
                )
            self._temp_dir = None

        self._status = DevserverStatus(status=DevserverStatusValue.STOPPED)

        logger.info(
            "Devserver teardown complete",
            pipeline_id=self.pipeline_id,
        )

    def get_deployment_config(self) -> DeploymentConfig | None:
        """Load deployment config from the target repository.

        Convenience method that delegates to load_deployment_config.

        Returns:
            DeploymentConfig if target repo has opted in, None otherwise.
        """
        return load_deployment_config(self.worktree_path)
