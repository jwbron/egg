"""k3s regression guard for slice-spawn env threading (#2428, #2632).

The #2428 incident: slice-2's coder pushed to ``egg/<pid>/work``
instead of ``egg/<pid>/slice-2`` and was rejected by the gateway's
session-scoped branch allowlist. Root cause was that the spawn loop
threaded the pipeline-level ``EGG_BRANCH`` through ``sandbox_env``
which then arrived at ``spawn_agent_job(extra_env=...)`` and overrode
the per-slice ``branch`` parameter.

The fix moved ``EGG_BRANCH`` into ``_PROTECTED_ENV_KEYS`` so the
spawner's branch parameter wins. Unit coverage in
``orchestrator/tests/test_kubernetes_spawner.py`` exercises the
spawner's env-building logic with mocked k8s. This file pins the
**deployed-pod** end of the seam: a k3s Job whose pod spec carries
the right ``EGG_BRANCH`` / ``EGG_SLICE_ID`` after the spawner serializes
the env into the k8s API. A regression that survives the spawner's
override but gets clobbered by ``client.V1EnvVar`` ordering, the
sandbox image's entrypoint, or any future env-rewriting layer would
slip past the unit tests; this test catches it.

Constraint (#2474): the SDLC pipeline cannot run integration tests.
Correctness is verified by the ``Test / aggregate`` required check on
the PR — see the issue body for the full write-up.

Why this test uses ``REVIEWER_CODE`` instead of ``CODER``:

  The env-threading code path in ``KubernetesSpawner.spawn_agent_job``
  is role-independent — see ``kubernetes_spawner.py`` lines 754-774,
  where ``EGG_BRANCH`` and ``EGG_SLICE_ID`` are written from the
  ``branch`` / ``slice_id`` parameters regardless of ``agent_role``.
  ``REVIEWER_CODE`` is in ``_ROLES_WITHOUT_WORKTREE`` so the spawn
  doesn't require ``gateway.create_worktrees`` to succeed against a
  populated test repo — a fresh CI runner has
  ``local_repos.paths: []`` (see ``.github/workflows/test-integration.yml``)
  so a worktree-requiring spawn would skip the actual assertion. The
  CODER-specific BRC push-rejection path is unit-tested in
  ``orchestrator/tests/test_kubernetes_spawner.py::TestSpawnEnvironment``.
"""

from __future__ import annotations

import pytest

