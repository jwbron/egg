"""Regression test E.6 (decision-4) — Slice-DAG mid-flight restart.

Covers the invariant from #2535: a slice-N restart while the slice is in
PROPOSE state must NOT mutate slice-N's branch ref (i.e. the slice's
committed work survives the restart), and the pipeline must still reach
``PR_READY``.  The HITL decision-4 in the refine phase pinned the exact
scenario:

  3-slice DAG → restart slice-2's coder while it is in PROPOSE state →
  assert slice-2's branch ref commit SHA is unchanged across the restart →
  assert the pipeline reaches PR_READY.

The restart is driven by the orchestrator's ``restart_agent`` endpoint.
Branch-ref readback uses ``git ls-remote`` (deterministic, no local
state).  Pipeline-terminal observation uses the pipeline status API.

This test is deliberately **deterministic** — no wall-clock sleeps as
checkpoints; we poll on observed state transitions (PROPOSE message
arrival, status flip to PR_READY) so a slow-CI run does not flake.

Contract reference: issue #2474 task-1-10.  Acceptance criterion: passes
on ``main``; reverting #2535's slice-isolation invariant (or any future
regression that changes slice-2's ref on restart) trips the unchanged-
SHA assertion.
"""

from __future__ import annotations

import subprocess
import time

import pytest
import requests

from integration_tests.regression.conftest import start_pipeline

pytestmark = pytest.mark.integration


def _ls_remote_sha(remote_url: str, ref: str, *, timeout: float = 30.0) -> str | None:
    """Return the SHA at ``ref`` on origin, or None if the ref is missing."""
    result = subprocess.run(  # noqa: S603 - trusted args
        ["git", "ls-remote", remote_url, ref],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    parts = result.stdout.splitlines()[0].split("\t", 1)
    return parts[0] if parts else None


def _wait_for_slice_propose(
    orchestrator_url: str,
    pipeline_id: str,
    slice_id: str,
    role: str,
    *,
    timeout: float = 600.0,
    poll_interval: float = 5.0,
) -> dict:
    """Block until the named producer in the named slice emits PROPOSE."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = requests.get(
            f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/messages",
            params={
                "type": "CONSENSUS_PROPOSE",
                "slice_id": slice_id,
                "from_role": role,
            },
            timeout=15,
        )
        if resp.status_code < 400:
            msgs = resp.json().get("messages", []) or []
            if msgs:
                return msgs[0]
        time.sleep(poll_interval)
    raise AssertionError(
        f"Slice {slice_id!r}/{role!r} did not emit CONSENSUS_PROPOSE "
        f"within {timeout}s for pipeline {pipeline_id!r}"
    )


def _wait_for_pipeline_state(
    orchestrator_url: str,
    pipeline_id: str,
    state: str,
    *,
    timeout: float = 600.0,
    poll_interval: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        resp = requests.get(f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status", timeout=15)
        if resp.status_code < 400:
            data = resp.json().get("data", {})
            current = data.get("status") or data.get("state") or ""
            if current:
                seen.append(current)
            if current == state:
                return data
        time.sleep(poll_interval)
    raise AssertionError(
        f"Pipeline {pipeline_id!r} did not reach state {state!r} within "
        f"{timeout}s; observed states: {seen!r}"
    )


def test_slice2_branch_ref_survives_coder_restart_in_propose(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """3-slice DAG, restart slice-2's coder in PROPOSE, assert no ref drift
    and pipeline reaches PR_READY."""
    payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt=(
            "3-slice DAG regression for E.6 (decision-4): restart slice-2's "
            "coder while it is in PROPOSE state.  Scripted providers drive "
            "each slice's coder to PROPOSE then hold; reviewers ACK on the "
            "first iteration so the pipeline can reach PR_READY after the "
            "restart resumes."
        ),
        repo="test-owner/test-repo",
    )
    pipeline_id = payload["pipeline_id"]

    # ── Wait until slice-2's coder reaches PROPOSE.  Slice-1 must complete
    # before slice-2 starts (DAG dependency), so this also exercises the
    # cross-slice gating.
    _wait_for_slice_propose(egg_stack.gateway_url, pipeline_id, "slice-2", "coder")

    remote_url = f"{egg_stack.gateway_url}/git/test-owner/test-repo.git"
    slice2_ref = f"refs/heads/egg/{pipeline_id}/slice-2/work"

    sha_before = _ls_remote_sha(remote_url, slice2_ref)
    assert sha_before is not None, (
        f"slice-2's branch ref {slice2_ref!r} was missing on origin just "
        f"before the restart — the test's pre-condition is broken.  "
        f"Either the scripted coder did not push, or the ref naming "
        f"convention has changed."
    )

    # ── Trigger restart_agent for slice-2's coder.
    restart_resp = requests.post(
        f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}/restart_agent",
        json={"slice_id": "slice-2", "role": "coder"},
        timeout=30,
    )
    assert restart_resp.status_code < 400, (
        f"restart_agent failed: HTTP {restart_resp.status_code} {restart_resp.text}"
    )

    # ── Wait for the pipeline to reach PR_READY.  We don't poll the SHA
    # every iteration because the invariant is "unchanged at the end",
    # not "unchanged moment-to-moment".
    _wait_for_pipeline_state(egg_stack.gateway_url, pipeline_id, "pr_ready")

    sha_after = _ls_remote_sha(remote_url, slice2_ref)
    assert sha_after == sha_before, (
        f"#2535 / E.6 regression: slice-2 branch ref changed across the "
        f"restart: {sha_before!r} → {sha_after!r}.  The restart must "
        f"preserve the slice's committed work; a regression here means "
        f"the coder re-ran from scratch and clobbered the proposal SHA."
    )
