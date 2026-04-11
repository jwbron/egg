"""
Tests for docker_client backward-compatibility shim.

Verifies that the Docker-named aliases resolve to their Kubernetes
equivalents and that the shim-specific ``_validate_container_id``
function works correctly.

The underlying ``KubernetesClient`` behaviour is tested exhaustively
in ``test_kubernetes_client.py``.
"""

import pytest
from docker_client import (
    ContainerNotFoundError,
    ContainerOperationError,
    DockerClient,
    DockerClientError,
    ImageNotFoundError,
    InvalidContainerIdError,
    _validate_container_id,
    get_docker_client,
)
from kubernetes_client import (
    ImagePullError,
    JobOperationError,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
)

# ---------------------------------------------------------------------------
# Alias identity tests
# ---------------------------------------------------------------------------


class TestShimAliases:
    """Verify the shim re-exports map to the correct Kubernetes types."""

    def test_docker_client_is_kubernetes_client(self):
        assert DockerClient is KubernetesClient

    def test_docker_client_error_is_kubernetes_client_error(self):
        assert DockerClientError is KubernetesClientError

    def test_container_not_found_error_is_pod_not_found_error(self):
        assert ContainerNotFoundError is PodNotFoundError

    def test_container_operation_error_is_job_operation_error(self):
        assert ContainerOperationError is JobOperationError

    def test_image_not_found_error_is_image_pull_error(self):
        assert ImageNotFoundError is ImagePullError

    def test_invalid_container_id_error_is_kubernetes_client_error(self):
        assert InvalidContainerIdError is KubernetesClientError

    def test_get_docker_client_delegates(self):
        """get_docker_client() returns a KubernetesClient instance."""
        from unittest.mock import MagicMock

        # Reset singletons so this test is isolated
        import kubernetes_client

        old_k8s = kubernetes_client._kubernetes_client
        kubernetes_client._kubernetes_client = None

        try:
            # Inject mock APIs so __init__ skips real kube-config loading
            kubernetes_client._kubernetes_client = KubernetesClient(
                _batch_api=MagicMock(), _core_api=MagicMock()
            )
            client = get_docker_client()
            assert isinstance(client, KubernetesClient)
        finally:
            kubernetes_client._kubernetes_client = old_k8s


# ---------------------------------------------------------------------------
# _validate_container_id
# ---------------------------------------------------------------------------


class TestContainerIdValidation:
    """Tests for container ID validation."""

    def test_valid_full_container_id(self):
        """Test valid 64-character hex container ID."""
        valid_id = "abc123def456" * 5 + "abcd"  # 64 hex chars
        _validate_container_id(valid_id)  # Should not raise

    def test_valid_short_container_id(self):
        """Test valid 12-character short container ID."""
        _validate_container_id("abc123def456")  # Should not raise

    def test_valid_container_name(self):
        """Test valid container name."""
        _validate_container_id("egg-sandbox-test")  # Should not raise
        _validate_container_id("my_container.name")  # Should not raise
        _validate_container_id("container123")  # Should not raise

    def test_invalid_empty_id(self):
        """Test empty container ID raises error."""
        with pytest.raises(InvalidContainerIdError) as exc_info:
            _validate_container_id("")
        assert "must not be empty or None" in str(exc_info.value)

    def test_invalid_none_id(self):
        """Test None container ID raises error."""
        with pytest.raises(InvalidContainerIdError) as exc_info:
            _validate_container_id(None)  # type: ignore
        assert "must not be empty or None" in str(exc_info.value)

    def test_invalid_special_characters(self):
        """Test container ID with invalid special characters."""
        with pytest.raises(InvalidContainerIdError):
            _validate_container_id("abc;rm -rf /")

    def test_invalid_path_traversal(self):
        """Test container ID with path traversal attempt."""
        with pytest.raises(InvalidContainerIdError):
            _validate_container_id("../../../etc/passwd")

    def test_invalid_command_injection(self):
        """Test container ID with command injection attempt."""
        with pytest.raises(InvalidContainerIdError):
            _validate_container_id("abc$(whoami)")

    def test_invalid_name_starting_with_hyphen(self):
        """Test container name starting with hyphen is invalid."""
        with pytest.raises(InvalidContainerIdError):
            _validate_container_id("-invalid-name")

    def test_invalid_name_starting_with_period(self):
        """Test container name starting with period is invalid."""
        with pytest.raises(InvalidContainerIdError):
            _validate_container_id(".invalid-name")

    def test_invalid_whitespace(self):
        """Test container ID with whitespace is invalid."""
        with pytest.raises(InvalidContainerIdError):
            _validate_container_id("abc 123")
