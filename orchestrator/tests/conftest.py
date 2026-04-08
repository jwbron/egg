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

# ---- Kubernetes mock setup ----
# Same pattern as Docker: create a mock with real exception classes
# so that ``except ApiException`` works correctly in tests.
try:
    import kubernetes  # noqa: F401
except ImportError:
    class _ApiException(Exception):
        """Mock ApiException matching kubernetes SDK."""

        def __init__(self, status=None, reason=None, http_resp=None):
            self.status = status
            self.reason = reason or ""
            self.http_resp = http_resp
            super().__init__(f"({status}) Reason: {reason}")

    class _ConfigException(Exception):
        """Mock ConfigException."""

    _k8s_exceptions_mod = types.ModuleType("kubernetes.client.exceptions")
    _k8s_exceptions_mod.ApiException = _ApiException  # type: ignore[attr-defined]

    _k8s_config_mod = types.ModuleType("kubernetes.config")
    _k8s_config_mod.ConfigException = _ConfigException  # type: ignore[attr-defined]
    _k8s_config_mod.load_incluster_config = MagicMock(side_effect=_ConfigException)  # type: ignore[attr-defined]
    _k8s_config_mod.load_kube_config = MagicMock()  # type: ignore[attr-defined]

    _k8s_client_mod = MagicMock()
    _k8s_client_mod.exceptions = _k8s_exceptions_mod
    _k8s_client_mod.exceptions.ApiException = _ApiException

    _k8s_mod = MagicMock()
    _k8s_mod.client = _k8s_client_mod
    _k8s_mod.config = _k8s_config_mod

    sys.modules.setdefault("kubernetes", _k8s_mod)
    sys.modules.setdefault("kubernetes.client", _k8s_client_mod)
    sys.modules.setdefault("kubernetes.client.exceptions", _k8s_exceptions_mod)
    sys.modules.setdefault("kubernetes.config", _k8s_config_mod)
