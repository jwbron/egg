"""
Tests for the ContainerBackend protocol.

Verifies protocol conformance for both DockerClient and KubernetesClient,
exception hierarchy, and runtime-checkable behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from container_backend import ContainerBackend
from kubernetes_client import (
    ImagePullError,
    JobOperationError,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
)
from models import ContainerInfo, ContainerStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_k8s_client() -> KubernetesClient:
    """Create a KubernetesClient with mock API backends."""
    return KubernetesClient(
        namespace="test-ns",
        _batch_api=MagicMock(),
        _core_api=MagicMock(),
    )


class _MinimalBackend:
    """Minimal class that satisfies the ContainerBackend protocol."""

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
        return ContainerInfo(container_id="id", container_name="name")

    def start_container(self, container_id: str) -> ContainerInfo:
        return ContainerInfo(container_id=container_id, container_name="name")

    def stop_container(self, container_id: str, timeout: int = 10) -> ContainerInfo:
        return ContainerInfo(container_id=container_id, container_name="name")

    def remove_container(self, container_id: str, force: bool = False, v: bool = True) -> None:
        pass

    def get_container_info(self, container_id: str) -> ContainerInfo:
        return ContainerInfo(container_id=container_id, container_name="name")

    def list_containers(
        self, all: bool = True, labels: dict[str, str] | None = None
    ) -> list[ContainerInfo]:
        return []

    def get_container_logs(
        self, container_id: str, tail: int = 100, since: datetime | None = None
    ) -> str:
        return ""

    def wait_for_container(self, container_id: str, timeout: int = 300) -> ContainerInfo:
        return ContainerInfo(container_id=container_id, container_name="name")

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int:
        return 0

    def is_connected(self) -> bool:
        return True


class _IncompleteBackend:
    """A class that does NOT satisfy the ContainerBackend protocol — missing methods."""

    def is_connected(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify that concrete implementations satisfy ContainerBackend."""

    def test_kubernetes_client_is_container_backend(self):
        """KubernetesClient must be an instance of ContainerBackend."""
        client = _make_k8s_client()
        assert isinstance(client, ContainerBackend)

    def test_docker_client_is_container_backend(self):
        """DockerClient (alias for KubernetesClient) must be an instance of ContainerBackend."""
        from docker_client import DockerClient

        # DockerClient is now aliased to KubernetesClient after the k8s migration
        client = DockerClient(
            namespace="test-ns",
            _batch_api=MagicMock(),
            _core_api=MagicMock(),
        )
        assert isinstance(client, ContainerBackend)

    def test_minimal_backend_satisfies_protocol(self):
        """A minimal class with all methods should satisfy the protocol."""
        backend = _MinimalBackend()
        assert isinstance(backend, ContainerBackend)

    def test_incomplete_backend_fails_protocol(self):
        """A class missing required methods must NOT satisfy the protocol."""
        backend = _IncompleteBackend()
        assert not isinstance(backend, ContainerBackend)

    def test_protocol_is_runtime_checkable(self):
        """ContainerBackend must be a runtime-checkable Protocol."""
        # isinstance() must work — this proves @runtime_checkable is applied
        assert isinstance(_MinimalBackend(), ContainerBackend)

    def test_protocol_has_all_expected_methods(self):
        """The protocol defines the complete expected interface."""
        expected_methods = {
            "create_container",
            "start_container",
            "stop_container",
            "remove_container",
            "get_container_info",
            "list_containers",
            "get_container_logs",
            "wait_for_container",
            "cleanup_orphaned_containers",
            "is_connected",
        }
        # Collect methods defined on the Protocol (excluding dunder methods)
        protocol_methods = {
            name
            for name in dir(ContainerBackend)
            if not name.startswith("_") and callable(getattr(ContainerBackend, name, None))
        }
        assert expected_methods.issubset(protocol_methods)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Verify the Kubernetes exception class hierarchy."""

    def test_pod_not_found_is_kubernetes_error(self):
        """PodNotFoundError must inherit from KubernetesClientError."""
        assert issubclass(PodNotFoundError, KubernetesClientError)

    def test_job_operation_is_kubernetes_error(self):
        """JobOperationError must inherit from KubernetesClientError."""
        assert issubclass(JobOperationError, KubernetesClientError)

    def test_image_pull_is_kubernetes_error(self):
        """ImagePullError must inherit from KubernetesClientError."""
        assert issubclass(ImagePullError, KubernetesClientError)

    def test_kubernetes_error_is_exception(self):
        """KubernetesClientError must be a standard Exception."""
        assert issubclass(KubernetesClientError, Exception)

    def test_exceptions_are_raisable(self):
        """All custom exceptions must be raisable and catchable."""
        for exc_cls in (KubernetesClientError, PodNotFoundError, JobOperationError, ImagePullError):
            with pytest.raises(exc_cls):
                raise exc_cls(f"test {exc_cls.__name__}")

    def test_catch_base_catches_subclasses(self):
        """Catching KubernetesClientError must catch all subclasses."""
        for exc_cls in (PodNotFoundError, JobOperationError, ImagePullError):
            with pytest.raises(KubernetesClientError):
                raise exc_cls("caught via base")

    def test_exception_message_preserved(self):
        """Exception messages must be preserved."""
        msg = "Something went wrong"
        exc = PodNotFoundError(msg)
        assert str(exc) == msg


# ---------------------------------------------------------------------------
# ContainerInfo k8s fields
# ---------------------------------------------------------------------------


class TestContainerInfoKubernetesFields:
    """Verify the new k8s-specific fields on ContainerInfo."""

    def test_default_k8s_fields_are_none(self):
        """Kubernetes fields default to None for backwards compatibility."""
        info = ContainerInfo(container_id="abc", container_name="test")
        assert info.pod_name is None
        assert info.namespace is None
        assert info.job_name is None

    def test_k8s_fields_can_be_set(self):
        """Kubernetes fields can be populated."""
        info = ContainerInfo(
            container_id="uid-123",
            container_name="egg-sandbox-test",
            namespace="egg-agents",
            pod_name="egg-sandbox-test-abc12",
            job_name="egg-sandbox-test",
        )
        assert info.namespace == "egg-agents"
        assert info.pod_name == "egg-sandbox-test-abc12"
        assert info.job_name == "egg-sandbox-test"

    def test_k8s_fields_serialisation(self):
        """Kubernetes fields must survive serialization round-trip."""
        info = ContainerInfo(
            container_id="uid-123",
            container_name="egg-sandbox-test",
            namespace="egg-agents",
            pod_name="pod-xyz",
            job_name="egg-sandbox-test",
            status=ContainerStatus.RUNNING,
            started_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        data = info.model_dump()
        restored = ContainerInfo(**data)
        assert restored.namespace == "egg-agents"
        assert restored.pod_name == "pod-xyz"
        assert restored.job_name == "egg-sandbox-test"
        assert restored.status == ContainerStatus.RUNNING
