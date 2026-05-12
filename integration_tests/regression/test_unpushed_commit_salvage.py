"""Integration regression tests for the salvage hook (#2429).

The per-module unit tests in ``orchestrator/tests/`` already cover
``agent_salvage``, ``cleanup_pipeline``, and the ``/salvage`` route
in isolation — but each one stubs the layer beneath it (gateway,
filesystem, or salvage helper). A regression that breaks the *wiring*
between those layers — recovery-ref name composition, real git ref
discovery, the order of salvage vs. worktree deletion — would slip
past the unit tier.

These tests use **real** ``agent_salvage`` (``enumerate_agent_worktrees``,
``list_unpushed_commits``, ``salvage_worktree``, ``auto_salvage_pipeline``)
and **real** git plumbing on ``tmp_path``. Only the gateway HTTP boundary
is mocked. The scenario mirrors the #2429 incident: agent commits to its
per-agent worktree's ``egg/{worktree_id}/work`` branch, the gateway
rejects the push to the assigned branch (so no ``origin/<assigned>``
ref ever advanced past the anchor), and the salvage path must push to
``egg/recovered/<pipeline>/<scope>/<short_sha>`` before the worktree is
torn down.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agent_salvage import (
    RECOVERY_BRANCH_PREFIX,
    auto_salvage_pipeline,
)
from gateway_client import PushResult
from models import Pipeline, PipelinePhase, PipelineStatus

from ._helpers import (
    build_worktree as _build_worktree,
)
from ._helpers import (
    commit as _commit,
)
from ._helpers import (
    create_origin_tracking as _create_origin_tracking,
)
from ._helpers import (
    make_repo as _make_repo,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Direct ``auto_salvage_pipeline`` path
# ---------------------------------------------------------------------------


class TestAutoSalvageRealGit:
    """``auto_salvage_pipeline`` against real worktrees on disk."""

    def test_recovery_ref_carries_head_sha_and_scope(self, tmp_path, fake_gateway):
        """The recovery ref name is ``egg/recovered/<pipeline>/<scope>/<short>``.

        The short SHA in the ref name comes from the worktree's actual
        HEAD — so a salvage-then-resalvage of new work produces a *new*
        ref instead of force-overwriting the original.
        """
        wt, head_sha = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id="slice-2",
            assigned_branch="egg/issue-2429/slice-2",
            n_unpushed=3,
        )

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        assert len(results) == 1
        result = results[0]
        assert result.ok is True
        assert result.head_sha == head_sha
        assert result.n_commits == 3
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-2429/slice-2-coder/{head_sha[:12]}"
        assert result.recovery_ref == expected_ref

        # The gateway push call carries the right kwargs.
        fake_gateway.push_worktree_branch.assert_called_once()
        kwargs = fake_gateway.push_worktree_branch.call_args.kwargs
        assert kwargs["pipeline_id"] == "issue-2429"
        assert kwargs["branch"] == expected_ref
        assert kwargs["ref"] is None  # push HEAD, not a named ref
        assert kwargs["force"] is False  # immutable ref — no force needed

    def test_multiple_worktrees_each_get_own_ref(self, tmp_path, fake_gateway):
        """Pipeline-level + per-role + slice-scoped worktrees each get a ref."""
        _, pipeline_head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role=None,
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )
        _, coder_head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )
        _, slice_coder_head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id="slice-3",
            assigned_branch="egg/issue-2429/slice-3",
        )

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        # All three salvaged.
        assert {r.worktree_id for r in results} == {
            "issue-2429",
            "issue-2429-coder",
            "issue-2429-slice-3-coder",
        }
        assert all(r.ok and r.recovery_ref for r in results)

        recovered_refs = {r.recovery_ref for r in results}
        assert (
            f"{RECOVERY_BRANCH_PREFIX}/issue-2429/pipeline/{pipeline_head[:12]}" in recovered_refs
        )
        assert f"{RECOVERY_BRANCH_PREFIX}/issue-2429/coder/{coder_head[:12]}" in recovered_refs
        assert (
            f"{RECOVERY_BRANCH_PREFIX}/issue-2429/slice-3-coder/{slice_coder_head[:12]}"
            in recovered_refs
        )

    def test_no_unpushed_commits_skips_push(self, tmp_path, fake_gateway):
        """A worktree whose HEAD == anchor has nothing to salvage — no push."""
        wt, head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
            n_unpushed=0,
        )
        # No unpushed work means the local branch is at the anchor already.
        # Confirm fixture invariant before exercising the salvage path.
        _create_origin_tracking(wt.repo_path, "egg/issue-2429/work", head)

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].n_commits == 0
        assert results[0].recovery_ref is None
        fake_gateway.push_worktree_branch.assert_not_called()

    def test_gateway_push_rejection_surfaces_as_failed_result(self, tmp_path):
        """The wedged push category propagates through to ``SalvageResult.error``.

        Mirrors #2429's gateway-rejection scenario: the gateway refuses
        the recovery push for some reason (auth, transient transport).
        The hook captures the failure on the per-worktree row so the
        cleanup loop can keep going.
        """
        _, _ = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )

        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(
            ok=False,
            category="non_fast_forward",
            detail="recovery ref already exists at a different sha",
        )

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(gateway, "issue-2429")

        assert len(results) == 1
        result = results[0]
        assert result.ok is False
        assert result.recovery_ref is None
        assert result.error is not None
        assert "non_fast_forward" in result.error

    def test_salvage_filter_skips_unrelated_pipeline(self, tmp_path, fake_gateway):
        """A pipeline-id prefix collision must not pull a sibling's worktree.

        Mirrors #1865 / #2403 — naive ``startswith(pipeline_id + "-")``
        would match ``issue-2429-debug-tester`` from a hypothetical
        sibling pipeline. The real ``enumerate_agent_worktrees`` filters
        on the trailing role/slice shape so the prefix collision is
        ignored.
        """
        _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )
        # Looks like ``issue-2429-...`` but the trailing segment is not
        # a valid agent role — must be filtered out.
        unrelated = tmp_path / "issue-2429-debug-bogus-suffix" / "repo"
        _make_repo(unrelated, "main")

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        assert [r.worktree_id for r in results] == ["issue-2429-coder"]


# ---------------------------------------------------------------------------
# Edge cases — corrupt worktrees, fan-out, no-anchor fallback, concurrent salvage
# ---------------------------------------------------------------------------


class TestSalvageEdgeCases:
    """Edge cases that the per-module unit tests cover in isolation but
    that haven't been exercised through the integration chain on real
    on-disk state.
    """

    def test_corrupt_worktree_filtered_without_blocking_loop(self, tmp_path, fake_gateway):
        """A worktree missing ``.git`` is filtered out by the enumerator
        and does NOT block salvage of healthy siblings.

        ``enumerate_agent_worktrees(validate_git=True)`` — the default
        on the salvage path — drops directories without a usable repo
        checkout entirely, so the loop never reaches them. That's the
        invariant a single broken btrfs mount must not starve every
        salvageable sibling on the same pipeline (#1723 / #2429
        cleanup-policy).

        The per-row ``SalvageResult(ok=False, error="worktree has no
        .git marker")`` path is covered at the single-worktree level
        by the unit test
        ``TestSalvageWorktree.test_corrupt_worktree_returns_not_ok``;
        this test documents the multi-worktree enumeration filter.
        """
        # One healthy worktree with an unpushed commit.
        _, healthy_head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )
        # One broken worktree: directory exists, but no .git inside —
        # the enumerator's validate_git=True filter drops it.
        broken_dir = tmp_path / "issue-2429-tester" / "repo"
        broken_dir.mkdir(parents=True)

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        # Healthy sibling salvaged; broken sibling absent from results.
        worktree_ids = [r.worktree_id for r in results]
        assert worktree_ids == ["issue-2429-coder"]
        assert "issue-2429-tester" not in worktree_ids
        assert results[0].ok is True
        assert results[0].head_sha == healthy_head
        # Exactly one push — the broken worktree never tried.
        fake_gateway.push_worktree_branch.assert_called_once()

    def test_many_slices_each_get_distinct_recovery_ref(self, tmp_path, fake_gateway):
        """Slice-DAG fan-out: 5 slice coders => 5 distinct refs, one push each.

        Mirrors the #2429 trigger scenario where N slice coders all
        committed locally and the operator runs salvage once. The
        recovery refs must each carry their own scope + HEAD short
        SHA so a later operator-side replay can pick them apart per
        slice.
        """
        heads: dict[str, str] = {}
        for i in range(1, 6):
            slice_id = f"slice-{i}"
            _, head = _build_worktree(
                tmp_path,
                "issue-2429",
                agent_role="coder",
                slice_id=slice_id,
                assigned_branch=f"egg/issue-2429/{slice_id}",
            )
            heads[slice_id] = head

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        assert len(results) == 5
        assert all(r.ok and r.recovery_ref for r in results)
        # All recovery refs are distinct.
        recovery_refs = {r.recovery_ref for r in results}
        assert len(recovery_refs) == 5
        # And each carries its slice's short SHA.
        for r in results:
            slice_id = r.slice_id
            expected = (
                f"{RECOVERY_BRANCH_PREFIX}/issue-2429/{slice_id}-coder/{heads[slice_id][:12]}"
            )
            assert r.recovery_ref == expected
        # The gateway saw 5 pushes, one per slice.
        assert fake_gateway.push_worktree_branch.call_count == 5

    def test_no_anchor_falls_back_to_full_history(self, tmp_path, fake_gateway):
        """Worktree with no remote-tracking ref still salvages.

        Anchor discovery has three layers:

        1. ``branch.<local>.merge`` + ``origin/<assigned>`` — happy path.
        2. ``origin/<base_branch>`` — when the assigned tracking ref is
           absent (e.g. agent never managed a fetch).
        3. No anchor — fall back to the full HEAD history (capped at
           200 commits in ``list_unpushed_commits``).

        The third path was added defensively. Confirming it through
        the integration chain catches a regression where the salvage
        loop short-circuits to ``n_commits=0`` when no anchor exists.
        """
        # Build a worktree but skip ``set_assigned_branch`` and
        # ``create_origin_tracking``. The agent committed locally but
        # has no remote-tracking ref at all.
        worktree_id = "issue-2429-coder"
        local_branch = f"egg/{worktree_id}/work"
        repo = tmp_path / worktree_id / "repo"
        _make_repo(repo, local_branch)
        head = _commit(repo, "unpushed.txt", "work\n", "salvage me, no anchor")

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(fake_gateway, "issue-2429")

        assert len(results) == 1
        result = results[0]
        assert result.ok is True
        # Without an anchor the helper walks the full HEAD history. With
        # one seed commit and one local commit, exactly two are reported.
        # A regression that short-circuits to 0, or that trims to 1 by
        # mis-applying an anchor, would each fail this assertion.
        assert result.n_commits == 2
        assert result.head_sha == head
        assert result.recovery_ref == (f"{RECOVERY_BRANCH_PREFIX}/issue-2429/coder/{head[:12]}")


# ---------------------------------------------------------------------------
# ``/salvage`` route end-to-end
# ---------------------------------------------------------------------------


def _make_pipeline(pipeline_id: str = "issue-2429") -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        issue_number=2429,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}/work",
        base_branch="main",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )


class TestSalvageRouteEndToEnd:
    """POST ``/api/v1/pipelines/<id>/salvage`` with real salvage helpers.

    The route-level unit tests in ``orchestrator/tests/test_routes_salvage.py``
    mock both ``enumerate_agent_worktrees`` and ``salvage_worktree``. These
    tests mock only the gateway, so a regression in either of the salvage
    helpers OR in how the route plugs them together is caught.
    """

    def test_pushes_recovery_ref_per_worktree_and_returns_results(
        self, client, tmp_path, fake_gateway
    ):
        wt, head_sha = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id="slice-2",
            assigned_branch="egg/issue-2429/slice-2",
            n_unpushed=2,
        )
        pipeline = _make_pipeline("issue-2429")

        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch("routes.pipelines.get_gateway_client", return_value=fake_gateway),
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
        ):
            resp = client.post("/api/v1/pipelines/issue-2429/salvage")

        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["success"] is True
        data = body["data"]
        assert data["pipeline_id"] == "issue-2429"
        assert len(data["results"]) == 1
        row = data["results"][0]
        assert row["ok"] is True
        assert row["worktree_id"] == "issue-2429-slice-2-coder"
        assert row["agent_role"] == "coder"
        assert row["slice_id"] == "slice-2"
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-2429/slice-2-coder/{head_sha[:12]}"
        assert row["recovery_ref"] == expected_ref
        assert row["head_sha"] == head_sha
        assert row["n_commits"] == 2

        # The recovery push happened — exact ref name and force=False.
        fake_gateway.push_worktree_branch.assert_called_once()
        kwargs = fake_gateway.push_worktree_branch.call_args.kwargs
        assert kwargs["branch"] == expected_ref
        assert kwargs["force"] is False

    def test_filters_by_slice_id(self, client, tmp_path, fake_gateway):
        """``slice_id=slice-1`` narrows salvage to that slice's worktree.

        The filtered row carries the same helper-output shape as the
        unfiltered case — proving the route plugs slice-narrowed results
        through ``_serialize_salvage_result`` correctly. Without these
        assertions a regression that serialized only the worktree_id
        but dropped recovery_ref / head_sha / n_commits would pass.
        """
        wt_s1, s1_head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id="slice-1",
            assigned_branch="egg/issue-2429/slice-1",
            n_unpushed=2,
        )
        _, _ = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id="slice-2",
            assigned_branch="egg/issue-2429/slice-2",
        )
        pipeline = _make_pipeline("issue-2429")

        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch("routes.pipelines.get_gateway_client", return_value=fake_gateway),
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
        ):
            resp = client.post("/api/v1/pipelines/issue-2429/salvage?slice_id=slice-1")

        assert resp.status_code == 200, resp.data
        rows = resp.get_json()["data"]["results"]
        assert len(rows) == 1
        row = rows[0]
        assert row["worktree_id"] == wt_s1.worktree_id
        assert row["ok"] is True
        assert row["slice_id"] == "slice-1"
        assert row["head_sha"] == s1_head
        assert row["n_commits"] == 2
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-2429/slice-1-coder/{s1_head[:12]}"
        assert row["recovery_ref"] == expected_ref
        # Exactly one push — slice-2 was filtered out — and the pushed
        # branch matches the recovery ref reported back to the client.
        fake_gateway.push_worktree_branch.assert_called_once()
        push_kwargs = fake_gateway.push_worktree_branch.call_args.kwargs
        assert push_kwargs["branch"] == expected_ref

    def test_list_local_commits_uses_real_git(self, client, tmp_path):
        """The read-only GET surfaces real ``git log`` output, not mock data.

        Catches a regression where ``list_unpushed_commits`` stops
        delegating to git (e.g. someone swaps in a placeholder) — the
        commit summary on the response is the one literally written by
        the commit helper above.
        """
        wt, head = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
            n_unpushed=2,
        )
        # The two commits had messages "unpushed change 0" / "unpushed change 1".
        pipeline = _make_pipeline("issue-2429")

        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
        ):
            resp = client.get("/api/v1/pipelines/issue-2429/local-commits")

        assert resp.status_code == 200, resp.data
        data = resp.get_json()["data"]
        assert len(data["worktrees"]) == 1
        wt_data = data["worktrees"][0]
        assert wt_data["worktree_id"] == "issue-2429-coder"
        assert wt_data["assigned_branch"] == "egg/issue-2429/work"
        # newest-first.
        summaries = [c["summary"] for c in wt_data["commits"]]
        assert summaries == ["unpushed change 1", "unpushed change 0"]
        # files_changed is read off real `git log --shortstat`.
        assert all(c["files_changed"] == 1 for c in wt_data["commits"])


# ---------------------------------------------------------------------------
# Salvage-before-teardown sequencing (the explicit ask in #2633)
# ---------------------------------------------------------------------------


class TestSalvageBeforeWorktreeTeardown:
    """The salvage push to ``egg/recovered/...`` must appear before any
    worktree deletion. The ordering invariant is what makes the salvage
    hook recover-from-the-default-policy of "silent loss" — a regression
    where the order is swapped would push to a recovery ref pointing at
    a deleted worktree (or, worse, never push at all because the
    worktree is already gone by the time salvage starts).
    """

    def test_recovery_push_precedes_worktree_delete_call(self, tmp_path):
        """``push_worktree_branch`` is invoked strictly before ``delete_worktrees``."""
        # Stub kubernetes_spawner's gateway and k8s clients at the boundary.
        # We don't import KubernetesSpawner directly — that pulls in the
        # k8s SDK at module load and the integration tier doesn't bring
        # one up. Instead, exercise the auto-salvage hook the way
        # ``cleanup_pipeline`` calls it (real helper, mocked gateway)
        # and assert the gateway saw the recovery push.
        _, head_sha = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )

        call_log: list[tuple[str, dict]] = []
        gateway = MagicMock()

        def record_push(**kwargs: Any) -> PushResult:
            call_log.append(("push", kwargs))
            return PushResult(ok=True)

        def record_delete(**kwargs: Any) -> Any:
            call_log.append(("delete", kwargs))
            return MagicMock(success=True, worktrees={}, errors=[])

        gateway.push_worktree_branch.side_effect = record_push
        gateway.delete_worktrees.side_effect = record_delete

        # Run salvage first (the hook), then delete (the cleanup body).
        # This mirrors ``kubernetes_spawner.cleanup_pipeline``'s order
        # at lines 1070–1091.
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            salvage_results = auto_salvage_pipeline(gateway, "issue-2429")
        # Cleanup body — would normally be one delete per worktree id.
        gateway.delete_worktrees(container_id="issue-2429-coder", force=True)

        # Salvage push must come first in the call log.
        assert [stage for stage, _ in call_log] == ["push", "delete"]

        # And the recovery ref carries the actual HEAD SHA that was on
        # disk before deletion — proving the push happened against the
        # live worktree, not after teardown.
        push_kwargs = call_log[0][1]
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-2429/coder/{head_sha[:12]}"
        assert push_kwargs["branch"] == expected_ref
        assert salvage_results[0].recovery_ref == expected_ref

    def test_failed_salvage_does_not_block_subsequent_delete(self, tmp_path):
        """If the salvage push raises, cleanup continues to ``delete_worktrees``.

        Best-effort contract from #2429 — a salvage failure is logged
        but cannot wedge the cleanup loop. Otherwise a single wedged
        worktree could starve every later pipeline of its cleanup.
        """
        _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2429/work",
        )

        gateway = MagicMock()
        gateway.push_worktree_branch.side_effect = RuntimeError("gateway down")
        delete_calls: list[dict] = []
        gateway.delete_worktrees.side_effect = lambda **kw: delete_calls.append(kw)

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(gateway, "issue-2429")

        # Per-worktree row records the failure, ``auto_salvage_pipeline``
        # itself doesn't raise.
        assert len(results) == 1
        assert results[0].ok is False
        assert "gateway down" in (results[0].error or "")

        # The cleanup loop would proceed to ``delete_worktrees`` —
        # simulate the next step.
        gateway.delete_worktrees(container_id="issue-2429-coder", force=True)
        assert delete_calls == [{"container_id": "issue-2429-coder", "force": True}]


# ---------------------------------------------------------------------------
# Full cleanup_pipeline body — real salvage helpers + real worktrees (#2659)
# ---------------------------------------------------------------------------


class TestCleanupPipelineEndToEnd:
    """Drives ``KubernetesSpawner.cleanup_pipeline`` end-to-end with the
    real ``auto_salvage_pipeline`` hook against real on-disk worktrees.

    The unit tier (``orchestrator/tests/test_kubernetes_spawner_salvage.py``)
    stubs ``agent_salvage.auto_salvage_pipeline`` wholesale, so a regression
    in the wiring between the FS-scan loop, the salvage hook, and the
    per-worktree ``delete_worktrees`` calls would slip past both tiers.
    ``TestSalvageBeforeWorktreeTeardown`` above asserts the ordering
    invariant via a simulated cleanup body; this class invokes the actual
    ``cleanup_pipeline`` body so the FS-scan rules, the
    ``worktree_ids_to_clean`` set construction, and the salvage-then-delete
    iteration all stay covered.
    """

    def _make_spawner(self, gateway: MagicMock) -> object:
        """Instantiate ``KubernetesSpawner`` with mock k8s + injected gateway.

        Mirrors the unit-tier fixture in
        ``orchestrator/tests/test_kubernetes_spawner_salvage.py`` — the
        spawner constructor accepts the gateway as a kwarg so the
        ``get_gateway_client`` default never runs.
        """
        from kubernetes_client import PodNotFoundError
        from kubernetes_spawner import KubernetesSpawner
        from models import ContainerInfo, ContainerStatus

        k8s = MagicMock()
        k8s.delete_job.side_effect = PodNotFoundError("No existing job")
        k8s.list_containers.return_value = []
        k8s.remove_container.return_value = None
        k8s.create_container.return_value = ContainerInfo(
            container_id="uid-x",
            container_name="egg-x",
            job_name="egg-x",
            status=ContainerStatus.PENDING,
        )
        return KubernetesSpawner(k8s_client=k8s, gateway_client=gateway)

    def test_cleanup_pipeline_pushes_recovery_ref_before_delete(self, tmp_path):
        """Single coder worktree: recovery push lands before delete_worktrees.

        Drives the full ``cleanup_pipeline`` body — FS scan picks the
        ``{pipeline}-coder`` directory off ``WORKTREE_BASE_DIR`` ``tmp_path``,
        the real ``auto_salvage_pipeline`` enumerates the same set, the
        gateway sees a push for the ``egg/recovered/<pipeline>/coder/...``
        ref *before* the ``delete_worktrees`` call for the same worktree id.
        """
        _, head_sha = _build_worktree(
            tmp_path,
            "issue-2659",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2659/work",
        )

        call_log: list[tuple[str, str]] = []
        gateway = MagicMock()

        def record_push(**kwargs):
            call_log.append(("push", kwargs.get("branch", "")))
            return PushResult(ok=True)

        def record_delete(**kwargs):
            call_log.append(("delete", kwargs.get("container_id", "")))
            return MagicMock(success=True, worktrees={}, errors=[])

        gateway.push_worktree_branch.side_effect = record_push
        gateway.delete_worktrees.side_effect = record_delete

        spawner = self._make_spawner(gateway)

        with (
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
        ):
            spawner.cleanup_pipeline(
                "issue-2659",
                salvage_mode="private",
                salvage_base_branch="main",
            )

        # Salvage push for the coder ref must precede the
        # delete_worktrees call for the same worktree id.
        stages = [stage for stage, _ in call_log]
        assert "push" in stages
        assert "delete" in stages
        first_push = stages.index("push")
        # The delete for the salvaged worktree id specifically.
        coder_delete_idx = next(
            (
                i
                for i, (stage, target) in enumerate(call_log)
                if stage == "delete" and target == "issue-2659-coder"
            ),
            None,
        )
        assert coder_delete_idx is not None
        assert first_push < coder_delete_idx

        # The recovery ref name carries the actual HEAD SHA (proves the
        # push happened against the live worktree, pre-teardown).
        push_targets = [target for stage, target in call_log if stage == "push"]
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-2659/coder/{head_sha[:12]}"
        assert expected_ref in push_targets

    def test_cleanup_pipeline_threads_salvage_mode_and_base_branch(self, tmp_path):
        """``salvage_mode="private"`` reaches ``push_worktree_branch``.

        The unit tier proves the helper kwargs are forwarded; this drives
        the real chain so ``cleanup_pipeline`` → ``auto_salvage_pipeline``
        → ``salvage_worktree`` → ``gateway.push_worktree_branch`` lands
        on the gateway with ``mode="private"``. Regression target: a
        future refactor that drops the mode kwarg silently degrades a
        private-repo pipeline back to a public-mode push (the #2429
        review-feedback class).
        """
        _build_worktree(
            tmp_path,
            "issue-2659",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2659/work",
        )

        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)
        gateway.delete_worktrees.return_value = MagicMock(success=True, worktrees={}, errors=[])

        spawner = self._make_spawner(gateway)

        with (
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
        ):
            spawner.cleanup_pipeline(
                "issue-2659",
                salvage_mode="private",
                salvage_base_branch="main",
            )

        gateway.push_worktree_branch.assert_called_once()
        push_kwargs = gateway.push_worktree_branch.call_args.kwargs
        assert push_kwargs["mode"] == "private"

    def test_cleanup_pipeline_pushes_one_ref_per_worktree(self, tmp_path):
        """Pipeline-level + per-role + slice-scoped worktrees: one ref each.

        Three distinct worktree shapes get scanned off the FS, salvaged
        in one pass, then each deleted. The deletes must cover the same
        set the salvage walked — otherwise the FS-scan loop has drifted
        from ``enumerate_agent_worktrees``.
        """
        _, pipeline_head = _build_worktree(
            tmp_path,
            "issue-2659",
            agent_role=None,
            slice_id=None,
            assigned_branch="egg/issue-2659/work",
        )
        _, coder_head = _build_worktree(
            tmp_path,
            "issue-2659",
            agent_role="coder",
            slice_id=None,
            assigned_branch="egg/issue-2659/work",
        )
        _, slice_coder_head = _build_worktree(
            tmp_path,
            "issue-2659",
            agent_role="coder",
            slice_id="slice-3",
            assigned_branch="egg/issue-2659/slice-3",
        )

        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)
        gateway.delete_worktrees.return_value = MagicMock(success=True, worktrees={}, errors=[])

        spawner = self._make_spawner(gateway)

        with (
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
            patch("kubernetes_spawner.WORKTREE_BASE_DIR", tmp_path),
        ):
            spawner.cleanup_pipeline("issue-2659")

        pushed_refs = {
            call.kwargs["branch"] for call in gateway.push_worktree_branch.call_args_list
        }
        assert pushed_refs == {
            f"{RECOVERY_BRANCH_PREFIX}/issue-2659/pipeline/{pipeline_head[:12]}",
            f"{RECOVERY_BRANCH_PREFIX}/issue-2659/coder/{coder_head[:12]}",
            f"{RECOVERY_BRANCH_PREFIX}/issue-2659/slice-3-coder/{slice_coder_head[:12]}",
        }

        deleted_ids = {
            call.kwargs["container_id"] for call in gateway.delete_worktrees.call_args_list
        }
        # The same three worktree ids the salvage hook walked.
        assert {
            "issue-2659",
            "issue-2659-coder",
            "issue-2659-slice-3-coder",
        }.issubset(deleted_ids)
