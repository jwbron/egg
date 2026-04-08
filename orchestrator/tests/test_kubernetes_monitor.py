"""
Tests for KubernetesMonitor.

Mirrors test_container_monitor.py structure for Kubernetes Job state tracking.
Tests event emission, state reconciliation, and cleanup.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from container_backend import (
    KubernetesClientError,
    PodNotFoundError,
)
from container_monitor import ContainerEvent
from models import ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_container_info(
    container_id: str = "test-job",
    status: ContainerStatus = ContainerStatus.RUNNING,
    exit_code: int | None = None,
) -> ContainerInfo:
    """Create a ContainerInfo for testing."""
    return ContainerInfo(
        container_id=container_id,
        container_name=container_id,
        status=status,
        exit_code=exit_code,
        started_at=datetime.now(UTC) if status == ContainerStatus.RUNNING else None,
        exited_at=datetime.now(UTC) if status in (ContainerStatus.EXITED, ContainerStatus.FAILED) else None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_k8s_client():
    """Mock KubernetesClient for monitor tests."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.list_containers.return_value = []
    mock.cleanup_orphaned_containers.return_value = 0
    return mock


@pytest.fixture
def monitor(mock_k8s_client):
    """Create a KubernetesMonitor with mocked backend."""
    from kubernetes_monitor import KubernetesMonitor

    return KubernetesMonitor(k8s_client=mock_k8s_client, check_interval=1)


# ---------------------------------------------------------------------------
# Event emission tests
# ---------------------------------------------------------------------------


class TestEventEmission:
    """Tests for Job event emission."""

    def test_emits_started_event(self, monitor, mock_k8s_client):
        """Test monitor emits STARTED event for new running Job."""
        handler = MagicMock()
        monitor.add_handler(handler)

        running_job = _make_container_info(status=ContainerStatus.RUNNING)
        mock_k8s_client.list_containers.return_value = [running_job]

        monitor._check_all_jobs()

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == ContainerEvent.STARTED

    def test_emits_stopped_event_on_clean_exit(self, monitor, mock_k8s_client):
        """Test monitor emits STOPPED event for exit code 0."""
        handler = MagicMock()
        monitor.add_handler(handler)

        # First set running state
        running_job = _make_container_info(status=ContainerStatus.RUNNING)
        mock_k8s_client.list_containers.return_value = [running_job]
        monitor._check_all_jobs()
        handler.reset_mock()

        # Then transition to exited
        exited_job = _make_container_info(
            status=ContainerStatus.EXITED, exit_code=0
        )
        mock_k8s_client.list_containers.return_value = [exited_job]
        monitor._check_all_jobs()

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == ContainerEvent.STOPPED

    def test_emits_failed_event_on_nonzero_exit(self, monitor, mock_k8s_client):
        """Test monitor emits FAILED event for non-zero exit code."""
        handler = MagicMock()
        monitor.add_handler(handler)

        # First set running state
        running_job = _make_container_info(status=ContainerStatus.RUNNING)
        mock_k8s_client.list_containers.return_value = [running_job]
        monitor._check_all_jobs()
        handler.reset_mock()

        # Then transition to exited with error
        failed_job = _make_container_info(
            status=ContainerStatus.EXITED, exit_code=1
        )
        mock_k8s_client.list_containers.return_value = [failed_job]
        monitor._check_all_jobs()

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == ContainerEvent.FAILED

    def test_emits_failed_event_on_failed_status(self, monitor, mock_k8s_client):
        """Test monitor emits FAILED event for FAILED status."""
        handler = MagicMock()
        monitor.add_handler(handler)

        # First set running state
        running_job = _make_container_info(status=ContainerStatus.RUNNING)
        mock_k8s_client.list_containers.return_value = [running_job]
        monitor._check_all_jobs()
        handler.reset_mock()

        # Then transition to failed
        failed_job = _make_container_info(status=ContainerStatus.FAILED)
        mock_k8s_client.list_containers.return_value = [failed_job]
        monitor._check_all_jobs()

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert event.event_type == ContainerEvent.FAILED

    def test_no_event_for_unchanged_status(self, monitor, mock_k8s_client):
        """Test no event emitted when status unchanged."""
        handler = MagicMock()
        monitor.add_handler(handler)

        running_job = _make_container_info(status=ContainerStatus.RUNNING)
        mock_k8s_client.list_containers.return_value = [running_job]

        # First check: should emit STARTED
        monitor._check_all_jobs()
        assert handler.call_count == 1
        handler.reset_mock()

        # Second check: same status, no event
        monitor._check_all_jobs()
        handler.assert_not_called()


# ---------------------------------------------------------------------------
# Handler management tests
# ---------------------------------------------------------------------------


class TestHandlerManagement:
    """Tests for adding/removing event handlers."""

    def test_add_handler(self, monitor):
        """Test adding an event handler."""
        handler = MagicMock()
        monitor.add_handler(handler)
        assert handler in monitor._handlers

    def test_remove_handler(self, monitor):
        """Test removing an event handler."""
        handler = MagicMock()
        monitor.add_handler(handler)
        monitor.remove_handler(handler)
        assert handler not in monitor._handlers

    def test_handler_error_doesnt_crash(self, monitor, mock_k8s_client):
        """Test handler errors are caught and don't stop monitoring."""
        bad_handler = MagicMock(side_effect=Exception("handler error"))
        good_handler = MagicMock()
        monitor.add_handler(bad_handler)
        monitor.add_handler(good_handler)

        running_job = _make_container_info(status=ContainerStatus.RUNNING)
        mock_k8s_client.list_containers.return_value = [running_job]

        monitor._check_all_jobs()

        bad_handler.assert_called_once()
        good_handler.assert_called_once()


