"""Route-level tests for /local-commits and /salvage (#2429)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import Pipeline, PipelinePhase, PipelineStatus
from routes.pipelines import pipelines_bp


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_pipeline(pid: str = "issue-99") -> Pipeline:
    return Pipeline(
        id=pid,
        issue_number=99,
        repo="owner/repo",
        branch=f"egg/{pid}/work",
        base_branch="main",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )


class TestListLocalCommits:
    def test_returns_404_for_unknown_pipeline(self, client):
        from state_store import PipelineNotFoundError

        with patch(
            "routes.pipelines._resolve_pipeline",
            side_effect=PipelineNotFoundError("missing"),
        ):
            resp = client.get("/api/v1/pipelines/missing-pipeline/local-commits")
        assert resp.status_code == 404

    def test_returns_400_for_invalid_role(self, client):
        pipeline = _make_pipeline()
        with patch(
            "routes.pipelines._resolve_pipeline",
            return_value=(MagicMock(), pipeline),
        ):
            resp = client.get("/api/v1/pipelines/issue-99/local-commits?agent_role=not-a-role")
        assert resp.status_code == 400

    def test_returns_serialized_reports(self, client):
        from agent_salvage import (
            AgentWorktree,
            UnpushedCommit,
            WorktreeCommitReport,
        )

        pipeline = _make_pipeline()
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=Path("/tmp/issue-99-coder/repo"),
            local_branch="egg/issue-99-coder/work",
        )
        report = WorktreeCommitReport(
            worktree=wt,
            assigned_branch="egg/issue-99/work",
            anchor_ref="refs/remotes/origin/egg/issue-99/work",
            commits=[
                UnpushedCommit(
                    sha="abc123def456",
                    summary="salvageable change",
                    author="Coder",
                    authored_at="2026-05-06T10:00:00+00:00",
                    files_changed=3,
                )
            ],
        )

        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch(
                "agent_salvage.enumerate_agent_worktrees",
                return_value=[wt],
            ),
            patch(
                "agent_salvage.list_unpushed_commits",
                return_value=report,
            ),
        ):
            resp = client.get("/api/v1/pipelines/issue-99/local-commits")
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["success"] is True
        data = body["data"]
        assert data["pipeline_id"] == "issue-99"
        assert len(data["worktrees"]) == 1
        wt_data = data["worktrees"][0]
        assert wt_data["agent_role"] == "coder"
        assert wt_data["assigned_branch"] == "egg/issue-99/work"
        assert wt_data["commits"][0]["sha"] == "abc123def456"
        assert wt_data["commits"][0]["files_changed"] == 3

    def test_filters_by_agent_role_and_slice(self, client):
        from agent_salvage import AgentWorktree

        pipeline = _make_pipeline()
        wts = [
            AgentWorktree(
                worktree_id="issue-99-coder",
                pipeline_id="issue-99",
                agent_role="coder",
                slice_id=None,
                repo_path=Path("/tmp/c"),
                local_branch="egg/issue-99-coder/work",
            ),
            AgentWorktree(
                worktree_id="issue-99-slice-2-coder",
                pipeline_id="issue-99",
                agent_role="coder",
                slice_id="slice-2",
                repo_path=Path("/tmp/sc"),
                local_branch="egg/issue-99-slice-2-coder/work",
            ),
            AgentWorktree(
                worktree_id="issue-99-tester",
                pipeline_id="issue-99",
                agent_role="tester",
                slice_id=None,
                repo_path=Path("/tmp/t"),
                local_branch="egg/issue-99-tester/work",
            ),
        ]
        seen: list[str] = []

        def fake_list(wt, base_branch=None):
            from agent_salvage import WorktreeCommitReport

            seen.append(wt.worktree_id)
            return WorktreeCommitReport(
                worktree=wt,
                assigned_branch=None,
                anchor_ref=None,
                commits=[],
            )

        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch("agent_salvage.enumerate_agent_worktrees", return_value=wts),
            patch("agent_salvage.list_unpushed_commits", side_effect=fake_list),
        ):
            resp = client.get(
                "/api/v1/pipelines/issue-99/local-commits?agent_role=coder&slice_id=slice-2"
            )
        assert resp.status_code == 200, resp.data
        # Only the slice-2 coder worktree should have been inspected.
        assert seen == ["issue-99-slice-2-coder"]


class TestSalvagePipeline:
    def test_returns_404_for_unknown_pipeline(self, client):
        from state_store import PipelineNotFoundError

        with patch(
            "routes.pipelines._resolve_pipeline",
            side_effect=PipelineNotFoundError("missing"),
        ):
            resp = client.post("/api/v1/pipelines/missing-pipeline/salvage")
        assert resp.status_code == 404

    def test_aggregates_per_worktree_results(self, client):
        from agent_salvage import AgentWorktree, SalvageResult

        pipeline = _make_pipeline()
        wts = [
            AgentWorktree(
                worktree_id="issue-99-coder",
                pipeline_id="issue-99",
                agent_role="coder",
                slice_id=None,
                repo_path=Path("/tmp/c"),
                local_branch="egg/issue-99-coder/work",
            ),
            AgentWorktree(
                worktree_id="issue-99-tester",
                pipeline_id="issue-99",
                agent_role="tester",
                slice_id=None,
                repo_path=Path("/tmp/t"),
                local_branch="egg/issue-99-tester/work",
            ),
        ]

        def fake_salvage(_gw, wt, base_branch=None, mode="public"):
            if wt.agent_role == "coder":
                return SalvageResult(
                    worktree_id=wt.worktree_id,
                    agent_role=wt.agent_role,
                    slice_id=wt.slice_id,
                    recovery_ref=(f"egg/recovered/issue-99/{wt.scope_label}/abc123def456"),
                    head_sha="abc123def456",
                    n_commits=2,
                    ok=True,
                )
            return SalvageResult(
                worktree_id=wt.worktree_id,
                agent_role=wt.agent_role,
                slice_id=wt.slice_id,
                recovery_ref=None,
                head_sha=None,
                n_commits=0,
                ok=True,
            )

        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch("routes.pipelines.get_gateway_client", return_value=MagicMock()),
            patch("agent_salvage.enumerate_agent_worktrees", return_value=wts),
            patch("agent_salvage.salvage_worktree", side_effect=fake_salvage),
        ):
            resp = client.post("/api/v1/pipelines/issue-99/salvage")

        assert resp.status_code == 200, resp.data
        data = resp.get_json()["data"]
        assert data["pipeline_id"] == "issue-99"
        assert len(data["results"]) == 2
        coder_result = next(r for r in data["results"] if r["agent_role"] == "coder")
        assert coder_result["recovery_ref"] == ("egg/recovered/issue-99/coder/abc123def456")
        tester_result = next(r for r in data["results"] if r["agent_role"] == "tester")
        assert tester_result["recovery_ref"] is None

    def test_per_worktree_exception_is_captured_in_results(self, client):
        from agent_salvage import AgentWorktree

        pipeline = _make_pipeline()
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=Path("/tmp/c"),
            local_branch="egg/issue-99-coder/work",
        )
        with (
            patch(
                "routes.pipelines._resolve_pipeline",
                return_value=(MagicMock(), pipeline),
            ),
            patch("routes.pipelines.get_gateway_client", return_value=MagicMock()),
            patch("agent_salvage.enumerate_agent_worktrees", return_value=[wt]),
            patch(
                "agent_salvage.salvage_worktree",
                side_effect=RuntimeError("boom"),
            ),
        ):
            resp = client.post("/api/v1/pipelines/issue-99/salvage")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert result["ok"] is False
        assert "boom" in (result["error"] or "")

    def test_invalid_slice_id_returns_400(self, client):
        pipeline = _make_pipeline()
        with patch(
            "routes.pipelines._resolve_pipeline",
            return_value=(MagicMock(), pipeline),
        ):
            resp = client.post("/api/v1/pipelines/issue-99/salvage?slice_id=not-a-slice")
        assert resp.status_code == 400
