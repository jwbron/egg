"""Conftest for babysit-pr integration tests.

After #1748 the legacy ``shared/egg_babysit`` package is removed. The
babysit-pr workflow now lives as:

- The ``babysit_pr`` MCP tool in ``orchestrator.mcp_tools`` and the
  user-facing ``skills/babysit-pr/SKILL.md`` skill file.
- The ``POST /api/v1/pipelines`` route in ``orchestrator.routes.pipelines``
  which accepts ``mode=babysit`` and creates an implement-phase pipeline
  with ``has_contract=False``.
- The BRC (Broadcast-Review-Converge) consensus machinery in
  ``orchestrator.concurrent_executor``.

These integration tests exercise those surfaces end-to-end via the HTTP
route + MCP tool contract, with subprocess calls mocked.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure orchestrator/ and shared/ are importable so the MCP tool module
# and route handler can be loaded without installing the package.
_repo_root = Path(__file__).resolve().parent.parent.parent
for _dir in ("orchestrator", "shared"):
    _p = str(_repo_root / _dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The orchestrator route handler imports ``docker`` at module load time;
# stub it out so these tests run in environments without the SDK installed.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())


# `routes.pipelines` lifecycle endpoints are gated by
# `require_lifecycle_secret` (#1769) — they 503 unless
# `EGG_LIFECYCLE_SECRET` is set in the orchestrator's process env AND
# the request carries a matching `Authorization: Bearer …` header. The
# in-process Flask test client used by `test_pipeline.py` /
# `test_escalation.py` does neither by default. Mirror the autouse
# fixtures from `orchestrator/tests/conftest.py` (where unit tests for
# the same blueprint already pass).
_TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-babysit-integration"


@pytest.fixture(autouse=True, scope="session")
def _set_lifecycle_secret_env():
    """Set `EGG_LIFECYCLE_SECRET` for the babysit-integration session.

    Session-scoped so the env var is restored after the subtree
    finishes — prevents leaking a test secret into other integration
    suites that hit live gateway/orchestrator pods (those read the
    secret at pod start and don't care about the test-process env).

    Also disables the gateway-readiness gate
    (`EGG_GATEWAY_READY_TIMEOUT_SECONDS=0`): `routes.pipelines`
    `create_pipeline` waits up to 60s for the gateway HTTP listener
    before accepting the request (#1851), but the in-process Flask
    client tests run without a live gateway, so the wait would always
    time out.
    """
    overrides = {
        "EGG_LIFECYCLE_SECRET": _TEST_LIFECYCLE_SECRET,
        "EGG_GATEWAY_READY_TIMEOUT_SECONDS": "0",
    }
    prev = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _inject_lifecycle_auth(monkeypatch):
    """Auto-attach `Authorization: Bearer …` to every FlaskClient request."""
    try:
        from flask.testing import FlaskClient
    except ImportError:
        yield
        return

    original_open = FlaskClient.open

    def wrapper(self, *args, **kwargs):
        if kwargs.pop("_lifecycle_auth", True):
            headers = kwargs.get("headers")
            auth_header = ("Authorization", f"Bearer {_TEST_LIFECYCLE_SECRET}")
            if headers is None:
                kwargs["headers"] = dict([auth_header])
            else:
                existing: dict[str, object] = {}
                if hasattr(headers, "items"):
                    existing = dict(headers.items())
                elif isinstance(headers, (list, tuple)):
                    existing = dict(headers)
                if not any(k.lower() == "authorization" for k in existing):
                    if isinstance(headers, dict):
                        kwargs["headers"] = {**headers, auth_header[0]: auth_header[1]}
                    else:
                        kwargs["headers"] = list(headers) + [auth_header]
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(FlaskClient, "open", wrapper)
    yield
