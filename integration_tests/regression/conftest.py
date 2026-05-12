"""Shared helpers for the regression tier (#2474, #2634).

The parent ``integration_tests/conftest.py`` owns the k3s harness —
``egg_stack``, ``orchestrator_url``, kubectl-skip semantics — so we
re-export nothing here. This file adds two tier-local helpers:

* :func:`deterministic_pipeline_id` — derives a stable 12-hex-char
  pipeline id from a pytest nodeid so re-runs of the same test reuse
  the same id (easier post-mortem in ``kubectl get pods``).
* :func:`lifecycle_secret` — reads the orchestrator's lifecycle
  bearer from ``gateway-secrets/lifecycle-secret`` in ``egg-system``.
  Returns ``None`` (and the caller should ``pytest.skip``) when the
  secret is unavailable — happy-path tests need it, auth-rejection
  tests do not.

Tests in this tier MUST be marked ``@pytest.mark.integration`` so
``make test-integration`` (`-m "integration or security"`) picks them
up. The parent conftest's k3s harness drives the skip behaviour when
kubectl is missing.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess

import pytest

_LIFECYCLE_SECRET_NAMESPACE = "egg-system"
_LIFECYCLE_SECRET_NAME = "gateway-secrets"
_LIFECYCLE_SECRET_KEY = "lifecycle-secret"


def deterministic_pipeline_id(test_nodeid: str) -> str:
    """Return a stable, **syntactically valid** pipeline id from a nodeid.

    The id matches the ``pipeline-{8 hex chars}`` arm of
    ``state_store.PIPELINE_ID_PATTERN`` — any other shape (e.g. the
    ``regression-<hex>`` shape from #2474's recovered attempt) trips
    ``InvalidPipelineIdError`` → 400 before the 404 path runs, masking
    "pipeline not found" assertions.

    SHA-1 is used as a stable digest, not a cryptographic hash, so the
    Bandit warning is suppressed.
    """
    digest = hashlib.sha1(test_nodeid.encode("utf-8")).hexdigest()  # noqa: S324
    return f"pipeline-{digest[:8]}"


def lifecycle_secret() -> str | None:
    """Return the orchestrator's ``EGG_LIFECYCLE_SECRET`` if reachable.

    Reads ``gateway-secrets/lifecycle-secret`` from the ``egg-system``
    namespace. Returns ``None`` if kubectl is missing, the secret is
    absent, or the value cannot be decoded — callers should
    ``pytest.skip`` rather than fail in that case so happy-path tests
    are skipped cleanly when run by a developer without read access on
    the secret (CI has it).
    """
    cmd = [
        "kubectl",
        "-n",
        _LIFECYCLE_SECRET_NAMESPACE,
        "get",
        "secret",
        _LIFECYCLE_SECRET_NAME,
        "-o",
        f"jsonpath={{.data.{_LIFECYCLE_SECRET_KEY}}}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        # ``.strip()`` because ``kubectl create secret --from-file`` keeps
        # every byte of the source file including the trailing newline;
        # a ``\n`` inside ``f"Bearer {secret}"`` is rejected by
        # ``http.client.putheader``.
        return base64.b64decode(result.stdout).decode("utf-8").strip()
    except ValueError, UnicodeDecodeError:
        return None


@pytest.fixture
def lifecycle_bearer() -> str:
    """Return an ``Authorization: Bearer ...`` value or skip the test.

    Used by happy-path tests that need to call
    ``@require_lifecycle_secret`` endpoints. When the secret is not
    reachable from the test runner (developer laptop without rbac on
    the secret) the test is skipped, not failed.
    """
    secret = lifecycle_secret()
    if not secret:
        pytest.skip(
            "lifecycle-secret not readable from gateway-secrets in "
            f"namespace {_LIFECYCLE_SECRET_NAMESPACE} — happy-path "
            "lifecycle endpoint tests skipped"
        )
    return f"Bearer {secret}"


@pytest.fixture
def regression_pipeline_id(request: pytest.FixtureRequest) -> str:
    """Stable pipeline id derived from the calling test's pytest nodeid."""
    return deterministic_pipeline_id(request.node.nodeid)


__all__ = [
    "deterministic_pipeline_id",
    "lifecycle_bearer",
    "lifecycle_secret",
    "regression_pipeline_id",
]
