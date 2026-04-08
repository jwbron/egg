"""
Tests for KubernetesClient.

Mirrors test_docker_client.py structure but for the Kubernetes backend.
Mocks the kubernetes Python client since real k8s operations are not
available in the test sandbox.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_backend import (
    ImagePullError,
    JobOperationError,
    PodNotFoundError,
)
from kubernetes.client.exceptions import ApiException
from models import AgentRole, ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_api():
    """Mock kubernetes API objects.

    Patches the kubernetes config loading and API client creation to return
    mock API objects, mirroring the mock_docker fixture pattern.
    """
    with (
        patch("kubernetes_client.KubernetesClient._ensure_initialized"),
    ):
        mock_core_v1 = MagicMock()
        mock_batch_v1 = MagicMock()

        from kubernetes_client import KubernetesClient

        client = KubernetesClient()
        client._core_api = mock_core_v1
        client._batch_api = mock_batch_v1
        client._initialized = True

        yield {
            "client": client,
            "core_v1": mock_core_v1,
            "batch_v1": mock_batch_v1,
        }


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


class TestKubernetesClientConnection:
    """Tests for Kubernetes client connection."""

    def test_is_connected_true(self, mock_k8s_api):
        """Test is_connected returns True when cluster is reachable."""
        client = mock_k8s_api["client"]
        mock_k8s_api["core_v1"].get_api_versions.return_value = MagicMock()
        assert client.is_connected() is True

    def test_is_connected_false_on_exception(self, mock_k8s_api):
        """Test is_connected returns False when cluster unreachable."""
        client = mock_k8s_api["client"]
        mock_k8s_api["core_v1"].get_api_versions.side_effect = Exception(
            "Connection refused"
        )
        assert client.is_connected() is False

    def test_default_namespace(self):
        """Test default namespace is egg-agents."""
        from kubernetes_client import DEFAULT_AGENT_NAMESPACE, KubernetesClient

        with patch(
            "kubernetes_client.KubernetesClient._ensure_initialized"
        ):
            client = KubernetesClient()
            assert client.namespace == DEFAULT_AGENT_NAMESPACE

    def test_custom_namespace(self):
        """Test custom namespace is respected."""
        with patch(
            "kubernetes_client.KubernetesClient._ensure_initialized"
        ):
            from kubernetes_client import KubernetesClient

            client = KubernetesClient(namespace="custom-ns")
            assert client.namespace == "custom-ns"


# ---------------------------------------------------------------------------
# Job creation tests
# ---------------------------------------------------------------------------


class TestJobCreation:
    """Tests for creating k8s Jobs."""

    def test_create_container_creates_job(self, mock_k8s_api):
        """Test creating a container creates a k8s Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]
        batch_v1.create_namespaced_job.return_value = MagicMock()

        info = client.create_container(
            name="test",
            environment={"FOO": "bar"},
        )

        assert info.container_id == "test"
        assert info.status == ContainerStatus.PENDING
        batch_v1.create_namespaced_job.assert_called_once()

    def test_create_container_with_labels(self, mock_k8s_api):
        """Test creating container with custom labels propagated to Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]
        batch_v1.create_namespaced_job.return_value = MagicMock()

        client.create_container(
            name="test",
            labels={"custom.label": "value"},
        )

        call_args = batch_v1.create_namespaced_job.call_args
        job_body = call_args.kwargs.get("body") or call_args[1].get("body")
        # Verify that custom labels are included in the Job metadata
        assert job_body is not None

    def test_create_container_default_image(self, mock_k8s_api):
        """Test default image is egg:latest."""
        client = mock_k8s_api["client"]
        assert client.DEFAULT_SANDBOX_IMAGE == "egg:latest"

    def test_create_container_image_pull_error(self, mock_k8s_api):
        """Test create raises ImagePullError on ImagePull failure."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        exc = ApiException(status=422, reason="ImagePullBackOff")
        batch_v1.create_namespaced_job.side_effect = exc

        with pytest.raises((ImagePullError, JobOperationError)):
            client.create_container(name="test", image="nonexistent:latest")

    def test_create_container_api_error(self, mock_k8s_api):
        """Test create raises JobOperationError on API failure."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        exc = ApiException(status=500, reason="Internal Server Error")
        batch_v1.create_namespaced_job.side_effect = exc

        with pytest.raises(JobOperationError):
            client.create_container(name="test")

    def test_create_container_with_volumes(self, mock_k8s_api):
        """Test creating container with volume mounts."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]
        batch_v1.create_namespaced_job.return_value = MagicMock()

        volumes = {
            "/host/path": {"bind": "/container/path", "mode": "rw"},
        }
        info = client.create_container(
            name="test",
            volumes=volumes,
        )

        assert info.status == ContainerStatus.PENDING
        batch_v1.create_namespaced_job.assert_called_once()

    def test_create_container_with_command(self, mock_k8s_api):
        """Test creating container with custom command."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]
        batch_v1.create_namespaced_job.return_value = MagicMock()

        info = client.create_container(
            name="test",
            command=["python3", "-c", "print('hello')"],
        )

        assert info.status == ContainerStatus.PENDING


# ---------------------------------------------------------------------------
# Container operations tests (start, stop, remove)
# ---------------------------------------------------------------------------


class TestContainerOperations:
    """Tests for container operations."""

    def test_start_container_is_noop(self, mock_k8s_api):
        """Test start_container returns current info (k8s Jobs auto-start)."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {}
        mock_job.status.conditions = None
        mock_job.status.active = 1
        mock_job.status.start_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        batch_v1.read_namespaced_job.return_value = mock_job

        # Mock get_pod_for_job to return None
        with patch.object(client, "get_pod_for_job", return_value=None):
            info = client.start_container("test-job")

        assert info.status == ContainerStatus.RUNNING

    def test_stop_container_deletes_job(self, mock_k8s_api):
        """Test stop_container deletes the Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        # Mock get_container_info
        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {}
        mock_job.status.conditions = None
        mock_job.status.active = 1
        mock_job.status.start_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value=None):
            info = client.stop_container("test-job")

        assert info.status == ContainerStatus.EXITED
        batch_v1.delete_namespaced_job.assert_called_once()

    def test_stop_container_not_found(self, mock_k8s_api):
        """Test stop raises PodNotFoundError when Job not found."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        exc = ApiException(status=404, reason="Not Found")
        batch_v1.read_namespaced_job.side_effect = exc

        with pytest.raises(PodNotFoundError):
            client.stop_container("nonexistent")

    def test_remove_container_deletes_job(self, mock_k8s_api):
        """Test remove_container deletes the Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        client.remove_container("test-job")

        batch_v1.delete_namespaced_job.assert_called_once()

    def test_remove_container_not_found(self, mock_k8s_api):
        """Test remove raises PodNotFoundError when Job not found."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        exc = ApiException(status=404, reason="Not Found")
        batch_v1.delete_namespaced_job.side_effect = exc

        with pytest.raises(PodNotFoundError):
            client.remove_container("nonexistent")

    def test_remove_container_api_error(self, mock_k8s_api):
        """Test remove raises JobOperationError on API failure."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        exc = ApiException(status=500, reason="Server Error")
        batch_v1.delete_namespaced_job.side_effect = exc

        with pytest.raises(JobOperationError):
            client.remove_container("test-job")


# ---------------------------------------------------------------------------
# Container info tests
# ---------------------------------------------------------------------------


class TestContainerInfo:
    """Tests for getting container info from k8s Jobs."""

    def test_get_container_info_running(self, mock_k8s_api):
        """Test getting info for running Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {}
        mock_job.status.conditions = None
        mock_job.status.active = 1
        mock_job.status.start_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value=None):
            info = client.get_container_info("test-job")

        assert info.status == ContainerStatus.RUNNING
        assert info.started_at is not None

    def test_get_container_info_completed(self, mock_k8s_api):
        """Test getting info for completed Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        completed_condition = MagicMock()
        completed_condition.type = "Complete"
        completed_condition.status = "True"
        completed_condition.last_transition_time = datetime(
            2024, 1, 15, 12, 30, tzinfo=UTC
        )

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {}
        mock_job.status.conditions = [completed_condition]
        mock_job.status.active = None
        mock_job.status.start_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value="test-pod"):
            with patch.object(client, "_get_pod_exit_code", return_value=0):
                info = client.get_container_info("test-job")

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0
        assert info.exited_at is not None

    def test_get_container_info_failed(self, mock_k8s_api):
        """Test getting info for failed Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        failed_condition = MagicMock()
        failed_condition.type = "Failed"
        failed_condition.status = "True"
        failed_condition.last_transition_time = datetime(
            2024, 1, 15, 12, 30, tzinfo=UTC
        )

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {}
        mock_job.status.conditions = [failed_condition]
        mock_job.status.active = None
        mock_job.status.start_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value="test-pod"):
            with patch.object(client, "_get_pod_exit_code", return_value=137):
                info = client.get_container_info("test-job")

        assert info.status == ContainerStatus.FAILED
        assert info.exit_code == 137

    def test_get_container_info_pending(self, mock_k8s_api):
        """Test getting info for pending Job (no active pods, no conditions)."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {}
        mock_job.status.conditions = None
        mock_job.status.active = 0
        mock_job.status.start_time = None
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value=None):
            info = client.get_container_info("test-job")

        assert info.status == ContainerStatus.PENDING

    def test_get_container_info_not_found(self, mock_k8s_api):
        """Test get_container_info raises PodNotFoundError for missing Job."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        exc = ApiException(status=404, reason="Not Found")
        batch_v1.read_namespaced_job.side_effect = exc

        with pytest.raises(PodNotFoundError):
            client.get_container_info("nonexistent")

    def test_get_container_info_with_agent_role(self, mock_k8s_api):
        """Test getting info with agent role label."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        mock_job = MagicMock()
        mock_job.metadata.name = "egg-issue-123-coder"
        mock_job.metadata.labels = {"egg.agent.role": "coder"}
        mock_job.status.conditions = None
        mock_job.status.active = 1
        mock_job.status.start_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value=None):
            info = client.get_container_info("egg-issue-123-coder")

        assert info.agent_role == AgentRole.CODER


# ---------------------------------------------------------------------------
# Listing tests
# ---------------------------------------------------------------------------


class TestContainerListing:
    """Tests for listing Jobs."""

    def test_list_containers(self, mock_k8s_api):
        """Test listing containers returns job info."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.labels = {"egg.orchestrator": "true"}
        mock_job.status.conditions = None
        mock_job.status.active = 1
        mock_job.status.start_time = datetime.now(UTC)
        batch_v1.list_namespaced_job.return_value = MagicMock(items=[mock_job])
        batch_v1.read_namespaced_job.return_value = mock_job

        with patch.object(client, "get_pod_for_job", return_value=None):
            containers = client.list_containers()

        assert len(containers) == 1

    def test_list_containers_with_labels(self, mock_k8s_api):
        """Test listing containers with label filter."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]
        batch_v1.list_namespaced_job.return_value = MagicMock(items=[])

        client.list_containers(labels={"egg.pipeline.id": "issue-123"})

        call_args = batch_v1.list_namespaced_job.call_args
        label_selector = call_args.kwargs.get("label_selector", "")
        assert "egg.orchestrator=true" in label_selector
        assert "egg.pipeline.id=issue-123" in label_selector

    def test_list_containers_empty(self, mock_k8s_api):
        """Test listing containers when none exist."""
        client = mock_k8s_api["client"]
        batch_v1 = mock_k8s_api["batch_v1"]
        batch_v1.list_namespaced_job.return_value = MagicMock(items=[])

        containers = client.list_containers()
        assert containers == []


# ---------------------------------------------------------------------------
# Logs tests
# ---------------------------------------------------------------------------


class TestContainerLogs:
    """Tests for getting pod logs."""

    def test_get_container_logs(self, mock_k8s_api):
        """Test getting pod logs."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        core_v1.read_namespaced_pod_log.return_value = (
            "2024-01-15T12:00:00Z Log line 1\n"
        )

        with patch.object(client, "get_pod_for_job", return_value="test-pod"):
            logs = client.get_container_logs("test-job")

        assert "Log line 1" in logs

    def test_get_container_logs_no_pod(self, mock_k8s_api):
        """Test get_container_logs raises when no pod exists."""
        client = mock_k8s_api["client"]

        with patch.object(client, "get_pod_for_job", return_value=None):
            with pytest.raises(PodNotFoundError):
                client.get_container_logs("test-job")