from integration_tests.regression.conftest import (
    env_from_pod,
    kubectl_get_pod_yaml,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def slice_pipeline_id() -> str:
    """Unique pipeline id per test run so concurrent runs don't collide."""
    import time as _t

    return f"reg-2632-{_t.time_ns()}"


@pytest.fixture
def cleanup_jobs(spawner, slice_pipeline_id, egg_stack):
    """Tear down any Job spawned during the test, even on assertion failure."""
    yield
    try:
        spawner.cleanup_pipeline(slice_pipeline_id, force=True)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def _spawn_slice(spawner, pipeline_id: str, slice_id: str, namespace: str) -> dict:
    """Spawn a single slice agent and return its pod spec (parsed dict).

    The pod's ``job-name`` label is the standard selector k8s sets on
    every Job-spawned pod; we read the first pod under that Job.
    """
    from egg_contracts.agent_roles import AgentRole

    slice_branch = f"egg/issue-2632/{slice_id}"
    spawned = spawner.spawn_agent_job(
        pipeline_id=pipeline_id,
        agent_role=AgentRole.REVIEWER_CODE,
        issue_number=2632,
        phase="implement",
        branch=slice_branch,
        repos=[],  # see module docstring
        mode="public",
        slice_id=slice_id,
        # Repro #2428: simulate the pipeline-level ``sandbox_env`` carrying
        # the pipeline-wide branch into a per-slice spawn. The spawner's
        # ``_PROTECTED_ENV_KEYS`` must drop this and keep the per-slice
        # value from ``branch``.
        extra_env={"EGG_BRANCH": f"egg/{pipeline_id}/work"},
    )
    job_name = spawned.container_info.job_name
    pod = kubectl_get_pod_yaml(
        namespace=namespace,
        label_selector=f"job-name={job_name}",
    )
    return pod


class TestSliceSpawnEnvThreading:
    """Per-slice ``EGG_BRANCH`` and ``EGG_SLICE_ID`` reach the pod spec."""

    def test_each_slice_gets_its_own_branch_env(
        self,
        spawner,
        slice_pipeline_id,
        egg_stack,
        cleanup_jobs,
    ):
        """Three concurrent slices: each pod's ``EGG_BRANCH`` is its slice ref.

        The #2428 regression would manifest as ``EGG_BRANCH = egg/<pid>/work``
        on every pod — i.e. the ``extra_env`` override winning. The fix
        keeps the spawner's ``branch`` parameter authoritative.
        """
        slices = ["slice-1", "slice-2", "slice-3"]
        seen_branches: dict[str, str] = {}
        seen_slice_ids: dict[str, str] = {}
        seen_job_names: set[str] = set()
        for slice_id in slices:
            pod = _spawn_slice(
                spawner,
                slice_pipeline_id,
                slice_id,
                egg_stack.isolated_network,
            )
            env = env_from_pod(pod)

            expected_branch = f"egg/issue-2632/{slice_id}"
            assert env.get("EGG_BRANCH") == expected_branch, (
                f"Slice {slice_id} pod has EGG_BRANCH={env.get('EGG_BRANCH')!r}; "
                f"expected {expected_branch!r}. The #2428 ``extra_env`` override "
                f"likely won — verify ``EGG_BRANCH`` is in ``_PROTECTED_ENV_KEYS``."
            )
            assert env.get("EGG_SLICE_ID") == slice_id, (
                f"Slice {slice_id} pod has EGG_SLICE_ID={env.get('EGG_SLICE_ID')!r}; "
                f"expected {slice_id!r}. The #2410 slice-id propagation regressed."
            )
            seen_branches[slice_id] = env["EGG_BRANCH"]
            seen_slice_ids[slice_id] = env["EGG_SLICE_ID"]

            job_name = pod.get("metadata", {}).get("labels", {}).get("job-name")
            assert job_name, f"Pod for {slice_id} missing job-name label"
            seen_job_names.add(job_name)

        # Sibling-isolation invariant (#2403): concurrent slices in the
        # same pipeline must spawn under distinct Job names so the
        # pre-spawn cleanup in ``spawn_agent_job`` doesn't delete the
        # in-flight sibling slice's Job.
        assert len(seen_job_names) == len(slices), (
            f"Slice Jobs should have distinct names; got {seen_job_names}"
        )
        # And the EGG_BRANCH map must be the identity expected_branch
        # function — no two slices share a branch ref.
        assert len(set(seen_branches.values())) == len(slices), (
            f"Each slice must own a distinct EGG_BRANCH; got {seen_branches}"
        )

    def test_baseline_spawn_without_extra_env_override(
        self,
        spawner,
        slice_pipeline_id,
        egg_stack,
        cleanup_jobs,
    ):
        """Baseline: when no conflicting ``extra_env`` is shipped, the
        per-slice ``branch`` parameter still flows through to the pod's
        ``EGG_BRANCH``. Distinguishes "the protected-key override is
        working" from "the env-threading code happens to work only
        when extra_env happens to be empty / non-conflicting" — without
        this guard, a regression that broke the default ``EGG_BRANCH``
        derivation (e.g. dropping line ``environment["EGG_BRANCH"] = branch``
        from ``kubernetes_spawner.py:754``) would be invisible to
        ``test_each_slice_gets_its_own_branch_env`` because the
        override path masks the default path.
        """
        from egg_contracts.agent_roles import AgentRole

        slice_id = "slice-2"
        slice_branch = "egg/issue-2632/slice-2"
        spawned = spawner.spawn_agent_job(
            pipeline_id=slice_pipeline_id,
            agent_role=AgentRole.REVIEWER_CODE,
            issue_number=2632,
            phase="implement",
            branch=slice_branch,
            repos=[],
            mode="public",
            slice_id=slice_id,
            # Critically: no ``extra_env`` — exercise the default
            # env-derivation path, not the override-rejection path.
        )
        pod = kubectl_get_pod_yaml(
            namespace=egg_stack.isolated_network,
            label_selector=f"job-name={spawned.container_info.job_name}",
        )
        env = env_from_pod(pod)
        assert env.get("EGG_BRANCH") == slice_branch, (
            f"Baseline (no extra_env): EGG_BRANCH={env.get('EGG_BRANCH')!r}; "
            f"expected {slice_branch!r}. The default env-derivation path "
            f"may have regressed independent of the override path."
        )
        assert env.get("EGG_SLICE_ID") == slice_id
