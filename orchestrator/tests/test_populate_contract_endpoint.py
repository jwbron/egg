"""Tests for the POST /<pipeline_id>/phase/populate-contract endpoint.

This endpoint wraps the internal _populate_contract_from_plan() function,
resolving the worktree path and returning phase/task counts.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from gateway_client import PushResult
from models import Pipeline, PipelinePhase
from routes.phases import phases_bp
from routes.pipelines import PopulateOutcome, PopulateResult
from state_store import InvalidPipelineIdError, PipelineNotFoundError


def _populated(slice_count: int = 1, task_count: int = 1) -> PopulateResult:
    """Build a successful PopulateResult for use as mock return values."""
    return PopulateResult(
        PopulateOutcome.POPULATED,
        slice_count=slice_count,
        task_count=task_count,
    )


@pytest.fixture
def app():
    """Create a test Flask app with the phases blueprint."""
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


def _make_pipeline(
    pipeline_id="issue-42",
    phase=PipelinePhase.IMPLEMENT,
    issue_number=42,
):
    """Create a minimal Pipeline for testing."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch="egg/issue-42",
    )
    pipeline.current_phase = phase
    return pipeline


class TestPopulateContractEndpoint:
    """Tests for POST /<pipeline_id>/phase/populate-contract."""

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_not_found(self, mock_get_store, client):
        """Returns 404 when pipeline doesn't exist."""
        mock_get_store.side_effect = PipelineNotFoundError("Pipeline not found")

        resp = client.post("/api/v1/pipelines/issue-999/phase/populate-contract")

        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "not found" in data["message"]

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_pipeline_id(self, mock_get_store, client):
        """Returns 400 for invalid pipeline ID format."""
        mock_get_store.side_effect = InvalidPipelineIdError("Invalid ID format")

        resp = client.post("/api/v1/pipelines/bad!!id/phase/populate-contract")

        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_mode_from_pipeline_not_config(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """pipeline.mode (not pipeline.config.mode) is used for pipeline_mode.

        Regression test: originally pipeline.config.mode was used but
        PipelineConfig has no mode attribute — mode lives on Pipeline directly.

        Stacks the same persist-block mocks as the success-path tests
        so the new commit/push block doesn't execute against the real
        :func:`_commit_statefiles_to_worktree` and (depending on host
        worktree state) fire real ``git add``/``commit`` calls.
        """
        pipeline = _make_pipeline()
        # pipeline.mode defaults to 'issue' from PipelineMode.ISSUE
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_populate.return_value = _populated()
        mock_commit.return_value = False
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        mock_spawner.return_value.gateway.push_worktree_branch.return_value = push_result

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        call_kwargs = mock_populate.call_args[1]
        assert call_kwargs["pipeline_mode"] == "issue"

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_populate_function_exception(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """Returns 500 when the populate function raises an exception."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.side_effect = RuntimeError("plan draft not found")

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "plan draft not found" in data["message"]

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_with_counts_and_issue_number(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """Successful populate returns phase/task counts and forwards issue_number."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.return_value = _populated(slice_count=1, task_count=2)
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        mock_spawner.return_value.gateway.push_worktree_branch.return_value = push_result

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        # Counts now come straight from the populate return value (#2627
        # follow-up): no separate read-back to disagree with the populator.
        assert data["data"]["phase_count"] == 1
        assert data["data"]["task_count"] == 2
        assert data["data"]["pushed_to_origin"] is True

        # Verify populate was called with pipeline.mode (defaults to 'issue')
        call_kwargs = mock_populate.call_args[1]
        assert str(call_kwargs["pipeline_mode"]) == "issue"
        assert call_kwargs["issue_number"] == 42

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_commits_and_pushes_contract_to_origin(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """populate_contract commits and pushes the contract so fresh agent
        spawns see the populated state on origin (#2629)."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        worktree_path = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_resolve_wt.return_value = worktree_path
        mock_populate.return_value = _populated()
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        gateway = mock_spawner.return_value.gateway
        gateway.push_worktree_branch.return_value = push_result

        client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        mock_commit.assert_called_once()
        commit_kwargs = mock_commit.call_args.kwargs
        assert commit_kwargs["pipeline_identifier"] == 42
        assert commit_kwargs["pipeline_id"] == "issue-42"

        gateway.push_worktree_branch.assert_called_once_with(
            pipeline_id="issue-42",
            repo_path=str(worktree_path),
            branch="egg/issue-42",
            mode="public",
            base_branch=pipeline.base_branch,
        )

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_push_failure_reports_pushed_to_origin_false(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When the push fails, populate still returns success but reports
        ``pushed_to_origin=False`` so the operator knows the contract is
        only on the orchestrator's local worktree (#2629)."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.return_value = _populated(slice_count=1, task_count=1)
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        gateway = mock_spawner.return_value.gateway
        gateway.push_worktree_branch.side_effect = RuntimeError("network down")

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["pushed_to_origin"] is False
        # Counts still come back so the caller can confirm populate worked.
        assert data["data"]["task_count"] == 1

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_falsy_push_result_reports_pushed_to_origin_false(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When ``push_worktree_branch`` *returns* a falsy ``PushResult``
        (the gateway client converts most push failures to this shape —
        ``non_fast_forward``, ``auth_failed``, ``reconcile_fetch_failed``,
        etc. — rather than raising), the route logs
        ``push_result.describe()`` and reports
        ``pushed_to_origin=False`` (#2629).

        This covers the falsy-return branch separately from the
        exception branch exercised by
        ``test_push_failure_reports_pushed_to_origin_false`` — the
        falsy-return shape is the more common one in practice because
        :func:`gateway_client._do_push` catches most exceptions and
        converts them via :func:`_classify_push_stderr`.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.return_value = _populated(slice_count=1, task_count=1)
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        gateway = mock_spawner.return_value.gateway
        gateway.push_worktree_branch.return_value = PushResult(
            ok=False,
            category="non_fast_forward",
            detail="(fetch first)",
        )

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["pushed_to_origin"] is False
        # The push attempt happened — only the result reports failure.
        gateway.push_worktree_branch.assert_called_once()

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_commit_noop_still_pushes(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When the commit short-circuits (nothing staged), the push
        still runs — a no-op commit does NOT imply origin matches local
        (#2629).

        The per-pipeline worktree is long-lived and may carry commits
        ahead of origin from a prior failed push.  Pushing
        unconditionally fast-forwards in the safe case (origin already
        matches → no-op push) and delivers the un-pushed commit in the
        dangerous one.  Reporting ``pushed_to_origin=True`` here
        requires the push to actually report success — we do not
        infer it from the no-op commit alone.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.return_value = _populated()
        mock_commit.return_value = False
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        gateway = mock_spawner.return_value.gateway
        gateway.push_worktree_branch.return_value = push_result

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["data"]["pushed_to_origin"] is True
        gateway.push_worktree_branch.assert_called_once()

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_failed_push_retry_with_noop_commit_still_reports_failure(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """Regression for the failed-push-then-retry recovery scenario
        (#2629).

        Models the post-failure retry state: a prior ``populate_contract``
        call committed locally but the push failed, leaving local HEAD
        ahead of origin by one commit.  The operator retries — the
        file on disk now matches HEAD so ``_commit_statefiles_to_worktree``
        returns ``False`` (no-op commit) — but the un-pushed commit is
        still on local only.

        The route must still attempt the push.  If the push fails again
        (gateway down, ``non_fast_forward``, etc.), ``pushed_to_origin``
        must be ``False`` so the operator does not interpret a no-op
        commit as success when origin is in fact still empty.  The
        original wedge #2629 was opened against was a caller silently
        treating ``pushed_to_origin=True`` as "contract is on origin"
        when it was not.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.return_value = _populated()
        # Second-call shape: file already matches HEAD locally → no-op.
        mock_commit.return_value = False
        mock_gw_mode.return_value = ("public", None)
        gateway = mock_spawner.return_value.gateway
        # Push fails again — origin still does not have the contract.
        gateway.push_worktree_branch.return_value = PushResult(
            ok=False,
            category="non_fast_forward",
            detail="(fetch first)",
        )

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        # The push was attempted (no shortcut from no-op commit) and
        # its failure was honored (no false success).
        gateway.push_worktree_branch.assert_called_once()
        assert data["data"]["pushed_to_origin"] is False

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_branch_unset_skips_persist_and_reports_false(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When ``pipeline.branch`` is unset, the persist block is skipped
        and ``pushed_to_origin=False`` is returned (#2629).

        The persist block is gated on ``pipeline.branch and worktree_path
        != store.repo_path``; without a branch there is nothing to push
        to.  ``False`` is the correct signal here — the caller knows the
        populated state is only on the orchestrator's local worktree.
        """
        pipeline = _make_pipeline()
        pipeline.branch = None  # No work branch configured yet.
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.return_value = _populated()
        mock_gw_mode.return_value = ("public", None)
        gateway = mock_spawner.return_value.gateway

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["pushed_to_origin"] is False
        # Neither commit nor push are attempted without a branch.
        mock_commit.assert_not_called()
        gateway.push_worktree_branch.assert_not_called()

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_worktree_path_passed_to_populate(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """Verify worktree path (not raw repo path) is passed to populate.

        Stacks the same persist-block mocks as the success-path tests
        so the new commit/push block doesn't execute against the real
        :func:`_commit_statefiles_to_worktree`.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)

        worktree_path = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_resolve_wt.return_value = worktree_path
        mock_populate.return_value = _populated()
        mock_commit.return_value = False
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        mock_spawner.return_value.gateway.push_worktree_branch.return_value = push_result

        client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        # Verify resolve_worktree_path was called correctly
        mock_resolve_wt.assert_called_once_with("issue-42", Path("/home/egg/repos/egg"))
        # Verify populate got the resolved worktree path, not the raw repo path
        mock_populate.assert_called_once()
        call_kwargs = mock_populate.call_args[1]
        assert call_kwargs["repo_path"] == worktree_path

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_returns_404_when_draft_missing(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """#2627 follow-up: draft-missing outcome maps to 404."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_populate.return_value = PopulateResult(PopulateOutcome.DRAFT_MISSING)

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "draft_missing" in data["message"]

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_returns_422_on_parse_failed(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """#2627 follow-up: parse-failed outcome maps to 422 (caller's plan,
        not server's fault)."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_populate.return_value = PopulateResult(PopulateOutcome.PARSE_FAILED)

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 422
        data = json.loads(resp.data)
        assert "parse_failed" in data["message"]

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_returns_422_on_empty_result(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """#2627 follow-up: empty-contract outcome maps to 422 — the
        populator ran but yielded nothing, so a 200 would have lied
        about success."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_populate.return_value = PopulateResult(PopulateOutcome.EMPTY_RESULT)

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 422
        data = json.loads(resp.data)
        assert "empty_result" in data["message"]

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_returns_500_on_contract_load_failed(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """#2627 follow-up: server-side load failure maps to 500."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_populate.return_value = PopulateResult(PopulateOutcome.CONTRACT_LOAD_FAILED)

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert "contract_load_failed" in data["message"]