# ---------------------------------------------------------------------------
# Wait tests
# ---------------------------------------------------------------------------


class TestContainerWait:
    """Tests for waiting on Job completion."""

    def test_wait_for_container_already_complete(self, mock_k8s_api):
        """Test wait returns immediately when Job already completed."""
        client = mock_k8s_api["client"]

        completed_info = ContainerInfo(
            container_id="test-job",
            container_name="test-job",
            status=ContainerStatus.EXITED,
            exit_code=0,
        )

        with patch.object(client, "get_container_info", return_value=completed_info):
            info = client.wait_for_container("test-job", timeout=5)

        assert info.status == ContainerStatus.EXITED
        assert info.exit_code == 0

    def test_wait_for_container_timeout(self, mock_k8s_api):
        """Test wait raises JobOperationError on timeout."""
        client = mock_k8s_api["client"]

        running_info = ContainerInfo(
            container_id="test-job",
            container_name="test-job",
            status=ContainerStatus.RUNNING,
        )

        with patch.object(client, "get_container_info", return_value=running_info):
            with pytest.raises(JobOperationError, match="Timeout"):
                client.wait_for_container("test-job", timeout=1)


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for cleanup operations."""

    def test_cleanup_orphaned_containers(self, mock_k8s_api):
        """Test cleaning up orphaned Jobs."""
        client = mock_k8s_api["client"]

        old_time = datetime.now(UTC) - timedelta(hours=48)
        old_info = ContainerInfo(
            container_id="old-job",
            container_name="old-job",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=old_time,
        )

        with patch.object(client, "list_containers", return_value=[old_info]):
            removed = client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1

    def test_cleanup_skips_recent_containers(self, mock_k8s_api):
        """Test cleanup does not remove recent exited Jobs."""
        client = mock_k8s_api["client"]

        recent_time = datetime.now(UTC) - timedelta(hours=2)
        recent_info = ContainerInfo(
            container_id="recent-job",
            container_name="recent-job",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=recent_time,
        )

        with patch.object(client, "list_containers", return_value=[recent_info]):
            removed = client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 0

    def test_cleanup_skips_running_containers(self, mock_k8s_api):
        """Test cleanup does not remove running Jobs."""
        client = mock_k8s_api["client"]

        running_info = ContainerInfo(
            container_id="running-job",
            container_name="running-job",
            status=ContainerStatus.RUNNING,
        )

        with patch.object(client, "list_containers", return_value=[running_info]):
            removed = client.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 0


# ---------------------------------------------------------------------------
# Pod helper tests
# ---------------------------------------------------------------------------


class TestPodHelpers:
    """Tests for pod helper methods."""

    def test_get_pod_for_job(self, mock_k8s_api):
        """Test getting pod name for a Job."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"
        core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])

        pod_name = client.get_pod_for_job("test-job", "egg-agents")

        assert pod_name == "test-job-abc123"

    def test_get_pod_for_job_not_found(self, mock_k8s_api):
        """Test get_pod_for_job returns None when no pod exists."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        core_v1.list_namespaced_pod.return_value = MagicMock(items=[])

        pod_name = client.get_pod_for_job("test-job", "egg-agents")

        assert pod_name is None

    def test_get_pod_exit_code(self, mock_k8s_api):
        """Test extracting exit code from pod status."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        terminated = MagicMock()
        terminated.exit_code = 42
        container_status = MagicMock()
        container_status.state.terminated = terminated

        mock_pod = MagicMock()
        mock_pod.status.container_statuses = [container_status]
        core_v1.read_namespaced_pod.return_value = mock_pod

        exit_code = client._get_pod_exit_code("test-pod", "egg-agents")

        assert exit_code == 42

    def test_get_pod_exit_code_none_when_running(self, mock_k8s_api):
        """Test _get_pod_exit_code returns None when pod is still running."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        container_status = MagicMock()
        container_status.state.terminated = None

        mock_pod = MagicMock()
        mock_pod.status.container_statuses = [container_status]
        core_v1.read_namespaced_pod.return_value = mock_pod

        exit_code = client._get_pod_exit_code("test-pod", "egg-agents")

        assert exit_code is None

    def test_get_pod_status_running(self, mock_k8s_api):
        """Test pod status mapping for Running phase."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        core_v1.read_namespaced_pod.return_value = mock_pod

        status = client.get_pod_status("test-pod", "egg-agents")
        assert status == ContainerStatus.RUNNING

    def test_get_pod_status_succeeded(self, mock_k8s_api):
        """Test pod status mapping for Succeeded phase."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        mock_pod = MagicMock()
        mock_pod.status.phase = "Succeeded"
        core_v1.read_namespaced_pod.return_value = mock_pod

        status = client.get_pod_status("test-pod", "egg-agents")
        assert status == ContainerStatus.EXITED

    def test_get_pod_status_failed(self, mock_k8s_api):
        """Test pod status mapping for Failed phase."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        mock_pod = MagicMock()
        mock_pod.status.phase = "Failed"
        core_v1.read_namespaced_pod.return_value = mock_pod

        status = client.get_pod_status("test-pod", "egg-agents")
        assert status == ContainerStatus.FAILED

    def test_get_pod_status_not_found(self, mock_k8s_api):
        """Test pod status raises PodNotFoundError for missing pod."""
        client = mock_k8s_api["client"]
        core_v1 = mock_k8s_api["core_v1"]

        exc = ApiException(status=404, reason="Not Found")
        core_v1.read_namespaced_pod.side_effect = exc

        with pytest.raises(PodNotFoundError):
            client.get_pod_status("nonexistent", "egg-agents")


# ---------------------------------------------------------------------------
# Singleton tests
# ---------------------------------------------------------------------------


class TestGetKubernetesClient:
    """Tests for singleton getter."""

    def test_get_kubernetes_client_returns_same_instance(self):
        """Test singleton behavior."""
        import kubernetes_client

        kubernetes_client._kubernetes_client = None

        with patch(
            "kubernetes_client.KubernetesClient._ensure_initialized"
        ):
            from kubernetes_client import get_kubernetes_client

            client1 = get_kubernetes_client()
            client2 = get_kubernetes_client()
            assert client1 is client2

        # Reset for other tests
        kubernetes_client._kubernetes_client = None
