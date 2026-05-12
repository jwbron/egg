"""Shared fixtures for regression integration tests (#2633).

These tests sit between the unit-tier (``orchestrator/tests/``) and the
k3s-tier (``integration_tests/test_*.py`` against a live cluster):

* They import the real ``pipelines_bp`` blueprint and exercise routes
  through an in-process Flask test client — same shape as
  ``integration_tests/test_babysit_pr/``.
* They use the real ``agent_salvage`` module, the real git binary, and
  real worktree directories on ``tmp_path``. Only the gateway HTTP
  client and the spawner backend (the network/k8s boundaries) are
  stubbed out.
* They are marker-gated under ``@pytest.mark.integration`` so they run
  under ``make test-integration`` / the ``Test / integration`` CI
  required-check.

The point is to catch regressions in the *wiring* between the route
layer, the salvage helpers, and the git plumbing — exactly the seams
that the per-module unit tests in ``orchestrator/tests/`` mock out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mirror integration_tests/test_babysit_pr/conftest.py: make orchestrator/
# and shared/ importable, and stub the kubernetes / docker SDKs so the
# blueprint loads without those packages installed.
_repo_root = Path(__file__).resolve().parent.parent.parent
for _dir in ("orchestrator", "shared"):
    _p = str(_repo_root / _dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())


_TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-regression"


@pytest.fixture(autouse=True)
def _set_lifecycle_secret_env():
    """Set ``EGG_LIFECYCLE_SECRET`` so lifecycle-gated routes don't 503.

    Function-scoped per the same reasoning as
    ``test_babysit_pr/conftest.py``: a session-scoped autouse would leak
    the env var to any sibling suite that happens to read it directly.
    """
    overrides = {
        "EGG_LIFECYCLE_SECRET": _TEST_LIFECYCLE_SECRET,
        # In-process Flask client never talks to a real gateway; short-
        # circuit the gateway-readiness gate that ``create_pipeline``
        # otherwise waits 60s for.
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
    """Auto-attach ``Authorization: Bearer …`` to every FlaskClient request."""
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


@pytest.fixture
def app():
    """A Flask app with the real pipelines blueprint mounted."""
    from flask import Flask
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()
