"""
Tests for ContainerBackend protocol.

Verifies that:
- ContainerBackend is a proper Protocol (structural typing)
- Both DockerClient and KubernetesClient expose the required methods
- Exception hierarchy is properly defined
"""

import pytest
from container_backend import (
    ContainerBackend,
    ContainerBackendError,
    ImagePullError,
    JobOperationError,
    KubernetesClientError,
    PodNotFoundError,
)


class TestContainerBackendProtocol:
    """Tests for ContainerBackend protocol definition."""

    def test_protocol_has_required_methods(self):
        """Test that ContainerBackend protocol defines expected methods."""
        required_methods = [
            "is_connected",
            "create_container",
            "start_container",
            "stop_container",
            "remove_container",
            "get_container_info",
            "list_containers",
            "get_container_logs",
            "wait_for_container",
            "cleanup_orphaned_containers",
        ]

        for method_name in required_methods:
            assert hasattr(ContainerBackend, method_name), (
                f"ContainerBackend protocol missing method: {method_name}"
            )

    def test_protocol_is_runtime_checkable(self):
        """Test that ContainerBackend can be used with isinstance checks."""
        # @runtime_checkable protocols set _is_runtime_protocol
        assert getattr(ContainerBackend, "_is_runtime_protocol", False), (
            "ContainerBackend should be @runtime_checkable"
        )


class TestExceptionHierarchy:
    """Tests for the container backend exception hierarchy."""

    def test_kubernetes_client_error_is_backend_error(self):
        """Test KubernetesClientError extends ContainerBackendError."""
        assert issubclass(KubernetesClientError, ContainerBackendError)

    def test_pod_not_found_is_kubernetes_error(self):
        """Test PodNotFoundError extends KubernetesClientError."""
        assert issubclass(PodNotFoundError, KubernetesClientError)

    def test_job_operation_error_is_kubernetes_error(self):
        """Test JobOperationError extends KubernetesClientError."""
        assert issubclass(JobOperationError, KubernetesClientError)

    def test_image_pull_error_is_kubernetes_error(self):
        """Test ImagePullError extends KubernetesClientError."""
        assert issubclass(ImagePullError, KubernetesClientError)

    def test_exceptions_are_catchable(self):
        """Test that all exceptions can be raised and caught."""
        with pytest.raises(ContainerBackendError):
            raise PodNotFoundError("test pod not found")

        with pytest.raises(ContainerBackendError):
            raise JobOperationError("test job op failed")

        with pytest.raises(ContainerBackendError):
            raise ImagePullError("test image pull failed")


class TestDockerClientConformance:
    """Tests that DockerClient structurally conforms to ContainerBackend."""

    def test_docker_client_has_all_protocol_methods(self):
        """Test DockerClient has all ContainerBackend methods."""
        from docker_client import DockerClient

        protocol_methods = [
            "is_connected",
            "create_container",
            "start_container",
            "stop_container",
            "remove_container",
            "get_container_info",
            "list_containers",
            "get_container_logs",
            "wait_for_container",
            "cleanup_orphaned_containers",
        ]
        for method_name in protocol_methods:
            assert hasattr(DockerClient, method_name), (
                f"DockerClient missing ContainerBackend method: {method_name}"
            )


class TestKubernetesClientConformance:
    """Tests that KubernetesClient structurally conforms to ContainerBackend."""

    def test_kubernetes_client_has_all_protocol_methods(self):
        """Test KubernetesClient has all ContainerBackend methods."""
        from kubernetes_client import KubernetesClient

        protocol_methods = [
            "is_connected",
            "create_container",
            "start_container",
            "stop_container",
            "remove_container",
            "get_container_info",
            "list_containers",
            "get_container_logs",
            "wait_for_container",
            "cleanup_orphaned_containers",
        ]
        for method_name in protocol_methods:
            assert hasattr(KubernetesClient, method_name), (
                f"KubernetesClient missing ContainerBackend method: {method_name}"
            )
