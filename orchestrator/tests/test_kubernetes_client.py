"""
Tests for KubernetesClient.

All tests mock the Kubernetes Python SDK (`BatchV1Api` and `CoreV1Api`)
by injecting MagicMock instances through the constructor's ``_batch_api``
and ``_core_api`` parameters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes_client import (
    DEFAULT_NAMESPACE,
    LABEL_AGENT_ROLE,
    LABEL_CONTAINER_NAME,
    LABEL_ORCHESTRATOR,
    LABEL_PIPELINE_ID,
    ImagePullError,
    JobOperationError,
    KubernetesClient,
    PodNotFoundError,
    _parse_k8s_datetime,
    _pod_phase_to_status,
    get_kubernetes_client,
)
from models import AgentRole, ContainerStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_batch_api() -> MagicMock:
    """Mock BatchV1Api."""
    return MagicMock()


@pytest.fixture
def mock_core_api() -> MagicMock:
    """Mock CoreV1Api."""
    return MagicMock()


@pytest.fixture
def k8s_client(mock_batch_api: MagicMock, mock_core_api: MagicMock) -> KubernetesClient:
    """Create a KubernetesClient with injected mock APIs."""
    return KubernetesClient(
        namespace="test-ns",
        _batch_api=mock_batch_api,
        _core_api=mock_core_api,
    )


def _make_mock_pod(
    name: str = "egg-sandbox-test-abc12",
    uid: str = "pod-uid-123",
    phase: str = "Running",
    labels: dict[str, str] | None = None,
    start_time: datetime | None = None,
    container_statuses: list[Any] | None = None,
) -> MagicMock:
    """Create a mock pod object matching the k8s SDK shape."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.uid = uid
    pod.metadata.labels = labels or {LABEL_ORCHESTRATOR: "true"}
    pod.status.phase = phase
    pod.status.start_time = start_time or datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    pod.status.container_statuses = container_statuses
    return pod


def _make_mock_job(
    name: str = "egg-sandbox-test",
    uid: str = "job-uid-456",
    labels: dict[str, str] | None = None,
    succeeded: int | None = None,
    failed: int | None = None,
    active: int | None = None,
    start_time: datetime | None = None,
    completion_time: datetime | None = None,
) -> MagicMock:
    """Create a mock Job object matching the k8s SDK shape."""
    job = MagicMock()
    job.metadata.name = name
    job.metadata.uid = uid
    job.metadata.labels = labels or {LABEL_ORCHESTRATOR: "true"}
    job.status.succeeded = succeeded
    job.status.failed = failed
    job.status.active = active
    job.status.start_time = start_time or datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    job.status.completion_time = completion_time
    return job


# ---------------------------------------------------------------------------
# Constructor / Connection
# ---------------------------------------------------------------------------


class TestKubernetesClientInit:
    """Tests for client initialisation."""

    def test_init_with_injected_apis(self, k8s_client: KubernetesClient):
        """Constructor should accept injected API mocks."""
        assert k8s_client.namespace == "test-ns"
        assert k8s_client.batch_api is not None
        assert k8s_client.core_api is not None

    def test_default_namespace(self):
        """DEFAULT_NAMESPACE should be 'egg-agents'."""
        assert DEFAULT_NAMESPACE == "egg-agents"

    def test_is_connected_true(self, k8s_client: KubernetesClient, mock_core_api: MagicMock):
        """is_connected returns True when API server responds."""
        mock_core_api.get_api_resources.return_value = MagicMock()
        assert k8s_client.is_connected() is True

    def test_is_connected_false(self, k8s_client: KubernetesClient, mock_core_api: MagicMock):
        """is_connected returns False when API call fails."""
        mock_core_api.get_api_resources.side_effect = Exception("connection refused")
        assert k8s_client.is_connected() is False


# ---------------------------------------------------------------------------
# create_container
# ---------------------------------------------------------------------------


