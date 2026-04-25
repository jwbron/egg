"""
Pytest configuration for orchestrator tests.

Adds orchestrator and shared directories to sys.path so that modules
can be imported with bare names (e.g., ``from models import Pipeline``).
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The auth decorator added for #1769 rejects any request without a valid
# ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>`` header, so endpoint
# tests need both a known secret and a way to attach the header. See the
# FlaskClient patch and session-scoped fixture below.
TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-egg1769"

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


# Auto-attach the lifecycle bearer token to every FlaskClient request so
# existing orchestrator tests that don't know about auth continue to
# exercise the happy path. Auth-specific tests pass ``Authorization`` or
# ``_lifecycle_auth=False`` explicitly to override.
#
# This is applied via an autouse fixture (below) rather than a module-level
# monkey-patch so that it does NOT leak into gateway or other test suites
# that share the same pytest session.
def _flask_open_with_auth(original_open, secret):  # type: ignore[no-untyped-def]
    """Build a wrapper around FlaskClient.open that injects lifecycle auth."""

    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.pop("_lifecycle_auth", True):
            headers = kwargs.get("headers")
            # Werkzeug accepts dict, list[tuple], or Headers — normalize to dict
            # for a simple "is Authorization already set?" check.
            if headers is None:
                kwargs["headers"] = {
                    "Authorization": f"Bearer {secret}",
                }
            else:
                existing = {}
                if hasattr(headers, "items"):
                    existing = dict(headers.items())
                elif isinstance(headers, (list, tuple)):
                    existing = dict(headers)
                if not any(k.lower() == "authorization" for k in existing):
                    if isinstance(headers, dict):
                        headers = {**headers, "Authorization": f"Bearer {secret}"}
                    else:
                        headers = list(headers) + [("Authorization", f"Bearer {secret}")]
                    kwargs["headers"] = headers
        return original_open(self, *args, **kwargs)

    return wrapper


@pytest.fixture(autouse=True, scope="session")
def _set_lifecycle_secret_env():
    """Set EGG_LIFECYCLE_SECRET for the orchestrator test session.

    Uses a session-scoped fixture instead of module-level mutation so
    the env var doesn't leak into other test suites that share the same
    pytest process (e.g., sandbox tests that exercise orch_client.py).
    """
    prev = os.environ.get("EGG_LIFECYCLE_SECRET")
    os.environ["EGG_LIFECYCLE_SECRET"] = TEST_LIFECYCLE_SECRET
    yield
    if prev is None:
        os.environ.pop("EGG_LIFECYCLE_SECRET", None)
    else:
        os.environ["EGG_LIFECYCLE_SECRET"] = prev


@pytest.fixture(autouse=True)
def _inject_lifecycle_auth(monkeypatch):
    """Patch FlaskClient.open for the duration of each orchestrator test."""
    try:
        from flask.testing import FlaskClient  # type: ignore[import-not-found]
    except ImportError:
        yield
        return

    wrapper = _flask_open_with_auth(FlaskClient.open, TEST_LIFECYCLE_SECRET)
    monkeypatch.setattr(FlaskClient, "open", wrapper)
    yield


@pytest.fixture(autouse=True)
def _skip_worktree_disk_check(monkeypatch):
    """Bypass the spawner's spawn-time worktree-existence checks (#1869).

    ``KubernetesSpawner.spawn_agent_job`` now verifies both (a) that the
    per-agent worktree exists on disk after ``create_worktrees`` returns
    and (b) that producer-class roles were not spawned with ``repos=[]``.
    In unit tests the gateway is mocked, so no real worktree is ever
    created and many pre-existing tests spawn producers with empty
    ``repos`` purely to exercise other spawn mechanics — the checks
    would trip on those tests.

    Stub both checks with a monkey-patch on the class so tests see the
    pre-#1869 behavior by default.  Tests specifically exercising the
    #1869 scenarios re-patch these to the real behavior.
    """
    try:
        import kubernetes_spawner
        from kubernetes_spawner import KubernetesSpawner
    except ImportError:
        yield
        return

    monkeypatch.setattr(KubernetesSpawner, "_find_missing_worktrees", lambda self, *args, **kw: [])
    monkeypatch.setattr(kubernetes_spawner, "_role_needs_worktree", lambda role: False)
    yield


@pytest.fixture(autouse=True)
def _reset_heartbeat_coordinator():
    """Reset the heartbeat coordinator singleton between every orchestrator test.

    The coordinator carries per-(pipeline, role) dedup, rate-limit, and
    gateway-fan-out throttle state. Without resetting it, tests that
    share role names can observe contaminated state across files. As
    new coordinator surfaces are added, this single fixture covers them
    all — see #2076 NB4 (the throttle was the third axis after dedup
    and rate-limit, and the fixture had drifted across two test files).
    """
    try:
        from heartbeat import reset_heartbeat_coordinator
    except ImportError:
        yield
        return
    reset_heartbeat_coordinator()
    yield
    reset_heartbeat_coordinator()


@pytest.fixture
def lifecycle_secret() -> str:
    """The shared test bearer token. Useful for building explicit headers."""
    return TEST_LIFECYCLE_SECRET


@pytest.fixture
def lifecycle_auth_headers() -> dict[str, str]:
    """Valid Authorization header for lifecycle-control endpoints."""
    return {"Authorization": f"Bearer {TEST_LIFECYCLE_SECRET}"}
