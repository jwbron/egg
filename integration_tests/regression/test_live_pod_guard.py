"""Regression test for #2420 — live-pod guard on pipeline restart.

#2420 was a state-machine bug where the orchestrator's ``start_pipeline``
endpoint would happily relaunch a pipeline whose previous agent pods
were still Running, leaving the cluster with two parallel coder pods
on the same branch — racing to push, racing to claim BRC consensus, in
general producing silent data races.

The fix added a live-pod guard that refuses ``start_pipeline`` when any
agent pod is still in ``Running`` (or ``Pending``) phase for the same
pipeline_id, unless the caller passes ``force=True`` to acknowledge the
clobber.

This test drives that decision tree: it starts a pipeline, waits for at
least one agent pod to reach Running, then calls ``start_pipeline`` a
second time and asserts the refusal.  A follow-up ``force=True`` call
must succeed and the new pod set must replace the old one (we observe a
different ``startTime`` to confirm fresh scheduling, not pod re-use).

Contract reference: issue #2474 task-1-6.  Acceptance criterion: passes
on ``main``; reverting #2420's guard makes the second
``start_pipeline(force=False)`` call succeed and the test fails on the
refusal assertion.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest
import requests

from integration_tests.regression.conftest import start_pipeline

pytestmark = pytest.mark.integration


def _list_pipeline_pods(pipeline_id: str, *, namespace: str = "egg-system") -> list[dict]:
    """Return all agent pods labelled with the given pipeline_id."""
    cmd = [
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        f"egg/pipeline-id={pipeline_id}",
        "-o",
        "json",
    ]
    result = subprocess.run(  # noqa: S603 - args are trusted
        cmd, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout).get("items", [])


def _wait_for_running_pod(pipeline_id: str, timeout: float = 120.0) -> dict:
    """Poll until at least one agent pod reaches phase ``Running``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pod in _list_pipeline_pods(pipeline_id):
            phase = pod.get("status", {}).get("phase", "")
            if phase == "Running":
                return pod
        time.sleep(2.0)
    raise AssertionError(
        f"No agent pod for pipeline {pipeline_id!r} reached Running within {timeout}s"
    )


def test_live_pod_guard_refuses_unforced_restart_then_force_succeeds(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """``start_pipeline(force=False)`` must refuse while pods are Running;
    ``force=True`` must replace them with a fresh pod set."""
    payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt="single-slice regression for #2420 live-pod guard",
        repo="test-owner/test-repo",
    )
    pipeline_id = payload["pipeline_id"]

    original_pod = _wait_for_running_pod(pipeline_id)
    original_start_time = original_pod.get("status", {}).get("startTime")
    assert original_start_time, (
        f"Initial pod for pipeline {pipeline_id!r} had no startTime in "
        f"status — kubectl payload shape changed?"
    )

    # ── Second call without force MUST refuse.  We assert on the response,
    # not by polling for pod state, because the refusal is the contract.
    refused = requests.post(
        f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}/start",
        json={"force": False},
        timeout=30.0,
    )
    assert refused.status_code in (409, 412, 423), (
        f"#2420 regression: start_pipeline(force=False) returned "
        f"HTTP {refused.status_code} while pods were Running — expected "
        f"409/412/423 refusal.  Body: {refused.text!r}.  Reverting "
        f"#2420's guard re-introduces this failure."
    )
    body = refused.json() if refused.content else {}
    assert "live" in (body.get("message") or body.get("error", "")).lower(), (
        f"Refusal body should reference live pods; got {body!r}"
    )

    # ── Now force the restart.  New pods must have a later startTime.
    forced = requests.post(
        f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}/start",
        json={"force": True},
        timeout=30.0,
    )
    assert forced.status_code < 400, (
        f"start_pipeline(force=True) failed: HTTP {forced.status_code} {forced.text}"
    )

    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        new_pod = _list_pipeline_pods(pipeline_id)
        for pod in new_pod:
            phase = pod.get("status", {}).get("phase", "")
            new_start = pod.get("status", {}).get("startTime", "")
            if phase == "Running" and new_start and new_start > original_start_time:
                # Fresh pod confirmed.
                return
        time.sleep(2.0)

    pytest.fail(
        f"After force=True restart, no pod with startTime > "
        f"{original_start_time!r} was observed for pipeline {pipeline_id!r}"
    )