class TestCreateContainer:
    """Tests for create_container (Job creation)."""

    def test_create_container_basic(self, k8s_client: KubernetesClient, mock_batch_api: MagicMock):
        """Creating a container should create a k8s Job and return ContainerInfo."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-abc123"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        info = k8s_client.create_container(name="test-agent")

        assert info.container_id == "uid-abc123"
        assert info.container_name == "egg-sandbox-test-agent"
        assert info.status == ContainerStatus.PENDING
        assert info.namespace == "test-ns"
        assert info.job_name == "egg-sandbox-test-agent"
        mock_batch_api.create_namespaced_job.assert_called_once()

    def test_create_container_default_image(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """When no image is specified, use DEFAULT_SANDBOX_IMAGE."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-1"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(name="test")

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        container = job_body.spec.template.spec.containers[0]
        assert container.image == "egg:latest"

    def test_create_container_custom_image(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """Custom image should be used when provided."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-2"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(name="test", image="custom:v2")

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        container = job_body.spec.template.spec.containers[0]
        assert container.image == "custom:v2"

    def test_create_container_with_environment(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """Environment variables should be passed to the container spec."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-3"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(
            name="test",
            environment={"FOO": "bar", "BAZ": "qux"},
        )

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        container = job_body.spec.template.spec.containers[0]
        env_vars = container.env
        assert len(env_vars) == 2
        env_dict = {ev.name: ev.value for ev in env_vars}
        assert env_dict == {"FOO": "bar", "BAZ": "qux"}

    def test_create_container_with_labels(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """Custom labels should be merged with orchestrator labels."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-4"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(
            name="test",
            labels={"egg.pipeline.id": "issue-42", "custom": "value"},
        )

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        job_labels = job_body.metadata.labels
        assert job_labels[LABEL_ORCHESTRATOR] == "true"
        assert job_labels[LABEL_CONTAINER_NAME] == "test"
        assert job_labels["egg.pipeline.id"] == "issue-42"
        assert job_labels["custom"] == "value"

    def test_create_container_with_command(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """Command should be set on the container spec."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-5"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(name="test", command=["python", "-m", "agent"])

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        container = job_body.spec.template.spec.containers[0]
        assert container.command == ["python", "-m", "agent"]

    def test_create_container_job_has_correct_spec(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """Job spec must have backoffLimit=0, restartPolicy=Never."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-6"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(name="test")

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        assert job_body.spec.backoff_limit == 0
        assert job_body.spec.template.spec.restart_policy == "Never"

    def test_create_container_api_failure(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """API failure should raise JobOperationError."""
        mock_batch_api.create_namespaced_job.side_effect = Exception("API error")

        with pytest.raises(JobOperationError, match="Failed to create job"):
            k8s_client.create_container(name="test")

    def test_create_container_image_pull_error(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """ImagePull failure should raise ImagePullError."""
        mock_batch_api.create_namespaced_job.side_effect = Exception(
            "ImagePullBackOff: ErrImagePull"
        )

        with pytest.raises(ImagePullError, match="Failed to pull image"):
            k8s_client.create_container(name="test", image="bad:image")

    def test_create_container_uid_fallback(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """When metadata.uid is None, use job_name as container_id."""
        mock_job = MagicMock()
        mock_job.metadata.uid = None
        mock_batch_api.create_namespaced_job.return_value = mock_job

        info = k8s_client.create_container(name="test")

        assert info.container_id == "egg-sandbox-test"

    def test_create_container_no_env_sets_none(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """When no environment is provided, env should be None on container."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-7"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(name="test")

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        container = job_body.spec.template.spec.containers[0]
        assert container.env is None

    def test_create_container_no_command_sets_none(
        self, k8s_client: KubernetesClient, mock_batch_api: MagicMock
    ):
        """When no command is provided, command should be None on container."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-8"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        k8s_client.create_container(name="test")

        call_args = mock_batch_api.create_namespaced_job.call_args
        job_body = call_args.kwargs["body"]
        container = job_body.spec.template.spec.containers[0]
        assert container.command is None


# ---------------------------------------------------------------------------
# start_container (no-op — returns current info)
# ---------------------------------------------------------------------------


class TestStartContainer:
    """Tests for start_container (k8s auto-starts, so it delegates to get_container_info)."""

    def test_start_container_returns_info(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
        mock_core_api: MagicMock,
    ):
        """start_container should return current container info."""
        # Set up _resolve_job_name (prefix match)
        job_name = "egg-sandbox-test"

        # get_pod_for_job
        mock_pod_list = MagicMock()
        mock_pod_list.items = [_make_mock_pod(name="pod-123", phase="Running")]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list

        # read_namespaced_pod
        pod = _make_mock_pod(name="pod-123", phase="Running")
        pod.status.container_statuses = None
        mock_core_api.read_namespaced_pod.return_value = pod

        info = k8s_client.start_container(job_name)

        assert info.status == ContainerStatus.RUNNING


# ---------------------------------------------------------------------------
# stop_container
# ---------------------------------------------------------------------------


class TestStopContainer:
    """Tests for stop_container (deletes the Job)."""

    def test_stop_container(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Stopping a container should delete the job and return EXITED."""
        job_name = "egg-sandbox-test"

        info = k8s_client.stop_container(job_name)

        assert info.status == ContainerStatus.EXITED
        assert info.container_name == job_name
        assert info.exited_at is not None
        mock_batch_api.delete_namespaced_job.assert_called_once()

    def test_stop_container_failure(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Failure to delete job should raise JobOperationError."""
        mock_batch_api.delete_namespaced_job.side_effect = Exception("cannot delete")

        with pytest.raises(JobOperationError, match="Failed to stop job"):
            k8s_client.stop_container("egg-sandbox-test")


# ---------------------------------------------------------------------------
# remove_container
# ---------------------------------------------------------------------------


class TestRemoveContainer:
    """Tests for remove_container."""

    def test_remove_container_default(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Default removal should use Background propagation."""
        k8s_client.remove_container("egg-sandbox-test")
        mock_batch_api.delete_namespaced_job.assert_called_once()
        call_args = mock_batch_api.delete_namespaced_job.call_args
        assert call_args.kwargs["body"].propagation_policy == "Background"

    def test_remove_container_force(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Force removal should use Foreground propagation."""
        k8s_client.remove_container("egg-sandbox-test", force=True)
        call_args = mock_batch_api.delete_namespaced_job.call_args
        assert call_args.kwargs["body"].propagation_policy == "Foreground"

    def test_remove_container_not_found(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Removing a non-existent job should raise JobOperationError.

        The underlying delete_job raises PodNotFoundError, but
        remove_container wraps all exceptions as JobOperationError.
        """
        mock_batch_api.delete_namespaced_job.side_effect = Exception("404 not found")

        with pytest.raises(JobOperationError, match="Failed to remove job"):
            k8s_client.remove_container("egg-sandbox-test")

    def test_remove_container_api_error(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Generic API error should raise JobOperationError."""
        mock_batch_api.delete_namespaced_job.side_effect = Exception("server error 500")

        with pytest.raises(JobOperationError, match="Failed to remove job"):
            k8s_client.remove_container("egg-sandbox-test")


# ---------------------------------------------------------------------------
# get_container_info
# ---------------------------------------------------------------------------


class TestGetContainerInfo:
    """Tests for get_container_info."""

    def _setup_pod_lookup(
        self,
        mock_core_api: MagicMock,
        pod: MagicMock,
    ) -> None:
        """Wire up the mock APIs for get_container_info flow."""
        # get_pod_for_job
        mock_pod_list = MagicMock()
        mock_pod_list.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        # read_namespaced_pod
        mock_core_api.read_namespaced_pod.return_value = pod

    def test_running_container(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Running pod should return RUNNING status with started_at."""
        start = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        pod = _make_mock_pod(phase="Running", start_time=start)
        pod.status.container_statuses = None
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.status == ContainerStatus.RUNNING
        assert info.started_at == start

    def test_succeeded_container(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Succeeded pod should return EXITED with exit_code=0."""
        pod = _make_mock_pod(phase="Succeeded")
        cs = MagicMock()
        cs.state.terminated.finished_at = datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC)
        cs.state.terminated.exit_code = 0
        pod.status.container_statuses = [cs]
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0
        assert info.exited_at is not None

    def test_failed_container(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Failed pod should return FAILED status with non-zero exit code."""
        pod = _make_mock_pod(phase="Failed")
        cs = MagicMock()
        cs.state.terminated.finished_at = datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC)
        cs.state.terminated.exit_code = 1
        pod.status.container_statuses = [cs]
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.status == ContainerStatus.FAILED
        assert info.exit_code == 1

    def test_pending_container(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Pending pod should return PENDING status."""
        pod = _make_mock_pod(phase="Pending")
        pod.status.container_statuses = None
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.status == ContainerStatus.PENDING

    def test_container_with_agent_role(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Agent role label should be extracted from pod metadata."""
        pod = _make_mock_pod(
            phase="Running",
            labels={LABEL_ORCHESTRATOR: "true", LABEL_AGENT_ROLE: "coder"},
        )
        pod.status.container_statuses = None
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.agent_role == AgentRole.CODER

    def test_container_with_invalid_agent_role(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Invalid agent role label should result in agent_role=None."""
        pod = _make_mock_pod(
            phase="Running",
            labels={LABEL_ORCHESTRATOR: "true", LABEL_AGENT_ROLE: "invalid_role"},
        )
        pod.status.container_statuses = None
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.agent_role is None

    def test_pod_not_found(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """PodNotFoundError should propagate when no pod exists."""
        mock_pod_list = MagicMock()
        mock_pod_list.items = []
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list

        with pytest.raises(PodNotFoundError):
            k8s_client.get_container_info("egg-sandbox-test")

    def test_container_info_fields(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """All k8s-specific fields should be populated in ContainerInfo."""
        pod = _make_mock_pod(name="pod-xyz", phase="Running")
        pod.status.container_statuses = None
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.pod_name == "pod-xyz"
        assert info.namespace == "test-ns"
        assert info.job_name == "egg-sandbox-test"

    def test_no_container_statuses(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """When no container_statuses exist, exit info should be None."""
        pod = _make_mock_pod(phase="Pending")
        pod.status.container_statuses = None
        self._setup_pod_lookup(mock_core_api, pod)

        info = k8s_client.get_container_info("egg-sandbox-test")

        assert info.exit_code is None
        assert info.exited_at is None

    def test_api_failure_wraps_in_job_operation_error(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Non-PodNotFound API errors should be wrapped in JobOperationError."""
        # get_pod_for_job succeeds but read_namespaced_pod fails
        mock_pod_list = MagicMock()
        mock_pod_list.items = [_make_mock_pod()]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        mock_core_api.read_namespaced_pod.side_effect = Exception("server error")

        with pytest.raises(JobOperationError, match="Failed to get info"):
            k8s_client.get_container_info("egg-sandbox-test")


# ---------------------------------------------------------------------------
# list_containers
# ---------------------------------------------------------------------------


class TestListContainers:
    """Tests for list_containers."""

    def test_list_containers_empty(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Empty pod list should return empty list."""
        mock_result = MagicMock()
        mock_result.items = []
        mock_core_api.list_namespaced_pod.return_value = mock_result

        result = k8s_client.list_containers()

        assert result == []

    def test_list_containers_single(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should return ContainerInfo for each pod."""
        pod = _make_mock_pod(
            name="pod-1",
            uid="uid-1",
            phase="Running",
            labels={LABEL_ORCHESTRATOR: "true", LABEL_CONTAINER_NAME: "test"},
        )
        pod.status.container_statuses = None

        mock_result = MagicMock()
        mock_result.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_result

        containers = k8s_client.list_containers()

        assert len(containers) == 1
        assert containers[0].container_id == "uid-1"
        assert containers[0].status == ContainerStatus.RUNNING

    def test_list_containers_with_label_filter(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Label filters should be included in the label selector."""
        mock_result = MagicMock()
        mock_result.items = []
        mock_core_api.list_namespaced_pod.return_value = mock_result

        k8s_client.list_containers(labels={"egg.pipeline.id": "issue-42"})

        call_args = mock_core_api.list_namespaced_pod.call_args
        selector = call_args.kwargs["label_selector"]
        assert f"{LABEL_ORCHESTRATOR}=true" in selector
        assert "egg.pipeline.id=issue-42" in selector

    def test_list_containers_with_terminated_pod(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Terminated pods should have exit_code and exited_at populated."""
        pod = _make_mock_pod(
            name="pod-term",
            uid="uid-term",
            phase="Succeeded",
            labels={LABEL_ORCHESTRATOR: "true", LABEL_CONTAINER_NAME: "worker"},
        )
        cs = MagicMock()
        cs.state.terminated.finished_at = datetime(2024, 1, 15, 14, 0, 0, tzinfo=UTC)
        cs.state.terminated.exit_code = 0
        pod.status.container_statuses = [cs]

        mock_result = MagicMock()
        mock_result.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_result

        containers = k8s_client.list_containers()

        assert containers[0].status == ContainerStatus.EXITED
        assert containers[0].exit_code == 0
        assert containers[0].exited_at is not None

    def test_list_containers_with_agent_role(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Agent role should be extracted from pod labels."""
        pod = _make_mock_pod(
            name="pod-coder",
            uid="uid-coder",
            phase="Running",
            labels={
                LABEL_ORCHESTRATOR: "true",
                LABEL_CONTAINER_NAME: "coder",
                LABEL_AGENT_ROLE: "coder",
            },
        )
        pod.status.container_statuses = None

        mock_result = MagicMock()
        mock_result.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_result

        containers = k8s_client.list_containers()

        assert containers[0].agent_role == AgentRole.CODER

    def test_list_containers_api_error(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """API failures should raise JobOperationError."""
        mock_core_api.list_namespaced_pod.side_effect = Exception("forbidden")

        with pytest.raises(JobOperationError, match="Failed to list pods"):
            k8s_client.list_containers()


# ---------------------------------------------------------------------------
# get_container_logs
# ---------------------------------------------------------------------------


class TestGetContainerLogs:
    """Tests for get_container_logs."""

    def test_get_logs(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should return pod log text."""
        # get_pod_for_job
        mock_pod_list = MagicMock()
        mock_pod_list.items = [_make_mock_pod(name="pod-log")]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list

        mock_core_api.read_namespaced_pod_log.return_value = "line 1\nline 2\n"

        logs = k8s_client.get_container_logs("egg-sandbox-test")

        assert "line 1" in logs
        assert "line 2" in logs

    def test_get_logs_with_tail(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Tail parameter should be forwarded to the API."""
        mock_pod_list = MagicMock()
        mock_pod_list.items = [_make_mock_pod(name="pod-log")]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        mock_core_api.read_namespaced_pod_log.return_value = "log output"

        k8s_client.get_container_logs("egg-sandbox-test", tail=50)

        call_kwargs = mock_core_api.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["tail_lines"] == 50

    def test_get_logs_with_since(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Since parameter should be converted to since_seconds."""
        mock_pod_list = MagicMock()
        mock_pod_list.items = [_make_mock_pod(name="pod-log")]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        mock_core_api.read_namespaced_pod_log.return_value = "log output"

        since = datetime.now(UTC) - timedelta(hours=1)
        k8s_client.get_container_logs("egg-sandbox-test", since=since)

        call_kwargs = mock_core_api.read_namespaced_pod_log.call_args.kwargs
        assert "since_seconds" in call_kwargs
        # Should be approximately 3600 seconds
        assert call_kwargs["since_seconds"] >= 3599

    def test_get_logs_pod_not_found(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """PodNotFoundError should propagate when no pod exists for job."""
        mock_pod_list = MagicMock()
        mock_pod_list.items = []
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list

        with pytest.raises(PodNotFoundError):
            k8s_client.get_container_logs("egg-sandbox-test")


# ---------------------------------------------------------------------------
# wait_for_container
# ---------------------------------------------------------------------------


class TestWaitForContainer:
    """Tests for wait_for_container."""

    def test_wait_already_exited(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """If pod already exited, should return immediately."""
        pod = _make_mock_pod(phase="Succeeded")
        cs = MagicMock()
        cs.state.terminated.finished_at = datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC)
        cs.state.terminated.exit_code = 0
        pod.status.container_statuses = [cs]

        mock_pod_list = MagicMock()
        mock_pod_list.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        mock_core_api.read_namespaced_pod.return_value = pod

        info = k8s_client.wait_for_container("egg-sandbox-test", timeout=5)

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0

    def test_wait_already_failed(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """If pod already failed, should return immediately."""
        pod = _make_mock_pod(phase="Failed")
        cs = MagicMock()
        cs.state.terminated.finished_at = datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC)
        cs.state.terminated.exit_code = 1
        pod.status.container_statuses = [cs]

        mock_pod_list = MagicMock()
        mock_pod_list.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        mock_core_api.read_namespaced_pod.return_value = pod

        info = k8s_client.wait_for_container("egg-sandbox-test", timeout=5)

        assert info.status == ContainerStatus.FAILED

    def test_wait_timeout(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should raise JobOperationError on timeout."""
        pod = _make_mock_pod(phase="Running")
        pod.status.container_statuses = None

        mock_pod_list = MagicMock()
        mock_pod_list.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_pod_list
        mock_core_api.read_namespaced_pod.return_value = pod

        with patch("kubernetes_client.time") as mock_time:
            # Simulate immediate timeout
            mock_time.monotonic.side_effect = [0, 0, 100, 100]
            mock_time.sleep = MagicMock()

            with pytest.raises(JobOperationError, match="Timed out"):
                k8s_client.wait_for_container("egg-sandbox-test", timeout=1)

    def test_wait_pod_not_scheduled_yet(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should keep polling when pod not found (not scheduled yet)."""
        # First call: no pod. Second call: exited pod.
        empty_list = MagicMock()
        empty_list.items = []

        exited_pod = _make_mock_pod(phase="Succeeded")
        cs = MagicMock()
        cs.state.terminated.finished_at = datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC)
        cs.state.terminated.exit_code = 0
        exited_pod.status.container_statuses = [cs]

        found_list = MagicMock()
        found_list.items = [exited_pod]

        # First call to list_namespaced_pod returns empty (for get_pod_for_job),
        # second call returns the pod (for get_pod_for_job), third for get_pod_status,
        # and fourth for get_container_info
        mock_core_api.list_namespaced_pod.side_effect = [
            empty_list,  # 1st poll: get_pod_for_job → PodNotFoundError
            found_list,  # 2nd poll: get_pod_for_job → found
            found_list,  # get_container_info → get_pod_for_job
        ]
        mock_core_api.read_namespaced_pod.return_value = exited_pod

        with patch("kubernetes_client.time") as mock_time:
            mock_time.monotonic.side_effect = [0, 1, 1, 2, 2, 3, 3, 4]
            mock_time.sleep = MagicMock()

            info = k8s_client.wait_for_container("egg-sandbox-test", timeout=30)

        assert info.status == ContainerStatus.EXITED


# ---------------------------------------------------------------------------
# cleanup_orphaned_containers
# ---------------------------------------------------------------------------


class TestCleanupOrphanedContainers:
    """Tests for cleanup_orphaned_containers."""

    def test_cleanup_old_jobs(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should remove exited jobs older than max_age_hours."""
        old_time = datetime.now(UTC) - timedelta(hours=48)
        old_job = _make_mock_job(
            name="egg-sandbox-old",
            uid="uid-old",
            succeeded=1,
            completion_time=old_time,
            start_time=old_time - timedelta(hours=1),
        )

        mock_job_list = MagicMock()
        mock_job_list.items = [old_job]
        mock_batch_api.list_namespaced_job.return_value = mock_job_list

        removed = k8s_client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1
        mock_batch_api.delete_namespaced_job.assert_called_once()

    def test_cleanup_skips_recent_jobs(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should skip jobs that haven't exceeded max_age_hours."""
        recent_time = datetime.now(UTC) - timedelta(hours=1)
        recent_job = _make_mock_job(
            name="egg-sandbox-recent",
            uid="uid-recent",
            succeeded=1,
            completion_time=recent_time,
        )

        mock_job_list = MagicMock()
        mock_job_list.items = [recent_job]
        mock_batch_api.list_namespaced_job.return_value = mock_job_list

        removed = k8s_client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 0
        mock_batch_api.delete_namespaced_job.assert_not_called()

    def test_cleanup_skips_running_jobs(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should skip actively running jobs."""
        running_job = _make_mock_job(
            name="egg-sandbox-running",
            uid="uid-running",
            active=1,
        )

        mock_job_list = MagicMock()
        mock_job_list.items = [running_job]
        mock_batch_api.list_namespaced_job.return_value = mock_job_list

        removed = k8s_client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 0

    def test_cleanup_handles_failed_jobs(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should clean up old failed jobs too."""
        old_time = datetime.now(UTC) - timedelta(hours=48)
        failed_job = _make_mock_job(
            name="egg-sandbox-failed",
            uid="uid-failed",
            failed=1,
            completion_time=old_time,
        )

        mock_job_list = MagicMock()
        mock_job_list.items = [failed_job]
        mock_batch_api.list_namespaced_job.return_value = mock_job_list

        removed = k8s_client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1

    def test_cleanup_api_error_returns_zero(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """API failure when listing jobs should return 0."""
        mock_batch_api.list_namespaced_job.side_effect = Exception("forbidden")

        removed = k8s_client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 0

    def test_cleanup_ignores_individual_delete_failures(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should continue cleanup even when individual deletes fail."""
        old_time = datetime.now(UTC) - timedelta(hours=48)
        job1 = _make_mock_job(
            name="egg-sandbox-fail",
            uid="uid-fail",
            succeeded=1,
            completion_time=old_time,
        )
        job2 = _make_mock_job(
            name="egg-sandbox-ok",
            uid="uid-ok",
            succeeded=1,
            completion_time=old_time,
        )

        mock_job_list = MagicMock()
        mock_job_list.items = [job1, job2]
        mock_batch_api.list_namespaced_job.return_value = mock_job_list

        # First delete fails, second succeeds
        mock_batch_api.delete_namespaced_job.side_effect = [
            Exception("server error 500"),
            None,
        ]

        removed = k8s_client.cleanup_orphaned_containers(max_age_hours=24)

        # Only the second one was removed successfully
        assert removed == 1


# ---------------------------------------------------------------------------
# Kubernetes-native methods
# ---------------------------------------------------------------------------


class TestCreateJob:
    """Tests for create_job (raw spec)."""

    def test_create_job(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should create a job from raw spec and return ContainerInfo."""
        mock_job = MagicMock()
        mock_job.metadata.uid = "uid-raw"
        mock_batch_api.create_namespaced_job.return_value = mock_job

        info = k8s_client.create_job("my-job", "test-ns", MagicMock())

        assert info.container_id == "uid-raw"
        assert info.container_name == "my-job"
        assert info.status == ContainerStatus.PENDING

    def test_create_job_failure(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """API failure should raise JobOperationError."""
        mock_batch_api.create_namespaced_job.side_effect = Exception("quota exceeded")

        with pytest.raises(JobOperationError, match="Failed to create job"):
            k8s_client.create_job("my-job", "test-ns", MagicMock())


class TestDeleteJob:
    """Tests for delete_job."""

    def test_delete_job_background(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Default propagation should be Background."""
        k8s_client.delete_job("my-job", "test-ns")

        call_args = mock_batch_api.delete_namespaced_job.call_args
        assert call_args.kwargs["body"].propagation_policy == "Background"

    def test_delete_job_foreground(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Foreground propagation when requested."""
        k8s_client.delete_job("my-job", "test-ns", propagation_policy="Foreground")

        call_args = mock_batch_api.delete_namespaced_job.call_args
        assert call_args.kwargs["body"].propagation_policy == "Foreground"

    def test_delete_job_not_found(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Not-found error should raise PodNotFoundError."""
        mock_batch_api.delete_namespaced_job.side_effect = Exception("404 not found")

        with pytest.raises(PodNotFoundError):
            k8s_client.delete_job("missing", "test-ns")

    def test_delete_job_api_error(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Generic API error should raise JobOperationError."""
        mock_batch_api.delete_namespaced_job.side_effect = Exception("internal server error")

        with pytest.raises(JobOperationError, match="Failed to delete job"):
            k8s_client.delete_job("my-job", "test-ns")


class TestListJobs:
    """Tests for list_jobs."""

    def test_list_jobs_empty(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Empty job list should return empty list."""
        mock_result = MagicMock()
        mock_result.items = []
        mock_batch_api.list_namespaced_job.return_value = mock_result

        result = k8s_client.list_jobs("test-ns")

        assert result == []

    def test_list_jobs_with_status_mapping(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Job status should be mapped from k8s conditions."""
        succeeded_job = _make_mock_job(
            name="j1",
            uid="uid-1",
            succeeded=1,
            completion_time=datetime(2024, 1, 15, 13, 0, 0, tzinfo=UTC),
        )
        failed_job = _make_mock_job(name="j2", uid="uid-2", failed=1)
        active_job = _make_mock_job(name="j3", uid="uid-3", active=1)
        pending_job = _make_mock_job(name="j4", uid="uid-4")

        mock_result = MagicMock()
        mock_result.items = [succeeded_job, failed_job, active_job, pending_job]
        mock_batch_api.list_namespaced_job.return_value = mock_result

        jobs = k8s_client.list_jobs("test-ns")

        assert len(jobs) == 4
        assert jobs[0].status == ContainerStatus.EXITED
        assert jobs[1].status == ContainerStatus.FAILED
        assert jobs[2].status == ContainerStatus.RUNNING
        assert jobs[3].status == ContainerStatus.PENDING

    def test_list_jobs_with_label_selector(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Label selector should be passed to the API."""
        mock_result = MagicMock()
        mock_result.items = []
        mock_batch_api.list_namespaced_job.return_value = mock_result

        k8s_client.list_jobs("test-ns", label_selector="app=myapp")

        call_args = mock_batch_api.list_namespaced_job.call_args
        assert call_args.kwargs["label_selector"] == "app=myapp"

    def test_list_jobs_api_error(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """API failure should raise JobOperationError."""
        mock_batch_api.list_namespaced_job.side_effect = Exception("forbidden")

        with pytest.raises(JobOperationError, match="Failed to list jobs"):
            k8s_client.list_jobs("test-ns")


class TestGetPodForJob:
    """Tests for get_pod_for_job."""

    def test_finds_pod(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should return the first pod's name."""
        pod = _make_mock_pod(name="my-pod-abc12")
        mock_result = MagicMock()
        mock_result.items = [pod]
        mock_core_api.list_namespaced_pod.return_value = mock_result

        name = k8s_client.get_pod_for_job("my-job", "test-ns")

        assert name == "my-pod-abc12"

    def test_no_pod_raises(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should raise PodNotFoundError when no pods match."""
        mock_result = MagicMock()
        mock_result.items = []
        mock_core_api.list_namespaced_pod.return_value = mock_result

        with pytest.raises(PodNotFoundError, match="No pods found"):
            k8s_client.get_pod_for_job("missing-job", "test-ns")

    def test_uses_correct_label_selector(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should use job-name=<name> label selector."""
        mock_result = MagicMock()
        mock_result.items = [_make_mock_pod()]
        mock_core_api.list_namespaced_pod.return_value = mock_result

        k8s_client.get_pod_for_job("my-job", "test-ns")

        call_args = mock_core_api.list_namespaced_pod.call_args
        assert call_args.kwargs["label_selector"] == "job-name=my-job"

    def test_api_error_wraps(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Non-PodNotFound API error should be wrapped in JobOperationError."""
        mock_core_api.list_namespaced_pod.side_effect = Exception("server error")

        with pytest.raises(JobOperationError, match="Failed to find pod"):
            k8s_client.get_pod_for_job("my-job", "test-ns")


class TestGetPodLogs:
    """Tests for get_pod_logs."""

    def test_get_pod_logs(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should return log text."""
        mock_core_api.read_namespaced_pod_log.return_value = "hello world"

        logs = k8s_client.get_pod_logs("my-pod", "test-ns")

        assert logs == "hello world"

    def test_get_pod_logs_with_params(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Should forward tail_lines and since_seconds."""
        mock_core_api.read_namespaced_pod_log.return_value = "log"

        k8s_client.get_pod_logs("my-pod", "test-ns", tail_lines=50, since_seconds=3600)

        call_kwargs = mock_core_api.read_namespaced_pod_log.call_args.kwargs
        assert call_kwargs["tail_lines"] == 50
        assert call_kwargs["since_seconds"] == 3600

    def test_get_pod_logs_not_found(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Not-found error should raise PodNotFoundError."""
        mock_core_api.read_namespaced_pod_log.side_effect = Exception("404 not found")

        with pytest.raises(PodNotFoundError):
            k8s_client.get_pod_logs("missing-pod", "test-ns")

    def test_get_pod_logs_api_error(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Generic API error should raise JobOperationError."""
        mock_core_api.read_namespaced_pod_log.side_effect = Exception("internal error")

        with pytest.raises(JobOperationError, match="Failed to get logs"):
            k8s_client.get_pod_logs("my-pod", "test-ns")


class TestGetPodStatus:
    """Tests for get_pod_status."""

    def test_running_pod(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Running pod should return RUNNING status."""
        pod = _make_mock_pod(phase="Running")
        pod.status.container_statuses = None
        mock_core_api.read_namespaced_pod.return_value = pod

        status = k8s_client.get_pod_status("my-pod", "test-ns")

        assert status == ContainerStatus.RUNNING

    def test_succeeded_pod(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Succeeded pod should return EXITED status."""
        pod = _make_mock_pod(phase="Succeeded")
        pod.status.container_statuses = None
        mock_core_api.read_namespaced_pod.return_value = pod

        status = k8s_client.get_pod_status("my-pod", "test-ns")

        assert status == ContainerStatus.EXITED

    def test_failed_pod(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Failed pod should return FAILED status."""
        pod = _make_mock_pod(phase="Failed")
        pod.status.container_statuses = None
        mock_core_api.read_namespaced_pod.return_value = pod

        status = k8s_client.get_pod_status("my-pod", "test-ns")

        assert status == ContainerStatus.FAILED

    def test_pending_pod(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Pending pod should return PENDING status."""
        pod = _make_mock_pod(phase="Pending")
        pod.status.container_statuses = None
        mock_core_api.read_namespaced_pod.return_value = pod

        status = k8s_client.get_pod_status("my-pod", "test-ns")

        assert status == ContainerStatus.PENDING

    def test_image_pull_error(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """ImagePull waiting reason should raise ImagePullError."""
        pod = _make_mock_pod(phase="Pending")
        cs = MagicMock()
        cs.state.waiting.reason = "ErrImagePull"
        cs.state.terminated = None
        pod.status.container_statuses = [cs]
        mock_core_api.read_namespaced_pod.return_value = pod

        with pytest.raises(ImagePullError, match="Image pull failed"):
            k8s_client.get_pod_status("my-pod", "test-ns")

    def test_image_pull_backoff(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """ImagePullBackOff should also raise ImagePullError."""
        pod = _make_mock_pod(phase="Pending")
        cs = MagicMock()
        cs.state.waiting.reason = "ImagePullBackOff"
        cs.state.terminated = None
        pod.status.container_statuses = [cs]
        mock_core_api.read_namespaced_pod.return_value = pod

        with pytest.raises(ImagePullError):
            k8s_client.get_pod_status("my-pod", "test-ns")

    def test_pod_not_found(
        self,
        k8s_client: KubernetesClient,
        mock_core_api: MagicMock,
    ):
        """Not-found error should raise PodNotFoundError."""
        mock_core_api.read_namespaced_pod.side_effect = Exception("404 not found")

        with pytest.raises(PodNotFoundError):
            k8s_client.get_pod_status("missing-pod", "test-ns")


# ---------------------------------------------------------------------------
# _resolve_job_name
# ---------------------------------------------------------------------------


class TestResolveJobName:
    """Tests for _resolve_job_name."""

    def test_prefix_match(self, k8s_client: KubernetesClient):
        """IDs starting with JOB_PREFIX should be returned as-is."""
        result = k8s_client._resolve_job_name("egg-sandbox-test")
        assert result == "egg-sandbox-test"

    def test_uid_lookup(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """Should resolve UID to job name via API lookup."""
        job = _make_mock_job(name="egg-sandbox-found", uid="uid-to-find")
        mock_result = MagicMock()
        mock_result.items = [job]
        mock_batch_api.list_namespaced_job.return_value = mock_result

        result = k8s_client._resolve_job_name("uid-to-find")

        assert result == "egg-sandbox-found"

    def test_uid_not_found_returns_raw(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """When UID doesn't match any job, return raw container_id."""
        mock_result = MagicMock()
        mock_result.items = []
        mock_batch_api.list_namespaced_job.return_value = mock_result

        result = k8s_client._resolve_job_name("unknown-id")

        assert result == "unknown-id"

    def test_api_error_returns_raw(
        self,
        k8s_client: KubernetesClient,
        mock_batch_api: MagicMock,
    ):
        """API failure during UID lookup should return raw container_id."""
        mock_batch_api.list_namespaced_job.side_effect = Exception("API error")

        result = k8s_client._resolve_job_name("some-id")

        assert result == "some-id"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestPodPhaseToStatus:
    """Tests for _pod_phase_to_status helper."""

    @pytest.mark.parametrize(
        ("phase", "expected"),
        [
            ("Pending", ContainerStatus.PENDING),
            ("Running", ContainerStatus.RUNNING),
            ("Succeeded", ContainerStatus.EXITED),
            ("Failed", ContainerStatus.FAILED),
            ("Unknown", ContainerStatus.FAILED),
        ],
    )
    def test_known_phases(self, phase: str, expected: ContainerStatus):
        """Known pod phases should map to expected ContainerStatus."""
        assert _pod_phase_to_status(phase) == expected

    def test_none_phase(self):
        """None phase should map to PENDING."""
        assert _pod_phase_to_status(None) == ContainerStatus.PENDING

    def test_empty_string_phase(self):
        """Empty string phase should map to PENDING."""
        assert _pod_phase_to_status("") == ContainerStatus.PENDING

    def test_unknown_string(self):
        """Unrecognized phase should map to PENDING (default)."""
        assert _pod_phase_to_status("SomeNewPhase") == ContainerStatus.PENDING


class TestParseK8sDatetime:
    """Tests for _parse_k8s_datetime helper."""

    def test_none_returns_none(self):
        """None input should return None."""
        assert _parse_k8s_datetime(None) is None

    def test_datetime_passthrough(self):
        """datetime objects should be returned as-is."""
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _parse_k8s_datetime(dt) is dt

    def test_iso_string(self):
        """ISO format string should be parsed."""
        result = _parse_k8s_datetime("2024-01-15T12:00:00+00:00")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_z_suffix_string(self):
        """String with Z suffix should be parsed."""
        result = _parse_k8s_datetime("2024-01-15T12:00:00Z")
        assert result is not None
        assert result.year == 2024

    def test_invalid_string_returns_none(self):
        """Invalid strings should return None."""
        assert _parse_k8s_datetime("not a date") is None

    def test_non_string_non_datetime(self):
        """Non-string, non-datetime objects should return None on parse failure."""
        assert _parse_k8s_datetime(12345) is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestGetKubernetesClient:
    """Tests for the singleton accessor."""

    def test_singleton_returns_same_instance(self):
        """get_kubernetes_client should return the same instance."""
        import kubernetes_client as kc

        kc._kubernetes_client = None

        # Create with injected APIs to avoid real k8s config
        client = KubernetesClient(
            namespace="test",
            _batch_api=MagicMock(),
            _core_api=MagicMock(),
        )
        kc._kubernetes_client = client

        result = get_kubernetes_client()

        assert result is client

        # Reset for other tests
        kc._kubernetes_client = None

    def test_singleton_creates_on_first_call(self):
        """First call should create a new client instance."""
        import kubernetes_client as kc

        kc._kubernetes_client = None

        with patch("kubernetes_client.KubernetesClient") as MockKC:
            instance = MagicMock()
            MockKC.return_value = instance

            result = get_kubernetes_client(namespace="custom-ns")

            assert result is instance
            MockKC.assert_called_once_with(namespace="custom-ns")

        # Reset
        kc._kubernetes_client = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify label constants and defaults."""

    def test_label_constants(self):
        """Label constants should match expected values."""
        assert LABEL_ORCHESTRATOR == "egg.orchestrator"
        assert LABEL_PIPELINE_ID == "egg.pipeline.id"
        assert LABEL_AGENT_ROLE == "egg.agent.role"
        assert LABEL_CONTAINER_NAME == "egg.container.name"

    def test_job_prefix(self):
        """JOB_PREFIX should match DockerClient naming convention."""
        assert KubernetesClient.JOB_PREFIX == "egg-sandbox-"

    def test_default_sandbox_image(self):
        """DEFAULT_SANDBOX_IMAGE should be egg:latest."""
        assert KubernetesClient.DEFAULT_SANDBOX_IMAGE == "egg:latest"
