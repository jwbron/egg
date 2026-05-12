"""Shared fixtures for ``integration_tests/regression`` (issue #2640).

The regression tier exercises load-bearing seams between the
orchestrator's Flask blueprints, the inter-agent message store
(in-memory + Redis Streams), and the in-process ``EventBus``.

These tests follow the integration-tier pattern established by
``integration_tests/test_slice_pipeline_e2e.py``:

* ``@pytest.mark.integration`` (applied at module level in each test
  file) so ``make test-integration`` picks them up.
* In-process fakes for any boundary that would otherwise require k3s,
  Docker, or live LLM calls — ``fakeredis`` for the Redis backend,
  ``unittest.mock.patch`` for the pipeline state-store and the inner
  context-PR hook.
* Real ``Flask`` blueprint and real ``EventBus`` so the routing path
  under test is the same one production runs.

The dual-backend parametrization mirrors the AC pattern from
``orchestrator/tests/test_pipelines_status_wait_route.py``: every
test that touches the message store runs against both ``MessageStore``
(in-memory) and ``RedisMessageStore`` backed by ``fakeredis.FakeRedis``
so a regression in either backend surfaces.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# sys.path setup: orchestrator + shared. Mirrors the existing
# integration_tests/test_slice_pipeline_e2e.py setup so the regression
# tier can import the orchestrator's internal modules (events,
# message_store, redis_message_store, routes.pipelines).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ORCH = _PROJECT_ROOT / "orchestrator"
_SHARED = _PROJECT_ROOT / "shared"
for _p in (_ORCH, _SHARED, _PROJECT_ROOT):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# Lifecycle-secret-gated routes (PATCH /pipelines/<id>, DELETE
# /pipelines/<id>, signals, etc.) require ``EGG_LIFECYCLE_SECRET`` to be
# set + ``Authorization: Bearer <secret>`` on the request. The
# orchestrator's own test suite sets this session-wide via
# ``orchestrator/tests/conftest.py``; mirror the pattern here so the
# regression tier can drive PATCH-side routes the same way.
_TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-regression"


@pytest.fixture(autouse=True, scope="session")
def _set_lifecycle_secret_env():
    """Set ``EGG_LIFECYCLE_SECRET`` for the regression test session."""
    prev = os.environ.get("EGG_LIFECYCLE_SECRET")
    os.environ["EGG_LIFECYCLE_SECRET"] = _TEST_LIFECYCLE_SECRET
    yield
    if prev is None:
        os.environ.pop("EGG_LIFECYCLE_SECRET", None)
    else:
        os.environ["EGG_LIFECYCLE_SECRET"] = prev


@pytest.fixture
def lifecycle_auth_headers() -> dict[str, str]:
    """Valid ``Authorization`` header for lifecycle-control endpoints."""
    return {"Authorization": f"Bearer {_TEST_LIFECYCLE_SECRET}"}