# ---------------------------------------------------------------------------
# State tracking tests
# ---------------------------------------------------------------------------


class TestStateTracking:
    """Tests for Job state tracking."""

    def test_tracks_job_states(self, monitor, mock_k8s_client):
        """Test monitor caches Job states."""
        running_job = _make_container_info(
            container_id="job-1", status=ContainerStatus.RUNNING
        )
        mock_k8s_client.list_containers.return_value = [running_job]

        monitor._check_all_jobs()

        assert monitor.get_container_status("job-1") == ContainerStatus.RUNNING

    def test_removes_deleted_jobs(self, monitor, mock_k8s_client):
        """Test monitor removes state for deleted Jobs."""
        running_job = _make_container_info(
            container_id="job-1", status=ContainerStatus.RUNNING
        )
        mock_k8s_client.list_containers.return_value = [running_job]
        monitor._check_all_jobs()

        # Job disappears
        mock_k8s_client.list_containers.return_value = []
        monitor._check_all_jobs()

        assert monitor.get_container_status("job-1") is None

    def test_get_container_status_unknown(self, monitor):
        """Test get_container_status returns None for unknown Jobs."""
        assert monitor.get_container_status("unknown-job") is None


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for Job health checking."""

    def test_check_healthy_job(self, monitor, mock_k8s_client):
        """Test health check for running Job."""
        mock_k8s_client.get_container_info.return_value = _make_container_info(
            status=ContainerStatus.RUNNING
        )

        health = monitor.check_container_health("test-job")

        assert health["healthy"] is True
        assert health["status"] == "running"

    def test_check_unhealthy_job(self, monitor, mock_k8s_client):
        """Test health check for failed Job."""
        mock_k8s_client.get_container_info.return_value = _make_container_info(
            status=ContainerStatus.FAILED
        )

        health = monitor.check_container_health("test-job")

        assert health["healthy"] is False
        assert health["status"] == "failed"

    def test_check_missing_job(self, monitor, mock_k8s_client):
        """Test health check for missing Job."""
        mock_k8s_client.get_container_info.side_effect = PodNotFoundError(
            "Job not found"
        )

        health = monitor.check_container_health("test-job")

        assert health["healthy"] is False
        assert health["status"] == "not_found"


# ---------------------------------------------------------------------------
# Orphan cleanup tests
# ---------------------------------------------------------------------------


class TestOrphanCleanup:
    """Tests for orphaned Job cleanup."""

    def test_cleanup_delegates_to_client(self, monitor, mock_k8s_client):
        """Test cleanup delegates to k8s client."""
        mock_k8s_client.cleanup_orphaned_containers.return_value = 3

        removed = monitor._cleanup_orphaned()

        assert removed == 3
        mock_k8s_client.cleanup_orphaned_containers.assert_called_once()

    def test_cleanup_handles_error(self, monitor, mock_k8s_client):
        """Test cleanup handles errors gracefully."""
        mock_k8s_client.cleanup_orphaned_containers.side_effect = Exception(
            "API error"
        )

        removed = monitor._cleanup_orphaned()

        assert removed == 0


# ---------------------------------------------------------------------------
# Start/Stop tests
# ---------------------------------------------------------------------------


class TestMonitorLifecycle:
    """Tests for monitor start/stop."""

    def test_start_sets_running(self, monitor):
        """Test start sets running flag."""
        monitor.start()
        assert monitor.is_running() is True
        monitor.stop()

    def test_stop_clears_running(self, monitor):
        """Test stop clears running flag."""
        monitor.start()
        monitor.stop()
        assert monitor.is_running() is False

    def test_stop_joins_thread(self, monitor):
        """Test stop waits for monitor thread."""
        monitor.start()
        assert monitor._thread is not None
        monitor.stop()
        assert monitor._thread is None

    def test_double_start_is_noop(self, monitor):
        """Test starting twice doesn't create extra threads."""
        monitor.start()
        thread1 = monitor._thread
        monitor.start()
        thread2 = monitor._thread
        assert thread1 is thread2
        monitor.stop()


# ---------------------------------------------------------------------------
# Job exit code retrieval tests
# ---------------------------------------------------------------------------


class TestJobExitCode:
    """Tests for _get_job_exit_code helper."""

    def test_returns_exit_code(self, monitor, mock_k8s_client):
        """Test returns exit code from k8s API."""
        mock_k8s_client.get_container_info.return_value = _make_container_info(
            status=ContainerStatus.EXITED, exit_code=42
        )

        code = monitor._get_job_exit_code("test-job")
        assert code == 42

    def test_returns_none_on_error(self, monitor, mock_k8s_client):
        """Test returns None when k8s API fails."""
        mock_k8s_client.get_container_info.side_effect = KubernetesClientError(
            "API error"
        )

        code = monitor._get_job_exit_code("test-job")
        assert code is None
