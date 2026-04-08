"""
Kubernetes client for container operations.

Provides container lifecycle management using Kubernetes Jobs
as the execution primitive, serving as an alternative backend
to DockerClient for the orchestrator.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from container_backend import (
    ImagePullError,
    JobOperationError,
    KubernetesClientError,
    PodNotFoundError,
)
from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.kubernetes")

DEFAULT_AGENT_NAMESPACE = "egg-agents"
DEFAULT_SYSTEM_NAMESPACE = "egg-system"


class KubernetesClient:
    """Kubernetes client for sandbox container management.

    Wraps the Kubernetes Python client to provide container operations
    using Jobs as the execution primitive. Satisfies the ContainerBackend
    protocol for runtime-agnostic orchestration.
    """

    DEFAULT_SANDBOX_IMAGE = "egg:latest"

    def __init__(self, namespace: str = DEFAULT_AGENT_NAMESPACE) -> None:
        """Initialize Kubernetes client.

        Args:
            namespace: Default namespace for agent Jobs.
        """
        self.namespace = namespace
        self._core_api: Any = None
        self._batch_api: Any = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazily initialize the Kubernetes client.

        Loads config from in-cluster environment if available,
        otherwise falls back to the default kubeconfig.

        Raises:
            KubernetesClientError: If Kubernetes client cannot be initialized.
        """
        if self._initialized:
            return

        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
                logger.info("Loaded in-cluster Kubernetes config")
            except config.ConfigException:
                config.load_kube_config()
                logger.info("Loaded kubeconfig from default location")

            self._core_api = client.CoreV1Api()
            self._batch_api = client.BatchV1Api()
            self._initialized = True

        except ImportError as e:
            raise KubernetesClientError(
                "kubernetes package not installed: pip install kubernetes"
            ) from e
        except Exception as e:
            raise KubernetesClientError(
                f"Failed to initialize Kubernetes client: {e}"
            ) from e

    def is_connected(self) -> bool:
        """Check if Kubernetes API is available.

        Returns:
            True if the Kubernetes API server is accessible.
        """
        try:
            self._ensure_initialized()
            self._core_api.get_api_versions()
            return True
        except Exception:
            return False

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
        """Create a new container as a Kubernetes Job.

        Args:
            name: Job name.
            image: Container image (default: egg:latest).
            environment: Environment variables.
            volumes: Volume mounts (mapped to k8s volumes).
            network: Ignored for Kubernetes (networking is cluster-managed).
            command: Command to run.
            labels: Job labels.
            **kwargs: Additional arguments (active_deadline_seconds, etc.).

        Returns:
            ContainerInfo with Job details.

        Raises:
            ImagePullError: If image cannot be pulled.
            JobOperationError: If Job creation fails.
        """
        image = image or self.DEFAULT_SANDBOX_IMAGE
        active_deadline_seconds = kwargs.pop("active_deadline_seconds", None)

        return self.create_job(
            name=name,
            namespace=self.namespace,
            image=image,
            environment=environment,
            labels=labels,
            volumes=volumes,
            command=command,
            active_deadline_seconds=active_deadline_seconds,
        )

    def start_container(self, container_id: str) -> ContainerInfo:
        """Start a container (no-op for Kubernetes).

        Kubernetes Jobs auto-start on creation, so this simply
        returns the current Job status.

        Args:
            container_id: Job name.

        Returns:
            Current ContainerInfo.
        """
        return self.get_container_info(container_id)

    def stop_container(self, container_id: str, timeout: int = 10) -> ContainerInfo:
        """Stop a container by deleting the Kubernetes Job.

        Args:
            container_id: Job name.
            timeout: Grace period in seconds (used as propagation grace).

        Returns:
            ContainerInfo with exited status.

        Raises:
            PodNotFoundError: If Job doesn't exist.
            JobOperationError: If deletion fails.
        """
        info = self.get_container_info(container_id)
        self.delete_job(container_id, self.namespace)

        return ContainerInfo(
            container_id=info.container_id,
            container_name=info.container_name,
            status=ContainerStatus.EXITED,
            exit_code=info.exit_code,
            started_at=info.started_at,
            exited_at=datetime.now(UTC),
        )

    def remove_container(
        self,
        container_id: str,
        force: bool = False,
        v: bool = True,
    ) -> None:
        """Remove a container by deleting the Kubernetes Job.

        Args:
            container_id: Job name.
            force: Ignored for Kubernetes (Jobs are always force-deleted).
            v: Ignored for Kubernetes (no volume semantics on Jobs).

        Raises:
            PodNotFoundError: If Job doesn't exist.
            JobOperationError: If deletion fails.
        """
        self.delete_job(container_id, self.namespace)

    def get_container_info(self, container_id: str) -> ContainerInfo:
        """Get container information from the Kubernetes Job.

        Args:
            container_id: Job name.

        Returns:
            ContainerInfo with current state.

        Raises:
            PodNotFoundError: If Job doesn't exist.
            JobOperationError: If status retrieval fails.
        """
        self._ensure_initialized()
        from kubernetes.client.exceptions import ApiException

        try:
            job = self._batch_api.read_namespaced_job(
                name=container_id, namespace=self.namespace
            )
        except ApiException as e:
            if e.status == 404:
                raise PodNotFoundError(f"Job {container_id} not found") from e
            raise JobOperationError(f"Failed to get Job info: {e}") from e

        # Determine status from Job conditions
        status = ContainerStatus.PENDING
        exit_code = None
        started_at = job.status.start_time
        exited_at = None

        if job.status.conditions:
            for condition in job.status.conditions:
                if condition.type == "Complete" and condition.status == "True":
                    status = ContainerStatus.EXITED
                    exit_code = 0
                    exited_at = condition.last_transition_time
                    break
                if condition.type == "Failed" and condition.status == "True":
                    status = ContainerStatus.FAILED
                    exit_code = 1
                    exited_at = condition.last_transition_time
                    break
        elif job.status.active and job.status.active > 0:
            status = ContainerStatus.RUNNING

        # Try to get more precise exit code from pod
        pod_name = self.get_pod_for_job(container_id, self.namespace)
        if pod_name and exit_code is not None:
            try:
                pod_status = self._get_pod_exit_code(pod_name, self.namespace)
                if pod_status is not None:
                    exit_code = pod_status
            except Exception:
                pass

        # Get agent role from labels
        agent_role = None
        if job.metadata.labels:
            agent_role_str = job.metadata.labels.get("egg.agent.role")
            if agent_role_str:
                from models import AgentRole

                try:
                    agent_role = AgentRole(agent_role_str)
                except ValueError:
                    pass

        return ContainerInfo(
            container_id=container_id,
            container_name=container_id,
            status=status,
            started_at=started_at,
            exited_at=exited_at,
            exit_code=exit_code,
            agent_role=agent_role,
        )

    def list_containers(
        self,
        all: bool = True,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List Jobs matching filters.

        Args:
            all: Include completed/failed Jobs.
            labels: Label filters.

        Returns:
            List of ContainerInfo.
        """
        label_parts = ["egg.orchestrator=true"]
        if labels:
            for key, value in labels.items():
                label_parts.append(f"{key}={value}")
        label_selector = ",".join(label_parts)

        return self.list_jobs(self.namespace, label_selector=label_selector)

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 100,
        since: datetime | None = None,
    ) -> str:
        """Get container logs from the Job's pod.

        Args:
            container_id: Job name.
            tail: Number of lines from the end.
            since: Only logs since this time.

        Returns:
            Log output as string.

        Raises:
            PodNotFoundError: If no pod found for the Job.
        """
        pod_name = self.get_pod_for_job(container_id, self.namespace)
        if not pod_name:
            raise PodNotFoundError(f"No pod found for Job {container_id}")

        return self.get_pod_logs(pod_name, self.namespace, tail=tail)

    def wait_for_container(self, container_id: str, timeout: int = 300) -> ContainerInfo:
        """Wait for Job to complete.

        Polls the Job status until it reaches a terminal state
        (Complete or Failed) or the timeout is exceeded.

        Args:
            container_id: Job name.
            timeout: Max seconds to wait.

        Returns:
            ContainerInfo with exit status.

        Raises:
            PodNotFoundError: If Job doesn't exist.
            JobOperationError: If timeout exceeded.
        """
        deadline = time.monotonic() + timeout
        poll_interval = 2.0

        while time.monotonic() < deadline:
            info = self.get_container_info(container_id)
            if info.status in (ContainerStatus.EXITED, ContainerStatus.FAILED):
                return info
            time.sleep(min(poll_interval, deadline - time.monotonic()))
            poll_interval = min(poll_interval * 1.5, 15.0)

        raise JobOperationError(
            f"Timeout waiting for Job {container_id} after {timeout}s"
        )

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int:
        """Remove orphaned Jobs older than max_age_hours.

        Args:
            max_age_hours: Max age before considering orphaned.

        Returns:
            Number of Jobs removed.
        """
        removed = 0
        cutoff = datetime.now(UTC)

        for info in self.list_containers(all=True):
            if info.status in (ContainerStatus.EXITED, ContainerStatus.FAILED):
                if info.exited_at:
                    age_hours = (cutoff - info.exited_at).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        try:
                            self.delete_job(info.container_id, self.namespace)
                            removed += 1
                        except JobOperationError:
                            pass

        if removed:
            logger.info("Cleaned up orphaned Jobs", count=removed)

        return removed

    # ---- Kubernetes-specific methods ----

    def create_job(
        self,
        name: str,
        namespace: str,
        image: str,
        environment: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        command: list[str] | None = None,
        active_deadline_seconds: int | None = None,
    ) -> ContainerInfo:
        """Create a Kubernetes Job.

        Args:
            name: Job name.
            namespace: Kubernetes namespace.
            image: Container image.
            environment: Environment variables.
            labels: Job labels.
            volumes: Volume mounts (host_path -> mount config).
            command: Command to run.
            active_deadline_seconds: Max time the Job may run.

        Returns:
            ContainerInfo with Job details.

        Raises:
            JobOperationError: If Job creation fails.
        """
        self._ensure_initialized()
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException

        # Build labels
        job_labels = {
            "egg.orchestrator": "true",
            "egg.container.name": name,
            "egg.created_at": datetime.now(UTC).isoformat(),
        }
        if labels:
            job_labels.update(labels)

        # Build environment variables
        env_vars = []
        if environment:
            env_vars = [
                client.V1EnvVar(name=k, value=v) for k, v in environment.items()
            ]

        # Build volume mounts and volumes
        k8s_volumes: list[client.V1Volume] = []
        k8s_volume_mounts: list[client.V1VolumeMount] = []
        if volumes:
            for idx, (host_path, mount_config) in enumerate(volumes.items()):
                vol_name = f"vol-{idx}"
                bind = mount_config.get("bind", f"/mnt/vol-{idx}")
                mode = mount_config.get("mode", "rw")
                k8s_volumes.append(
                    client.V1Volume(
                        name=vol_name,
                        host_path=client.V1HostPathVolumeSource(path=host_path),
                    )
                )
                k8s_volume_mounts.append(
                    client.V1VolumeMount(
                        name=vol_name,
                        mount_path=bind,
                        read_only=(mode == "ro"),
                    )
                )

        # Build container spec
        container = client.V1Container(
            name=name,
            image=image,
            command=command,
            env=env_vars or None,
            volume_mounts=k8s_volume_mounts or None,
        )

        # Build pod template
        pod_template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=job_labels),
            spec=client.V1PodSpec(
                containers=[container],
                restart_policy="Never",
                volumes=k8s_volumes or None,
            ),
        )

        # Build Job spec
        job_spec = client.V1JobSpec(
            template=pod_template,
            backoff_limit=0,
            active_deadline_seconds=active_deadline_seconds,
        )

        # Build Job object
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels=job_labels,
            ),
            spec=job_spec,
        )

        try:
            self._batch_api.create_namespaced_job(namespace=namespace, body=job)

            logger.info(
                "Job created",
                job_name=name,
                namespace=namespace,
                image=image,
            )

            return ContainerInfo(
                container_id=name,
                container_name=name,
                status=ContainerStatus.PENDING,
            )

        except ApiException as e:
            if "ImagePull" in str(e):
                raise ImagePullError(f"Failed to pull image {image}: {e}") from e
            raise JobOperationError(f"Failed to create Job {name}: {e}") from e

    def delete_job(self, name: str, namespace: str) -> None:
        """Delete a Kubernetes Job and its pods.

        Args:
            name: Job name.
            namespace: Kubernetes namespace.

        Raises:
            PodNotFoundError: If Job doesn't exist.
            JobOperationError: If deletion fails.
        """
        self._ensure_initialized()
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException

        try:
            self._batch_api.delete_namespaced_job(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(
                    propagation_policy="Foreground",
                ),
            )
            logger.info("Job deleted", job_name=name, namespace=namespace)

        except ApiException as e:
            if e.status == 404:
                raise PodNotFoundError(f"Job {name} not found") from e
            raise JobOperationError(f"Failed to delete Job {name}: {e}") from e

    def list_jobs(
        self,
        namespace: str,
        label_selector: str | None = None,
    ) -> list[ContainerInfo]:
        """List Kubernetes Jobs.

        Args:
            namespace: Kubernetes namespace.
            label_selector: Label selector string (e.g. "egg.orchestrator=true").

        Returns:
            List of ContainerInfo.
        """
        self._ensure_initialized()
        from kubernetes.client.exceptions import ApiException

        try:
            jobs = self._batch_api.list_namespaced_job(
                namespace=namespace,
                label_selector=label_selector or "",
            )
        except ApiException as e:
            raise JobOperationError(f"Failed to list Jobs: {e}") from e

        results = []
        for job in jobs.items:
            try:
                info = self.get_container_info(job.metadata.name)
                results.append(info)
            except Exception:
                pass

        return results

    def get_pod_for_job(self, job_name: str, namespace: str) -> str | None:
        """Get the pod name for a Job.

        Args:
            job_name: Job name.
            namespace: Kubernetes namespace.

        Returns:
            Pod name, or None if no pod found.
        """
        self._ensure_initialized()

        try:
            pods = self._core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            )
            if pods.items:
                return pods.items[0].metadata.name
        except Exception:
            pass

        return None

    def get_pod_logs(
        self,
        pod_name: str,
        namespace: str,
        tail: int = 100,
    ) -> str:
        """Get logs from a pod.

        Args:
            pod_name: Pod name.
            namespace: Kubernetes namespace.
            tail: Number of lines from the end.

        Returns:
            Log output as string.

        Raises:
            PodNotFoundError: If pod doesn't exist.
        """
        self._ensure_initialized()
        from kubernetes.client.exceptions import ApiException

        try:
            logs = self._core_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=tail,
            )
            return logs or ""

        except ApiException as e:
            if e.status == 404:
                raise PodNotFoundError(f"Pod {pod_name} not found") from e
            raise KubernetesClientError(f"Failed to get pod logs: {e}") from e

    def get_pod_status(self, pod_name: str, namespace: str) -> ContainerStatus:
        """Get the status of a pod.

        Args:
            pod_name: Pod name.
            namespace: Kubernetes namespace.

        Returns:
            ContainerStatus representing the pod phase.

        Raises:
            PodNotFoundError: If pod doesn't exist.
        """
        self._ensure_initialized()
        from kubernetes.client.exceptions import ApiException

        try:
            pod = self._core_api.read_namespaced_pod(
                name=pod_name, namespace=namespace
            )
        except ApiException as e:
            if e.status == 404:
                raise PodNotFoundError(f"Pod {pod_name} not found") from e
            raise KubernetesClientError(f"Failed to get pod status: {e}") from e

        phase = pod.status.phase
        if phase == "Running":
            return ContainerStatus.RUNNING
        elif phase == "Succeeded":
            return ContainerStatus.EXITED
        elif phase == "Failed":
            return ContainerStatus.FAILED
        elif phase == "Pending":
            return ContainerStatus.PENDING
        else:
            return ContainerStatus.FAILED

    def _get_pod_exit_code(self, pod_name: str, namespace: str) -> int | None:
        """Get the exit code from a pod's first container.

        Args:
            pod_name: Pod name.
            namespace: Kubernetes namespace.

        Returns:
            Exit code, or None if not available.
        """
        try:
            pod = self._core_api.read_namespaced_pod(
                name=pod_name, namespace=namespace
            )
            if pod.status.container_statuses:
                terminated = pod.status.container_statuses[0].state.terminated
                if terminated:
                    return terminated.exit_code
        except Exception:
            pass
        return None


_kubernetes_client: KubernetesClient | None = None


def get_kubernetes_client(
    namespace: str = DEFAULT_AGENT_NAMESPACE,
) -> KubernetesClient:
    """Get the singleton Kubernetes client.

    Args:
        namespace: Default namespace for agent Jobs.

    Returns:
        KubernetesClient instance.
    """
    global _kubernetes_client
    if _kubernetes_client is None:
        _kubernetes_client = KubernetesClient(namespace=namespace)
    return _kubernetes_client
