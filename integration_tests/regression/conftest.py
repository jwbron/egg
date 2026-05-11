"""Shared fixtures and helpers for the cross-module regression tier (#2474).

This conftest piggybacks on ``integration_tests/conftest.py`` (the parent
conftest) for the k3s harness — pytest auto-discovers parent conftests,
so the ``egg_stack``, ``gateway_session``, and container helpers defined
there are visible to every test in this subdir without any explicit
re-export. **Do not** re-define ``egg_stack`` here; that would shadow the
parent fixture and silently bypass the kubectl-availability skip.

What this file adds is three thin shims the eight new regression tests in
``integration_tests/regression/`` use to talk to the orchestrator and the
gateway:

* :func:`start_pipeline` — mint a deterministic ``pipeline_id`` from the
  test's pytest nodeid and POST ``/api/v1/pipelines`` + ``/start`` against
  the orchestrator.  The pipeline_id is a stable hash so a re-run of the
  same test name produces the same ID, which keeps the k8s namespace
  recycling sane and lets manual inspection (`kubectl get pods -n ...`)
  find the artefacts of a previous failed run.
* :func:`pod_env` — shell out to ``kubectl get pod -o jsonpath`` and
  return the named container's env-var dict.  Used by the slice-spawn
  EGG_BRANCH-threading test (#2428) and several others that need to
  assert a pod was launched with the right environment.
* :func:`gateway_audit_log_pushes` — read the gateway audit log for push
  events targeting a given ref and return them ordered by timestamp.
  Used by the babysit-PR single-final-push test (E.8 / #2474 HITL Q4)
  which must count *every* push attempt against the PR head ref,
  including pushes that did not advance the ref.

The helpers are deliberately written as plain module-level functions
rather than pytest fixtures so they can be imported directly from
``integration_tests.regression.conftest`` per the acceptance criterion
in task-1-4.  Tests that need the orchestrator URL still acquire it via
the parent conftest's ``egg_stack`` fixture (or its ``orchestrator_url``
attribute, when populated by the k8s overlay).

Contract reference: issue #2474 task-1-4.  Acceptance criterion:
``.venv/bin/pytest --collect-only integration_tests/regression/ -q``
reports 0 errors, the three helpers are importable, ``make lint`` passes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any

import pytest
import requests

# ---------------------------------------------------------------------------
# Helper 1: start_pipeline
# ---------------------------------------------------------------------------


def _deterministic_pipeline_id(test_nodeid: str, *, prefix: str = "regression") -> str:
    """Derive a deterministic 12-char hex id from a pytest nodeid.

    Using SHA-1 (not for security — just for a stable digest) so the same
    test name always reproduces the same id across runs.  Truncated to
    12 hex chars: that is ~48 bits of entropy, more than enough for the
    handful of concurrent regression pipelines we ever run.  The prefix
    keeps these ids visually distinct from normal ``issue-<N>`` ids in
    ``kubectl get pods``.
    """
    digest = hashlib.sha1(test_nodeid.encode("utf-8")).hexdigest()  # noqa: S324
    return f"{prefix}-{digest[:12]}"


def start_pipeline(
    test_request: pytest.FixtureRequest,
    *,
    orchestrator_url: str,
    prompt: str = "regression-test placeholder prompt",
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    config: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Create + start a pipeline keyed off the calling test's nodeid.

    The deterministic pipeline_id makes a flaky test's leftover k8s
    namespace easy to find after the fact.  Returns the orchestrator's
    decoded response payload augmented with ``pipeline_id``; raises
    ``pytest.fail`` (cleanly skipping cleanup downstream) on any non-2xx
    response so the test fails with a clear diagnostic rather than a
    KeyError three lines deeper.
    """
    pipeline_id = _deterministic_pipeline_id(test_request.node.nodeid)

    body: dict[str, Any] = {"prompt": prompt, "pipeline_id": pipeline_id}
    if issue_number is not None:
        body["issue_number"] = issue_number
    if repo is not None:
        body["repo"] = repo
    if branch is not None:
        body["branch"] = branch
    if config is not None:
        body["config"] = config

    create_resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines",
        json=body,
        timeout=timeout,
    )
    if create_resp.status_code >= 400:
        pytest.fail(
            f"Failed to create pipeline {pipeline_id}: "
            f"HTTP {create_resp.status_code} {create_resp.text}"
        )

    start_resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/start",
        timeout=timeout,
    )
    if start_resp.status_code >= 400:
        pytest.fail(
            f"Failed to start pipeline {pipeline_id}: "
            f"HTTP {start_resp.status_code} {start_resp.text}"
        )

    payload = start_resp.json() if start_resp.content else {}
    if isinstance(payload, dict):
        payload.setdefault("pipeline_id", pipeline_id)
    return payload if isinstance(payload, dict) else {"pipeline_id": pipeline_id}


