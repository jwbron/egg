"""Regression test for #2428 — slice-spawn EGG_BRANCH threading.

#2428 was an in-process state-mutation bug where ``_compose_branch_env``
threaded the *parent* pipeline's ``EGG_BRANCH`` value into every slice
pod's environment, instead of computing the slice-specific ref
``egg/<issue>/slice-N/work``.  That meant a child slice's coder would
attempt to push to (and look up its own anchor under) the wrong branch,
producing silent cross-slice contamination that CI did not catch
because no integration test exercised the multi-slice spawn path.

This test spins up a 2-slice DAG pipeline through the orchestrator, then
inspects each slice pod's ``EGG_BRANCH`` env var via :func:`pod_env`
and asserts the threaded value matches the slice's ref.  Inline failure
messages reference #2428 so a regression in the future surfaces with
the right issue number attached.

Contract reference: issue #2474 task-1-5.  Acceptance criterion: passes
on ``main``; reverting #2428's ``_compose_branch_env`` fix on a scratch
branch causes the test to fail with the inline #2428 message.
"""

from __future__ import annotations

import time

import pytest

from integration_tests.regression.conftest import pod_env, start_pipeline

pytestmark = pytest.mark.integration


def _wait_for_pod_with_env(
    pipeline_id: str,
    role: str,
    *,
    slice_id: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> dict[str, str]:
    """Poll :func:`pod_env` until a pod is observed or the deadline expires."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            env = pod_env(pipeline_id, role, slice_id=slice_id)
            if env:
                return env
        except RuntimeError as exc:  # pod not yet running
            last_error = exc
        time.sleep(poll_interval)
    raise AssertionError(
        f"Timed out waiting for {role!r} pod for slice {slice_id!r} in "
        f"pipeline {pipeline_id!r}: last_error={last_error!r}"
    )


def test_slice_pod_env_threads_per_slice_branch(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """Each slice's pod must see its own ``EGG_BRANCH`` value.

    The 2-slice DAG used here has ``slice-2`` depending on ``slice-1``;
    the orchestrator spawns ``slice-1``'s coder first, then ``slice-2``'s
    coder once ``slice-1`` reaches PR_READY.  We need both pods to have
    been scheduled at some point during the run for the assertion to
    fire — the polling helper above tolerates the slight scheduling
    delay between the two waves.
    """
    pipeline_payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt="2-slice regression for #2428 EGG_BRANCH threading",
        repo="test-owner/test-repo",
        # The orchestrator infers the 2-slice DAG from the prompt's
        # contract scaffold; in production a real plan phase produces
        # this — here the scripted-provider harness in the parent
        # conftest stubs it out for the test.
    )
    pipeline_id = pipeline_payload["pipeline_id"]

    slice_envs = {
        slice_id: _wait_for_pod_with_env(pipeline_id, role="coder", slice_id=slice_id)
        for slice_id in ("slice-1", "slice-2")
    }

    for slice_id, env in slice_envs.items():
        observed = env.get("EGG_BRANCH", "")
        expected = f"egg/issue-2474/{slice_id}/work"  # placeholder; replaced by issue under test  # noqa: E501
        # Issue number doesn't matter for the invariant — only that the
        # branch carries the SLICE id, not the parent ref.
        assert slice_id in observed, (
            f"#2428 regression: slice {slice_id!r} pod has "
            f"EGG_BRANCH={observed!r}; expected the slice id {slice_id!r} "
            f"to appear in the ref (expected shape: {expected!r}). "
            f"Reverting #2428's _compose_branch_env fix re-introduces "
            f"this failure mode."
        )

    # Cross-slice contamination check: slice-2 must NOT inherit slice-1's
    # branch verbatim.  This is the precise invariant #2428 broke.
    assert slice_envs["slice-1"]["EGG_BRANCH"] != slice_envs["slice-2"]["EGG_BRANCH"], (
        "#2428 regression: slice-1 and slice-2 pods are seeing the same "
        "EGG_BRANCH value — _compose_branch_env threaded the parent ref "
        "into both slices, defeating per-slice isolation."
    )
