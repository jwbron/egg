"""Regression test E.8 — Babysit-PR exactly-one final push.

#2474's ``_babysit_final_push_head_move_guard``
(``orchestrator/routes/pipelines.py``) ensures a babysit-PR run pushes
the PR head ref **exactly once** at the end of consensus, regardless of
how many coder revisions happened during the run.  Without that guard,
each coder revision would re-push the head ref, racing with the
gateway's anchor-write barrier and producing partial PR states that
confuse the PR-merge gating.

HITL Q4 in the refine phase specified the authoritative source for
counting pushes: the **gateway audit log** of push events targeting
the head ref.  ``git ls-remote`` on the PR head before/after each
revision was rejected because it misses pushes that did not advance
the ref (e.g. a force-push to the same commit, an attempted push that
the gateway rejected).  The audit log records every push **attempt**,
which is what we want to count.

This test drives a babysit-PR run through 2 coder revisions and then
asserts ``len(gateway_audit_log_pushes(...)) == 1`` for the PR head
ref.

Contract reference: issue #2474 task-1-12.  Acceptance criterion: passes
on ``main``; reverting ``_babysit_final_push_head_move_guard`` causes a
second push to land on the head ref and the exact-count assertion
fires.
"""

from __future__ import annotations

import time

import pytest
import requests

from integration_tests.regression.conftest import (
    gateway_audit_log_pushes,
    start_pipeline,
)

pytestmark = pytest.mark.integration


def _wait_for_pipeline_terminal(
    orchestrator_url: str,
    pipeline_id: str,
    *,
    timeout: float = 900.0,
    poll_interval: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = requests.get(f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status", timeout=15)
        if resp.status_code < 400:
            data = resp.json().get("data", {})
            status = data.get("status", "")
            if status in {"complete", "failed", "cancelled", "pr_ready"}:
                return data
        time.sleep(poll_interval)
    raise AssertionError(f"Pipeline {pipeline_id!r} did not reach terminal state within {timeout}s")


def test_babysit_pr_pushes_head_ref_exactly_once(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """A babysit-PR run through 2 coder revisions must produce exactly 1
    push to the PR head ref in the gateway audit log."""
    pr_head_ref = "refs/heads/babysit-regression-head"

    payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt=(
            "Babysit-PR regression for E.8 single-final-push.  Scripted "
            "coder produces two revisions to a synthetic PR; BRC reaches "
            "CONFIRMED on the second iteration."
        ),
        repo="test-owner/test-repo",
        config={
            "mode": "babysit_pr",
            "pr_number": 9999,
            "pr_head_ref": pr_head_ref,
        },
    )
    pipeline_id = payload["pipeline_id"]

    _wait_for_pipeline_terminal(egg_stack.gateway_url, pipeline_id)

    pushes = gateway_audit_log_pushes(
        pipeline_id,
        pr_head_ref,
        gateway_url=egg_stack.gateway_url,
        launcher_secret=getattr(egg_stack, "launcher_secret", None),
    )

    # Filter to successful pushes only — the rejection-and-retry path on
    # the coder side may produce additional *attempt* entries that did
    # not advance the head, and those are legitimate.  The invariant
    # under test is "exactly one push *succeeded* on the head ref",
    # which is what the orchestrator's
    # ``_babysit_final_push_head_move_guard`` is responsible for.
    successful_head_pushes = [
        push
        for push in pushes
        if push.get("ref") == pr_head_ref and push.get("outcome", "success") == "success"
    ]

    assert len(successful_head_pushes) == 1, (
        f"E.8 regression: gateway audit log shows {len(successful_head_pushes)} "
        f"successful pushes to {pr_head_ref!r} for pipeline {pipeline_id!r}; "
        f"expected exactly 1.  Reverting "
        f"_babysit_final_push_head_move_guard (orchestrator/routes/"
        f"pipelines.py) reproduces this failure — each coder revision "
        f"would push the head ref, defeating the single-final-push "
        f"invariant.  Push entries observed: {successful_head_pushes!r}"
    )
