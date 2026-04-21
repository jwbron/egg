"""
Pytest configuration for orchestrator tests.

Adds orchestrator and shared directories to sys.path so that modules
can be imported with bare names (e.g., ``from models import Pipeline``).
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Project root
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"

for p in (_orchestrator_path, _shared_path):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Import docker before test collection so that test modules using
# sys.modules.setdefault("docker", MagicMock()) don't shadow the real
# package.  This prevents docker_client.NotFound et al. from being
# bound to MagicMock objects (which aren't BaseException subclasses
# and break ``except NotFound`` clauses).
try:
    import docker  # noqa: F401
except ImportError:
    # docker SDK is not installed — create a mock module with real
    # exception classes so that ``except NotFound`` works correctly.
    class _DockerException(Exception):
        """Mock DockerException matching docker SDK hierarchy."""

    class _APIError(_DockerException):
        """Mock APIError."""

    class _NotFound(_APIError):
        """Mock NotFound."""

    class _ImageNotFound(_APIError):
        """Mock ImageNotFound."""

    _errors_mod = types.ModuleType("docker.errors")
    _errors_mod.DockerException = _DockerException  # type: ignore[attr-defined]
    _errors_mod.APIError = _APIError  # type: ignore[attr-defined]
    _errors_mod.NotFound = _NotFound  # type: ignore[attr-defined]
    _errors_mod.ImageNotFound = _ImageNotFound  # type: ignore[attr-defined]

    _docker_mod = MagicMock()
    _docker_mod.errors = _errors_mod
    # Also expose on the MagicMock directly so attribute access works
    _docker_mod.errors.DockerException = _DockerException
    _docker_mod.errors.APIError = _APIError
    _docker_mod.errors.NotFound = _NotFound
    _docker_mod.errors.ImageNotFound = _ImageNotFound

    sys.modules.setdefault("docker", _docker_mod)
    sys.modules.setdefault("docker.errors", _errors_mod)
    sys.modules.setdefault("docker.types", MagicMock())


# Mock the ``kubernetes`` package when it is not installed so that
# kubernetes_client tests can exercise code paths that do
# ``from kubernetes import client as k8s_client``.
try:
    import kubernetes  # noqa: F401
except ImportError:

    class _K8sDataObject:
        """Mock k8s SDK data class that stores kwargs as attributes."""

        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            for k, v in kwargs.items():
                setattr(self, k, v)

        def __repr__(self) -> str:
            attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
            return f"{type(self).__name__}({attrs})"

    # Create named subclasses so repr is informative
    _V1Container = type("V1Container", (_K8sDataObject,), {})
    _V1EnvVar = type("V1EnvVar", (_K8sDataObject,), {})
    _V1PodSpec = type("V1PodSpec", (_K8sDataObject,), {})
    _V1PodTemplateSpec = type("V1PodTemplateSpec", (_K8sDataObject,), {})
    _V1ObjectMeta = type("V1ObjectMeta", (_K8sDataObject,), {})
    _V1JobSpec = type("V1JobSpec", (_K8sDataObject,), {})
    _V1Job = type("V1Job", (_K8sDataObject,), {})
    _V1DeleteOptions = type("V1DeleteOptions", (_K8sDataObject,), {})
    _V1ResourceRequirements = type("V1ResourceRequirements", (_K8sDataObject,), {})

    _k8s_client_mod = types.ModuleType("kubernetes.client")
    _k8s_client_mod.V1Container = _V1Container  # type: ignore[attr-defined]
    _k8s_client_mod.V1EnvVar = _V1EnvVar  # type: ignore[attr-defined]
    _k8s_client_mod.V1PodSpec = _V1PodSpec  # type: ignore[attr-defined]
    _k8s_client_mod.V1PodTemplateSpec = _V1PodTemplateSpec  # type: ignore[attr-defined]
    _k8s_client_mod.V1ObjectMeta = _V1ObjectMeta  # type: ignore[attr-defined]
    _k8s_client_mod.V1JobSpec = _V1JobSpec  # type: ignore[attr-defined]
    _k8s_client_mod.V1Job = _V1Job  # type: ignore[attr-defined]
    _k8s_client_mod.V1DeleteOptions = _V1DeleteOptions  # type: ignore[attr-defined]
    _k8s_client_mod.V1ResourceRequirements = _V1ResourceRequirements  # type: ignore[attr-defined]
    _k8s_client_mod.BatchV1Api = MagicMock  # type: ignore[attr-defined]
    _k8s_client_mod.CoreV1Api = MagicMock  # type: ignore[attr-defined]

    _k8s_config_mod = types.ModuleType("kubernetes.config")
    # Simulate ConfigException for in-cluster config fallback
    _k8s_config_mod.ConfigException = type("ConfigException", (Exception,), {})  # type: ignore[attr-defined]
    _k8s_config_mod.load_incluster_config = MagicMock()  # type: ignore[attr-defined]
    _k8s_config_mod.load_kube_config = MagicMock()  # type: ignore[attr-defined]

    _k8s_mod = types.ModuleType("kubernetes")
    _k8s_mod.client = _k8s_client_mod  # type: ignore[attr-defined]
    _k8s_mod.config = _k8s_config_mod  # type: ignore[attr-defined]

    sys.modules.setdefault("kubernetes", _k8s_mod)
    sys.modules.setdefault("kubernetes.client", _k8s_client_mod)
    sys.modules.setdefault("kubernetes.config", _k8s_config_mod)
