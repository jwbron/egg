"""
Kubernetes client for container operations.

Provides container lifecycle management by mapping the ContainerBackend
protocol onto Kubernetes Jobs and Pods. Used as a drop-in replacement
for DockerClient when running the orchestrator on Kubernetes.
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


from models import ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.kubernetes")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KubernetesClientError(Exception):
    """Base exception for Kubernetes client errors."""


class PodNotFoundError(KubernetesClientError):
    """Pod not found in the cluster."""


class JobOperationError(KubernetesClientError):
    """A Job-level operation failed."""


class ImagePullError(KubernetesClientError):
    """Failed to pull a container image."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_NAMESPACE = "egg-agents"
LABEL_ORCHESTRATOR = "egg.orchestrator"
LABEL_PIPELINE_ID = "egg.pipeline.id"
LABEL_AGENT_ROLE = "egg.agent.role"
LABEL_CONTAINER_NAME = "egg.container.name"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pod_phase_to_status(phase: str | None) -> ContainerStatus:
    """Map a Kubernetes pod phase string to a ContainerStatus."""
    mapping: dict[str, ContainerStatus] = {
        "Pending": ContainerStatus.PENDING,
        "Running": ContainerStatus.RUNNING,
        "Succeeded": ContainerStatus.EXITED,
        "Failed": ContainerStatus.FAILED,
        "Unknown": ContainerStatus.FAILED,
    }
    return mapping.get(phase or "", ContainerStatus.PENDING)


