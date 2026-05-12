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

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agent_salvage import (
    RECOVERY_BRANCH_PREFIX,
    AgentWorktree,
    auto_salvage_pipeline,
)
from gateway_client import PushResult
from models import Pipeline, PipelinePhase, PipelineStatus

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Real-git helpers — mirror the shape of ``orchestrator/tests/test_agent_salvage.py``
# but live in the integration tier so they can be reused across the
# regression suite without coupling to the unit-tier conftest.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=salvage@test.example",
            "-c",
            "user.name=Salvage Tester",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(path: Path, branch_name: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--initial-branch", branch_name, cwd=path)
    (path / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "seed", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    _git("add", filename, cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _set_assigned_branch(repo: Path, local_branch: str, assigned: str) -> None:
    """Mirror what gateway/worktree_manager writes at worktree create time.

    The agent's per-agent worktree carries ``branch.<local>.merge`` set
    to ``refs/heads/<assigned>``; that's how ``list_unpushed_commits``
    discovers the assigned upstream when computing the anchor cut.
    """
    _git(
        "config",
        f"branch.{local_branch}.merge",
        f"refs/heads/{assigned}",
        cwd=repo,
    )


def _create_origin_tracking(repo: Path, remote_branch: str, sha: str) -> None:
    """Stand in for ``origin/<branch>`` after a fetch."""
    _git("update-ref", f"refs/remotes/origin/{remote_branch}", sha, cwd=repo)


def _build_worktree(
    base: Path,
    pipeline_id: str,
    *,
    agent_role: str | None,
    slice_id: str | None,
    assigned_branch: str,
    n_unpushed: int = 1,
) -> tuple[AgentWorktree, str]:
    """Create a per-agent worktree directory with ``n_unpushed`` local commits.

    Returns the ``AgentWorktree`` descriptor and the HEAD SHA (the SHA
    that the salvage path is supposed to push to its recovery ref).
    """
    if agent_role is None:
        worktree_id = pipeline_id
        scope = "pipeline"
    elif slice_id is None:
        worktree_id = f"{pipeline_id}-{agent_role}"
        scope = agent_role
    else:
        worktree_id = f"{pipeline_id}-{slice_id}-{agent_role}"
        scope = f"{slice_id}-{agent_role}"

    local_branch = f"egg/{worktree_id}/work"
    repo = base / worktree_id / "repo"
    anchor = _make_repo(repo, local_branch)
    _set_assigned_branch(repo, local_branch, assigned_branch)
    # The wedged scenario from #2429: agent pushes were rejected so the
    # assigned-branch tracking ref never advanced past the anchor. Local
    # commits accumulate on the work branch with no remote anchor for
    # them.
    _create_origin_tracking(repo, assigned_branch, anchor)

    head = anchor
    for i in range(n_unpushed):
        head = _commit(repo, f"unpushed-{i}.txt", f"work {i}\n", f"unpushed change {i}")

    wt = AgentWorktree(
        worktree_id=worktree_id,
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        slice_id=slice_id,
        repo_path=repo,
        local_branch=local_branch,
    )
    assert wt.scope_label == scope  # belt-and-braces against helper drift
    return wt, head


@pytest.fixture
def fake_gateway() -> MagicMock:
    """A gateway stub whose ``push_worktree_branch`` records every call."""
    gw = MagicMock()
    gw.push_worktree_branch.return_value = PushResult(ok=True)
    return gw


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
        """``slice_id=slice-1`` narrows salvage to that slice's worktree."""
        wt_s1, _ = _build_worktree(
            tmp_path,
            "issue-2429",
            agent_role="coder",
            slice_id="slice-1",
            assigned_branch="egg/issue-2429/slice-1",
        )
        wt_s2, _ = _build_worktree(
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
        assert rows[0]["worktree_id"] == wt_s1.worktree_id
        # Exactly one push — slice-2 was filtered out.
        fake_gateway.push_worktree_branch.assert_called_once()

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
