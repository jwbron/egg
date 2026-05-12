"""k3s regression guard for ``restart_agent_job`` slice invariants (#2632).

Background — what the issue's starting point #2 asks for:

  > **Slice DAG with mid-flight ``restart_agent``** — restart slice-2's
  > coder during PROPOSE; assert slice-2 branch ref unchanged and
  > pipeline reaches PR_READY.

The full "pipeline reaches PR_READY" half requires real BRC consensus
running against a Claude provider, which a fresh CI runner cannot
drive (see the ScriptedProvider pod-injection caveat in #2474, also
captured in feedback_scripted_provider_pod_injection.md). This file
pins the half that IS testable without a live agent: **the branch
ref invariants across a restart**.

Specifically, calling ``KubernetesSpawner.restart_agent_job`` on a
slice-scoped Job must:

  1. Produce a new pod whose ``EGG_BRANCH`` matches the slice's
     integration branch — not the pipeline-level ``<pid>/work`` ref.
  2. Preserve ``EGG_SLICE_ID`` so the restarted agent re-enters the
     per-slice consensus tracker (failure mode #3 of #2410).
  3. Use the slice-scoped Job name and worktree id so pipeline-level
     restarts of the same role don't disturb the slice-scoped Job.

A regression in any of these would re-open the #2428 push-rejection
or the #2410 stuck-on-pipeline-tracker bug — but only after the
restart, which is exactly the path that's underexercised today.

See module docstring of ``test_slice_spawn_env_threading.py`` for
why these tests use ``REVIEWER_CODE`` rather than ``CODER``.
"""

from __future__ import annotations

from typing import Any

import pytest

from integration_tests.regression.conftest import (
    env_from_pod,
    kubectl_get_pod_yaml,
)

pytestmark = [pytest.mark.integration]

# These tests previously xfailed on two bugs surfaced while writing
# them against the deployed spawner:
#
#   * #2644 — ``KubernetesClient.delete_job`` didn't apply the same
#     63-char name truncation as ``create_container``, so the
#     restart's delete was a silent 404 against the long form while
#     the Job actually existed under the truncated form. Fixed via
#     ``KubernetesClient._normalize_k8s_job_name``.
#   * #2655 — ``restart_agent_job`` raced the Foreground deletion
#     finalizer: the delete returned before the Job was removed from
#     the API server, and the immediate respawn 409'd on AlreadyExists.
#     Fixed via ``KubernetesClient.wait_for_job_gone`` between the
#     delete and the respawn in ``KubernetesSpawner.restart_agent_job``.


@pytest.fixture
def slice_pipeline_id() -> str:
    import time as _t

    return f"reg-2632r-{_t.time_ns()}"


@pytest.fixture
def cleanup_jobs(spawner, slice_pipeline_id):
    yield
    try:
        spawner.cleanup_pipeline(slice_pipeline_id, force=True)
    except Exception:  # noqa: BLE001
        pass