# ---------------------------------------------------------------------------
# Helper 2: pod_env
# ---------------------------------------------------------------------------


def pod_env(
    pipeline_id: str,
    role: str,
    *,
    slice_id: str | None = None,
    namespace: str | None = None,
    container: str | None = None,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Return the env-var dict of the named container in an agent pod.

    Selects pods via the label set the orchestrator stamps on every agent
    pod: ``egg/pipeline-id=<id>`` + ``egg/agent-role=<role>``.  When a
    slice DAG is in play, ``slice_id`` narrows the selection to the
    specific slice's pod (label ``egg/slice-id``).

    Returns a plain ``{name: value}`` dict.  ``EGG_BRANCH`` is the
    canonical thing every caller inspects but the dict carries every
    env var so additional assertions stay easy.
    """
    label_selectors = [
        f"egg/pipeline-id={pipeline_id}",
        f"egg/agent-role={role}",
    ]
    if slice_id is not None:
        label_selectors.append(f"egg/slice-id={slice_id}")

    ns = namespace or _default_namespace()
    cmd = [
        "kubectl",
        "-n",
        ns,
        "get",
        "pod",
        "-l",
        ",".join(label_selectors),
        "-o",
        "json",
    ]
    result = subprocess.run(  # noqa: S603 - args are trusted
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"kubectl get pod failed for {label_selectors!r} in {ns!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )

    try:
        items = json.loads(result.stdout).get("items", [])
    except json.JSONDecodeError as exc:  # pragma: no cover - kubectl returned bad JSON
        raise RuntimeError(f"kubectl returned non-JSON output: {exc}") from exc

    if not items:
        raise RuntimeError(f"No pod matched selectors {label_selectors!r} in namespace {ns!r}")
    # Take the most recently created matching pod so a restart-then-read
    # picks up the new pod, not the dying one.
    items.sort(
        key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""),
        reverse=True,
    )

    spec = items[0].get("spec", {})
    containers = spec.get("containers", [])
    target = _select_container(containers, container)
    env_list = target.get("env", []) or []
    return {entry.get("name"): entry.get("value", "") for entry in env_list if entry.get("name")}


def _select_container(
    containers: list[dict[str, Any]],
    name: str | None,
) -> dict[str, Any]:
    """Pick the named container or default to the first one."""
    if not containers:
        raise RuntimeError("Pod has no containers")
    if name is None:
        return containers[0]
    for c in containers:
        if c.get("name") == name:
            return c
    available = sorted(c.get("name", "?") for c in containers)
    raise RuntimeError(f"Container {name!r} not found; pod has {available}")


def _default_namespace() -> str:
    """Resolve the egg-system namespace.  k3s deployments park agents
    under the test namespace label-selected via ``egg/pipeline-id``; we
    let the caller override but default to the well-known overlay
    namespace.
    """
    return "egg-system"


# ---------------------------------------------------------------------------
# Helper 3: gateway_audit_log_pushes
# ---------------------------------------------------------------------------


def gateway_audit_log_pushes(
    pipeline_id: str,
    ref: str,
    *,
    gateway_url: str,
    launcher_secret: str | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Return push-event entries from the gateway audit log targeting ``ref``.

    The gateway exposes its audit log at ``/internal/audit/pushes`` (a
    privileged route used for debugging and regression assertions).  We
    fetch the full log, filter to events with ``pipeline_id`` and a
    matching ``ref``, and return them sorted oldest-first by timestamp.

    Returned entries are the gateway's raw audit dicts so a test can
    assert on any field (``outcome``, ``rejection_reason``, ``commit``).
    The test E.8 (#2474 task-1-12 / HITL Q4) uses this to count *all*
    push attempts, including ones that did not advance the head ref.
    """
    headers = {}
    if launcher_secret:
        headers["Authorization"] = f"Bearer {launcher_secret}"

    resp = requests.get(
        f"{gateway_url}/internal/audit/pushes",
        params={"pipeline_id": pipeline_id, "ref": ref},
        headers=headers,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Gateway audit-log query failed: HTTP {resp.status_code} {resp.text}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Gateway audit-log returned non-JSON: {exc}") from exc

    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise RuntimeError(
            f"Gateway audit-log payload had no events list; got {type(events).__name__}"
        )

    events.sort(key=lambda e: e.get("timestamp", ""))
    return events


__all__ = [
    "gateway_audit_log_pushes",
    "pod_env",
    "start_pipeline",
]