def _parse_k8s_datetime(ts: Any) -> datetime | None:
    """Parse a Kubernetes API datetime value.

    The ``kubernetes`` Python client deserialises timestamps as
    ``datetime`` objects already, but we guard against ``None`` and
    string representations for robustness.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# KubernetesClient
# ---------------------------------------------------------------------------


class KubernetesClient:
    """Kubernetes client for sandbox container management.

    Wraps the official ``kubernetes`` Python client to provide the same
    :class:`ContainerBackend` interface as :class:`DockerClient`, mapping
    k8s Jobs/Pods to the container lifecycle model.
    """

    DEFAULT_SANDBOX_IMAGE = "egg:latest"
    JOB_PREFIX = "egg-sandbox-"

    def __init__(
        self,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        _batch_api: Any | None = None,
        _core_api: Any | None = None,
    ) -> None:
        """Initialise the Kubernetes client.

        Attempts in-cluster configuration first (for pods running inside
        k8s), falling back to the local kubeconfig.

        Args:
            namespace: Default namespace for Jobs and Pods.
            _batch_api: Override for ``BatchV1Api`` (testing).
            _core_api: Override for ``CoreV1Api`` (testing).
        """
        self.namespace = namespace

        if _batch_api is not None and _core_api is not None:
            self.batch_api = _batch_api
            self.core_api = _core_api
            return

        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
                logger.info("Loaded in-cluster Kubernetes config")
            except config.ConfigException:
                config.load_kube_config()
                logger.info("Loaded kubeconfig from file")

            self.batch_api = client.BatchV1Api()
            self.core_api = client.CoreV1Api()
        except Exception as exc:
            raise KubernetesClientError(
                f"Failed to initialise Kubernetes client: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # ContainerBackend protocol — public interface
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Check if the Kubernetes API server is reachable."""
        try:
            self.core_api.get_api_resources()
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
        """Create a Kubernetes Job that runs a single pod.

        The ``volumes`` and ``network`` parameters are accepted for
        protocol compatibility but are currently not translated to k8s
        volume mounts or network policies — those are expected to be
        configured via the pod template in future phases.
        """
        from kubernetes import client as k8s_client

        image = image or self.DEFAULT_SANDBOX_IMAGE
        job_name = f"{self.JOB_PREFIX}{name}"

        # Build labels
        job_labels: dict[str, str] = {
            LABEL_ORCHESTRATOR: "true",
            LABEL_CONTAINER_NAME: name,
        }
        if labels:
            job_labels.update(labels)

        # Build environment
        env_vars: list[Any] = []
        if environment:
            env_vars = [
                k8s_client.V1EnvVar(name=k, value=v)
                for k, v in environment.items()
            ]

        container = k8s_client.V1Container(
            name="agent",
            image=image,
            env=env_vars or None,
            command=command or None,
        )

        pod_spec = k8s_client.V1PodSpec(
            containers=[container],
            restart_policy="Never",
        )

        template = k8s_client.V1PodTemplateSpec(
            metadata=k8s_client.V1ObjectMeta(labels=job_labels),
            spec=pod_spec,
        )

        job_spec = k8s_client.V1JobSpec(
            template=template,
            backoff_limit=0,
        )

        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                namespace=self.namespace,
                labels=job_labels,
            ),
            spec=job_spec,
        )

        try:
            created_job = self.batch_api.create_namespaced_job(
                namespace=self.namespace,
                body=job,
            )

            uid = created_job.metadata.uid or job_name
            logger.info(
                "Job created",
                job_name=job_name,
                namespace=self.namespace,
                image=image,
            )

            return ContainerInfo(
                container_id=uid,
                container_name=job_name,
                status=ContainerStatus.PENDING,
                namespace=self.namespace,
                job_name=job_name,
            )
        except Exception as exc:
            error_msg = str(exc)
            if "ImagePull" in error_msg or "ErrImagePull" in error_msg:
                raise ImagePullError(f"Failed to pull image {image}: {exc}") from exc
            raise JobOperationError(f"Failed to create job {job_name}: {exc}") from exc

    def start_container(self, container_id: str) -> ContainerInfo:
        """Check the status of the Job's pod.

        Kubernetes Jobs auto-start their pods, so this method simply
        retrieves the current status rather than issuing a start command.
        """
        return self.get_container_info(container_id)

    def stop_container(self, container_id: str, timeout: int = 10) -> ContainerInfo:
        """Stop a container by deleting its Job.

        Args:
            container_id: The Job name or UID.
            timeout: Grace period in seconds (mapped to
                ``grace_period_seconds`` on the delete options).
        """
        job_name = self._resolve_job_name(container_id)
        try:
            self.delete_job(job_name, self.namespace)
            logger.info("Job stopped (deleted)", job_name=job_name)
            return ContainerInfo(
                container_id=container_id,
                container_name=job_name,
                status=ContainerStatus.EXITED,
                exited_at=datetime.now(UTC),
                namespace=self.namespace,
                job_name=job_name,
            )
        except Exception as exc:
            raise JobOperationError(f"Failed to stop job {job_name}: {exc}") from exc

    def remove_container(
        self,
        container_id: str,
        force: bool = False,
        v: bool = True,
    ) -> None:
        """Remove a Job and its pods.

        Uses ``Foreground`` propagation when *force* is ``True`` so that
        all dependent pods are deleted before the call returns.
        """
        job_name = self._resolve_job_name(container_id)
        propagation = "Foreground" if force else "Background"
        try:
            self.delete_job(job_name, self.namespace, propagation_policy=propagation)
            logger.info("Job removed", job_name=job_name, propagation=propagation)
        except Exception as exc:
            raise JobOperationError(f"Failed to remove job {job_name}: {exc}") from exc

    def get_container_info(self, container_id: str) -> ContainerInfo:
        """Get information about a Job's pod."""
        job_name = self._resolve_job_name(container_id)
        try:
            pod_name = self.get_pod_for_job(job_name, self.namespace)
            status = self.get_pod_status(pod_name, self.namespace)

            # Fetch pod for timestamps
            pod = self.core_api.read_namespaced_pod(pod_name, self.namespace)
            started_at = _parse_k8s_datetime(
                pod.status.start_time if pod.status else None
            )

            exited_at: datetime | None = None
            exit_code: int | None = None
            if pod.status and pod.status.container_statuses:
                cs = pod.status.container_statuses[0]
                if cs.state and cs.state.terminated:
                    exited_at = _parse_k8s_datetime(cs.state.terminated.finished_at)
                    exit_code = cs.state.terminated.exit_code

            # Extract agent role from labels
            from models import AgentRole

            agent_role = None
            pod_labels = pod.metadata.labels or {}
            role_str = pod_labels.get(LABEL_AGENT_ROLE)
            if role_str:
                try:
                    agent_role = AgentRole(role_str)
                except ValueError:
                    pass

            return ContainerInfo(
                container_id=container_id,
                container_name=job_name,
                status=status,
                started_at=started_at,
                exited_at=exited_at,
                exit_code=exit_code,
                agent_role=agent_role,
                pod_name=pod_name,
                namespace=self.namespace,
                job_name=job_name,
            )
        except PodNotFoundError:
            raise
        except Exception as exc:
            raise JobOperationError(
                f"Failed to get info for job {job_name}: {exc}"
            ) from exc

    def list_containers(
        self,
        all: bool = True,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List pods matching label filters.

        Args:
            all: Ignored (k8s always returns all matching pods).
            labels: Additional label selectors to filter by.
        """
        selector_parts = [f"{LABEL_ORCHESTRATOR}=true"]
        if labels:
            for key, value in labels.items():
                selector_parts.append(f"{key}={value}")
        label_selector = ",".join(selector_parts)

        try:
            pods = self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector,
            )

            results: list[ContainerInfo] = []
            for pod in pods.items:
                pod_labels = pod.metadata.labels or {}
                status = _pod_phase_to_status(
                    pod.status.phase if pod.status else None
                )
                started_at = _parse_k8s_datetime(
                    pod.status.start_time if pod.status else None
                )

                exited_at: datetime | None = None
                exit_code: int | None = None
                if pod.status and pod.status.container_statuses:
                    cs = pod.status.container_statuses[0]
                    if cs.state and cs.state.terminated:
                        exited_at = _parse_k8s_datetime(cs.state.terminated.finished_at)
                        exit_code = cs.state.terminated.exit_code

                from models import AgentRole

                agent_role = None
                role_str = pod_labels.get(LABEL_AGENT_ROLE)
                if role_str:
                    try:
                        agent_role = AgentRole(role_str)
                    except ValueError:
                        pass

                job_name = pod_labels.get(LABEL_CONTAINER_NAME, pod.metadata.name)

                results.append(
                    ContainerInfo(
                        container_id=pod.metadata.uid or pod.metadata.name,
                        container_name=pod.metadata.name,
                        status=status,
                        started_at=started_at,
                        exited_at=exited_at,
                        exit_code=exit_code,
                        agent_role=agent_role,
                        pod_name=pod.metadata.name,
                        namespace=self.namespace,
                        job_name=f"{self.JOB_PREFIX}{job_name}",
                    )
                )

            return results
        except Exception as exc:
            raise JobOperationError(f"Failed to list pods: {exc}") from exc

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 100,
        since: datetime | None = None,
    ) -> str:
        """Get logs from a Job's pod."""
        job_name = self._resolve_job_name(container_id)
        try:
            pod_name = self.get_pod_for_job(job_name, self.namespace)
            since_seconds: int | None = None
            if since:
                delta = datetime.now(UTC) - since
                since_seconds = max(int(delta.total_seconds()), 1)
            return self.get_pod_logs(
                pod_name, self.namespace,
                tail_lines=tail,
                since_seconds=since_seconds,
            )
        except PodNotFoundError:
            raise
        except Exception as exc:
            raise JobOperationError(
                f"Failed to get logs for job {job_name}: {exc}"
            ) from exc

    def wait_for_container(
        self,
        container_id: str,
        timeout: int = 300,
    ) -> ContainerInfo:
        """Wait for a Job's pod to reach a terminal state."""
        job_name = self._resolve_job_name(container_id)
        deadline = time.monotonic() + timeout
        poll_interval = 2.0

        while True:
            try:
                pod_name = self.get_pod_for_job(job_name, self.namespace)
                status = self.get_pod_status(pod_name, self.namespace)

                if status in (ContainerStatus.EXITED, ContainerStatus.FAILED):
                    return self.get_container_info(container_id)

            except PodNotFoundError:
                pass  # Pod may not be scheduled yet

            if time.monotonic() >= deadline:
                raise JobOperationError(
                    f"Timed out waiting for job {job_name} after {timeout}s"
                )

            remaining = deadline - time.monotonic()
            time.sleep(min(poll_interval, max(remaining, 0.1)))

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int:
        """Delete completed/failed Jobs older than *max_age_hours*."""
        removed = 0
        cutoff = datetime.now(UTC)

        try:
            jobs = self.list_jobs(self.namespace, label_selector=f"{LABEL_ORCHESTRATOR}=true")
        except Exception:
            return 0

        for info in jobs:
            if info.status in (ContainerStatus.EXITED, ContainerStatus.FAILED):
                ended = info.exited_at or info.started_at
                if ended:
                    age_hours = (cutoff - ended).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        try:
                            self.remove_container(info.container_id, force=True)
                            removed += 1
                        except JobOperationError:
                            pass

        if removed:
            logger.info("Cleaned up orphaned jobs", count=removed)

        return removed

    # ------------------------------------------------------------------
    # Kubernetes-native methods
    # ------------------------------------------------------------------

    def create_job(
        self,
        name: str,
        namespace: str,
        job_spec: Any,
    ) -> ContainerInfo:
        """Create a Kubernetes Job from a raw spec.

        Args:
            name: Job name.
            namespace: Target namespace.
            job_spec: A ``V1Job`` object (or compatible dict).

        Returns:
            ContainerInfo representing the created Job.
        """
        try:
            created = self.batch_api.create_namespaced_job(
                namespace=namespace,
                body=job_spec,
            )
            uid = created.metadata.uid or name
            logger.info("Job created (raw spec)", job_name=name, namespace=namespace)
            return ContainerInfo(
                container_id=uid,
                container_name=name,
                status=ContainerStatus.PENDING,
                namespace=namespace,
                job_name=name,
            )
        except Exception as exc:
            raise JobOperationError(f"Failed to create job {name}: {exc}") from exc

    def delete_job(
        self,
        name: str,
        namespace: str,
        propagation_policy: str = "Background",
    ) -> None:
        """Delete a Kubernetes Job.

        Args:
            name: Job name.
            namespace: Namespace containing the Job.
            propagation_policy: ``Background``, ``Foreground``, or ``Orphan``.
        """
        from kubernetes import client as k8s_client

        try:
            self.batch_api.delete_namespaced_job(
                name=name,
                namespace=namespace,
                body=k8s_client.V1DeleteOptions(
                    propagation_policy=propagation_policy,
                ),
            )
            logger.info("Job deleted", job_name=name, namespace=namespace)
        except Exception as exc:
            error_msg = str(exc).lower()
            if "not found" in error_msg or "404" in error_msg:
                raise PodNotFoundError(f"Job {name} not found in {namespace}") from exc
            raise JobOperationError(f"Failed to delete job {name}: {exc}") from exc

    def list_jobs(
        self,
        namespace: str,
        label_selector: str | None = None,
    ) -> list[ContainerInfo]:
        """List Kubernetes Jobs in *namespace*.

        Args:
            namespace: Namespace to query.
            label_selector: Optional label selector string.

        Returns:
            List of ContainerInfo, one per Job.
        """
        try:
            jobs = self.batch_api.list_namespaced_job(
                namespace=namespace,
                label_selector=label_selector or "",
            )

            results: list[ContainerInfo] = []
            for job in jobs.items:
                uid = job.metadata.uid or job.metadata.name
                job_name = job.metadata.name

                # Determine status from Job conditions
                status = ContainerStatus.PENDING
                exited_at: datetime | None = None
                if job.status:
                    if job.status.succeeded and job.status.succeeded > 0:
                        status = ContainerStatus.EXITED
                    elif job.status.failed and job.status.failed > 0:
                        status = ContainerStatus.FAILED
                    elif job.status.active and job.status.active > 0:
                        status = ContainerStatus.RUNNING

                    completion = getattr(job.status, "completion_time", None)
                    exited_at = _parse_k8s_datetime(completion)

                started_at = _parse_k8s_datetime(
                    job.status.start_time if job.status else None
                )

                results.append(
                    ContainerInfo(
                        container_id=uid,
                        container_name=job_name,
                        status=status,
                        started_at=started_at,
                        exited_at=exited_at,
                        namespace=namespace,
                        job_name=job_name,
                    )
                )

            return results
        except Exception as exc:
            raise JobOperationError(f"Failed to list jobs: {exc}") from exc

    def get_pod_for_job(
        self,
        job_name: str,
        namespace: str,
    ) -> str:
        """Find the pod belonging to *job_name*.

        Returns the name of the first matching pod.

        Raises:
            PodNotFoundError: If no pod is found for the Job.
        """
        label_selector = f"job-name={job_name}"
        try:
            pods = self.core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector,
            )
            if not pods.items:
                raise PodNotFoundError(
                    f"No pods found for job {job_name} in {namespace}"
                )
            return pods.items[0].metadata.name
        except PodNotFoundError:
            raise
        except Exception as exc:
            raise JobOperationError(
                f"Failed to find pod for job {job_name}: {exc}"
            ) from exc

    def get_pod_logs(
        self,
        pod_name: str,
        namespace: str,
        tail_lines: int = 100,
        since_seconds: int | None = None,
    ) -> str:
        """Read logs from a pod.

        Args:
            pod_name: Pod name.
            namespace: Namespace containing the pod.
            tail_lines: Number of trailing log lines to return.
            since_seconds: Only return logs newer than this many seconds.

        Returns:
            Log text.
        """
        try:
            kwargs: dict[str, Any] = {
                "name": pod_name,
                "namespace": namespace,
                "tail_lines": tail_lines,
            }
            if since_seconds is not None:
                kwargs["since_seconds"] = since_seconds

            return self.core_api.read_namespaced_pod_log(**kwargs)
        except Exception as exc:
            error_msg = str(exc).lower()
            if "not found" in error_msg or "404" in error_msg:
                raise PodNotFoundError(f"Pod {pod_name} not found in {namespace}") from exc
            raise JobOperationError(
                f"Failed to get logs for pod {pod_name}: {exc}"
            ) from exc

    def get_pod_status(
        self,
        pod_name: str,
        namespace: str,
    ) -> ContainerStatus:
        """Get the status of a pod.

        Args:
            pod_name: Pod name.
            namespace: Namespace containing the pod.

        Returns:
            Mapped ContainerStatus.
        """
        try:
            pod = self.core_api.read_namespaced_pod(pod_name, namespace)
            phase = pod.status.phase if pod.status else None

            # Check container statuses for waiting/image-pull errors
            if pod.status and pod.status.container_statuses:
                cs = pod.status.container_statuses[0]
                if cs.state and cs.state.waiting:
                    reason = cs.state.waiting.reason or ""
                    if "ImagePull" in reason or "ErrImagePull" in reason:
                        raise ImagePullError(
                            f"Image pull failed for pod {pod_name}: {reason}"
                        )

            return _pod_phase_to_status(phase)
        except (PodNotFoundError, ImagePullError):
            raise
        except Exception as exc:
            error_msg = str(exc).lower()
            if "not found" in error_msg or "404" in error_msg:
                raise PodNotFoundError(f"Pod {pod_name} not found in {namespace}") from exc
            raise JobOperationError(
                f"Failed to get status for pod {pod_name}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_job_name(self, container_id: str) -> str:
        """Resolve a container_id to a Job name.

        If *container_id* already starts with the job prefix it is used
        as-is; otherwise we try to find a job whose UID matches.  As a
        last resort the raw value is returned.
        """
        if container_id.startswith(self.JOB_PREFIX):
            return container_id

        # Attempt UID lookup
        try:
            jobs = self.batch_api.list_namespaced_job(
                namespace=self.namespace,
                label_selector=f"{LABEL_ORCHESTRATOR}=true",
            )
            for job in jobs.items:
                if job.metadata.uid == container_id:
                    return job.metadata.name
        except Exception:
            pass

        return container_id


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_kubernetes_client: KubernetesClient | None = None


def get_kubernetes_client(namespace: str = DEFAULT_NAMESPACE) -> KubernetesClient:
    """Get the singleton Kubernetes client.

    Args:
        namespace: Default namespace (only used on first call).

    Returns:
        KubernetesClient instance.
    """
    global _kubernetes_client
    if _kubernetes_client is None:
        _kubernetes_client = KubernetesClient(namespace=namespace)
    return _kubernetes_client