class TestRestartSliceBranchInvariants:
    """``restart_agent_job`` preserves slice branch and slice-id scope."""

    def test_restart_preserves_egg_branch_and_slice_id(
        self,
        spawner,
        slice_pipeline_id,
        egg_stack,
        cleanup_jobs,
    ):
        """After restart, the new pod's ``EGG_BRANCH`` / ``EGG_SLICE_ID``
        match the original slice scope. UID-wise the pod is new.
        """
        from egg_contracts.agent_roles import AgentRole

        slice_id = "slice-2"
        slice_branch = "egg/issue-2632/slice-2"
        common_kwargs: dict[str, Any] = {
            "pipeline_id": slice_pipeline_id,
            "agent_role": AgentRole.REVIEWER_CODE,
            "issue_number": 2632,
            "phase": "implement",
            "branch": slice_branch,
            "repos": [],
            "mode": "public",
            "slice_id": slice_id,
        }

        # Initial spawn.
        first = spawner.spawn_agent_job(**common_kwargs)
        first_job_name = first.container_info.job_name
        first_pod = kubectl_get_pod_yaml(
            namespace=egg_stack.isolated_network,
            label_selector=f"job-name={first_job_name}",
        )
        first_uid = first_pod.get("metadata", {}).get("uid")
        first_env = env_from_pod(first_pod)
        assert first_env.get("EGG_BRANCH") == slice_branch
        assert first_env.get("EGG_SLICE_ID") == slice_id

        # Mid-flight restart — what ``mcp__restart_agent`` ultimately
        # invokes when an operator restarts a slice-scoped agent.
        restarted = spawner.restart_agent_job(**common_kwargs, reason="regression test")
        restarted_job_name = restarted.container_info.job_name
        assert restarted_job_name == first_job_name, (
            "Restart must reuse the slice-scoped Job name "
            f"(got {restarted_job_name}, expected {first_job_name})"
        )

        # Read the post-restart pod. We don't assert pod UID inequality
        # via the same label selector immediately — the deletion uses
        # ``Foreground`` propagation, so the new pod appears under the
        # same label only after the old pod is gone. Poll until we see
        # a pod with a different uid than the original.
        import time as _t

        deadline = _t.monotonic() + 60.0
        new_pod = None
        while _t.monotonic() < deadline:
            candidate = kubectl_get_pod_yaml(
                namespace=egg_stack.isolated_network,
                label_selector=f"job-name={restarted_job_name}",
                timeout_s=10.0,
            )
            candidate_uid = candidate.get("metadata", {}).get("uid")
            if candidate_uid and candidate_uid != first_uid:
                new_pod = candidate
                break
            _t.sleep(1.0)
        assert new_pod is not None, (
            f"Restart did not produce a new pod within 60s; still seeing uid={first_uid}"
        )

        new_env = env_from_pod(new_pod)
        # The core invariants this test pins:
        assert new_env.get("EGG_BRANCH") == slice_branch, (
            f"Post-restart EGG_BRANCH={new_env.get('EGG_BRANCH')!r}; "
            f"expected {slice_branch!r}. Slice branch ref regressed across "
            f"restart — #2428 fix may have decayed."
        )
        assert new_env.get("EGG_SLICE_ID") == slice_id, (
            f"Post-restart EGG_SLICE_ID={new_env.get('EGG_SLICE_ID')!r}; "
            f"expected {slice_id!r}. Slice-id scope regressed across restart — "
            f"#2410 fix may have decayed."
        )

    def test_restart_isolates_slice_from_pipeline_level_agent(
        self,
        spawner,
        slice_pipeline_id,
        egg_stack,
        cleanup_jobs,
    ):
        """Restarting a pipeline-level agent of the same role does NOT
        disturb the slice-scoped Job. The Job name + restart key carry
        the slice scope, so the two budgets/jobs are independent.
        """
        from egg_contracts.agent_roles import AgentRole

        slice_id = "slice-2"
        # Spawn the slice-scoped Job.
        slice_spawn = spawner.spawn_agent_job(
            pipeline_id=slice_pipeline_id,
            agent_role=AgentRole.REVIEWER_CODE,
            issue_number=2632,
            phase="implement",
            branch="egg/issue-2632/slice-2",
            repos=[],
            mode="public",
            slice_id=slice_id,
        )
        slice_job_name = slice_spawn.container_info.job_name

        # Spawn a pipeline-level Job (no slice_id). Different Job name.
        pipeline_spawn = spawner.spawn_agent_job(
            pipeline_id=slice_pipeline_id,
            agent_role=AgentRole.REVIEWER_CODE,
            issue_number=2632,
            phase="implement",
            branch=f"egg/{slice_pipeline_id}/work",
            repos=[],
            mode="public",
        )
        pipeline_job_name = pipeline_spawn.container_info.job_name
        assert pipeline_job_name != slice_job_name, (
            "Slice-scoped and pipeline-level Jobs must have distinct names "
            f"(both got {slice_job_name})"
        )

        # Restart the pipeline-level agent — independent restart key.
        # ``slice_id=None`` so the restart targets ``pipeline_job_name``,
        # not ``slice_job_name``.
        spawner.restart_agent_job(
            pipeline_id=slice_pipeline_id,
            agent_role=AgentRole.REVIEWER_CODE,
            issue_number=2632,
            phase="implement",
            branch=f"egg/{slice_pipeline_id}/work",
            repos=[],
            mode="public",
            reason="pipeline-level restart should not touch slice",
        )

        # Slice-scoped Job's branch env is unchanged. (We re-read by the
        # slice's job-name selector — if pipeline-level restart had
        # accidentally clobbered the slice Job, we'd either see no pod
        # or a pod with the wrong env.)
        slice_pod = kubectl_get_pod_yaml(
            namespace=egg_stack.isolated_network,
            label_selector=f"job-name={slice_job_name}",
        )
        slice_env = env_from_pod(slice_pod)
        assert slice_env.get("EGG_BRANCH") == "egg/issue-2632/slice-2", (
            "Pipeline-level restart clobbered the slice-scoped Job's "
            f"EGG_BRANCH: got {slice_env.get('EGG_BRANCH')!r}"
        )
        assert slice_env.get("EGG_SLICE_ID") == slice_id

        # Restart-budget isolation: the slice-scoped restart count is
        # still 0 after the pipeline-level restart bumped its own.
        assert (
            spawner.get_restart_count(
                slice_pipeline_id, AgentRole.REVIEWER_CODE.value, slice_id=slice_id
            )
            == 0
        )
        assert spawner.get_restart_count(slice_pipeline_id, AgentRole.REVIEWER_CODE.value) == 1
