"""
Tests for ``_finalize_pr_phase_failed`` — the PR-phase finalizer that creates
the PR (possibly against a stale remote HEAD) and persists the result.

These tests cover the fallback path added for jwbron/egg#1731:
when the orchestrator's push fails but the agents' work is already on
origin, the pipeline should still open the PR rather than failing.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from models import Pipeline, PipelinePhase, PipelineStatus
from routes.pipelines import _finalize_pr_phase_failed


def _make_pipeline(
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
):
    return Pipeline(
        id=f"issue-{issue_number}",
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PR,
    )


class TestFinalizePrPhase:
    def test_push_ok_and_pr_url_stores_artifact_and_reports_no_failure(self):
        """Happy path: push succeeded, _auto_create_pr returns URL,
        artifacts captured, returns False (not failed)."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/99"
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        assert failed is False
        phase_execution = pipeline.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pull/99"}
        store.save_pipeline.assert_called_once_with(pipeline)

    def test_push_failed_but_fallback_pr_url_still_stores_artifact(self):
        """#1731 fallback: push failed, but PR is opened against remote
        HEAD and the URL is returned — treat as success."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/100"
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=False,  # push failed
            )

        assert failed is False
        phase_execution = pipeline.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pull/100"}
        # _auto_create_pr was called despite push_ok=False
        mock_create.assert_called_once()

    def test_push_ok_but_pr_url_none_uses_generic_reason(self):
        """Push succeeded but gateway.create_pr returned None — use the
        generic ``no PR URL returned`` reason."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._handle_pr_creation_failure") as mock_fail,
        ):
            mock_create.return_value = None
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        assert failed is True
        mock_fail.assert_called_once()
        call_kwargs = mock_fail.call_args.kwargs
        assert call_kwargs["reason"] == "no PR URL returned"

    def test_push_failed_and_fallback_failed_uses_actionable_reason(self):
        """Both reconcile and fallback PR creation failed — the caller gets
        a specific reason naming both failure modes."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._handle_pr_creation_failure") as mock_fail,
        ):
            mock_create.return_value = None
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=False,
            )

        assert failed is True
        mock_fail.assert_called_once()
        reason = mock_fail.call_args.kwargs["reason"]
        assert "gateway push rejected" in reason
        assert "fetch+rebase reconcile failed" in reason
        assert "fallback PR against remote HEAD" in reason
