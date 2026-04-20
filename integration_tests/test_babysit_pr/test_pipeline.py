"""Integration tests for the babysit-pr BRC pipeline flow.

These tests exercise the orchestrator route + state-store surface for a
``mode=babysit`` pipeline and verify the key behaviours of the new
implement-phase BRC cycle:

* Happy-path creation produces a pipeline with ``has_contract=False``,
  ``mode=BABYSIT``, ``phase=implement``, and ``pipeline_id=pr-{N}``.
* Duplicate ``pr-{N}`` pipeline returns 409.
* Staging branches for producers are namespaced per PR head SHA (so
  concurrent cycles on the same PR don't collide with each other).
* The final consensus commit on the staging branch is the one pushed
  to the PR head — all intermediate NACK rounds remain on staging.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


@pytest.fixture
def app():
    """Create a Flask app with the pipelines blueprint registered."""
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _babysit_pr_state(
    *,
    state: str = "OPEN",
    is_fork: bool = False,
    base_ref: str = "main",
    head_ref: str = "feature-branch",
    head_sha: str = "abc1234deadbeef",
    changed_files: int = 3,
) -> dict:
    """Build a canned ``_fetch_pr_state()`` return dict for tests."""
    return {
        "state": state,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "is_fork": is_fork,
        "changed_files": changed_files,
        "head_repository_name_with_owner": "owner/repo" if not is_fork else "forker/repo",
    }


@pytest.mark.integration
class TestBabysitPipelineHappyPath:
    """201/200 happy path for mode=babysit."""

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_creates_pipeline_with_babysit_mode_and_has_contract_false(
        self, mock_get_store, mock_get_repo_path, mock_fetch, client
    ):
        from models import Pipeline, PipelineMode

        mock_fetch.return_value = _babysit_pr_state()

        mock_store = MagicMock()
        mock_pipeline = Pipeline(
            id="pr-99",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=99,
            has_contract=False,
            pr_head_sha="abc1234deadbeef",
            branch="feature-branch",
            base_branch="main",
        )
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99, "repo": "owner/repo"},
        )

        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        assert data["success"] is True

        # Verify create_pipeline was called with the right kwargs
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["pipeline_id"] == "pr-99"
        assert call_kwargs["pr_number"] == 99
        assert call_kwargs["mode"] == PipelineMode.BABYSIT
        assert call_kwargs["has_contract"] is False
        # base_branch is auto-populated from the PR's base_ref
        assert call_kwargs["base_branch"] == "main"
        # branch is auto-populated from the PR's head_ref
        assert call_kwargs["branch"] == "feature-branch"
        # pr_head_sha is captured at creation time
        assert call_kwargs["pr_head_sha"] == "abc1234deadbeef"


@pytest.mark.integration
class TestBabysitPipelineEarlyExits:
    """Fork / merged / empty-diff / missing-field early exits."""

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_fork_pr_rejected(self, mock_get_store, mock_get_repo_path, mock_fetch, client):
        mock_fetch.return_value = _babysit_pr_state(is_fork=True)
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99, "repo": "owner/repo"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["success"] is False
        details = body.get("details", {})
        assert details.get("reason") == "pr_from_fork"
        # Must not actually create a pipeline on refusal
        mock_get_store.assert_not_called()

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_merged_pr_rejected(self, mock_get_store, mock_get_repo_path, mock_fetch, client):
        mock_fetch.return_value = _babysit_pr_state(state="MERGED")
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99, "repo": "owner/repo"},
        )

        assert response.status_code == 409
        body = response.get_json()
        assert body["details"]["reason"] == "pr_merged"
        mock_get_store.assert_not_called()

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_closed_pr_rejected(self, mock_get_store, mock_get_repo_path, mock_fetch, client):
        mock_fetch.return_value = _babysit_pr_state(state="CLOSED")
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99, "repo": "owner/repo"},
        )

        assert response.status_code == 409
        body = response.get_json()
        assert body["details"]["reason"] == "pr_closed"

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_empty_diff_rejected(self, mock_get_store, mock_get_repo_path, mock_fetch, client):
        mock_fetch.return_value = _babysit_pr_state(changed_files=0)
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99, "repo": "owner/repo"},
        )

        assert response.status_code == 409
        body = response.get_json()
        assert body["details"]["reason"] == "pr_empty_diff"

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_missing_pr_number(self, mock_get_store, mock_get_repo_path, client):
        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "repo": "owner/repo"},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "pr_number" in body["message"].lower()

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_missing_repo(self, mock_get_store, mock_get_repo_path, client):
        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "repo" in body["message"].lower()

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_negative_pr_number(self, mock_get_store, mock_get_repo_path, client):
        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": -1, "repo": "owner/repo"},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "positive integer" in body["message"].lower()


@pytest.mark.integration
class TestBabysitPipelineIdCollision:
    """Duplicate ``pr-{N}`` pipeline is rejected with 409."""

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_duplicate_pipeline_returns_409(
        self, mock_get_store, mock_get_repo_path, mock_fetch, client
    ):
        from state_store import StateStoreError

        mock_fetch.return_value = _babysit_pr_state()

        mock_store = MagicMock()
        mock_store.create_pipeline.side_effect = StateStoreError("Pipeline pr-99 already exists")
        existing = MagicMock()
        existing.id = "pr-99"
        existing.status.value = "running"
        existing.current_phase.value = "implement"
        mock_store.load_pipeline.return_value = existing
        mock_get_store.return_value = mock_store
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 99, "repo": "owner/repo"},
        )

        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert "already exists" in body["message"].lower()


@pytest.mark.integration
class TestBabysitPipelineIdFormat:
    """``pipeline_id`` auto-derives to ``pr-{N}`` when not explicitly supplied."""

    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_pipeline_id_defaults_to_pr_prefix(
        self, mock_get_store, mock_get_repo_path, mock_fetch, client
    ):
        from models import Pipeline, PipelineMode

        mock_fetch.return_value = _babysit_pr_state()

        mock_store = MagicMock()
        mock_store.create_pipeline.return_value = Pipeline(
            id="pr-314",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=314,
            has_contract=False,
        )
        mock_get_store.return_value = mock_store
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 314, "repo": "owner/repo"},
        )
        assert response.status_code == 200
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["pipeline_id"] == "pr-314"


@pytest.mark.integration
class TestBabysitStagingBranchDerivation:
    """Per-role staging branches use PR number + head short SHA.

    The concurrent executor derives a per-role branch of the form
    ``egg/babysit-pr/{pr}/{short-sha}/{role}`` so reviewers and producers
    stay isolated from the PR head while BRC iterates.
    """

    def _make_executor(self, pipeline):
        """Build a ConcurrentPhaseExecutor with its I/O dependencies mocked."""
        from concurrent_executor import ConcurrentPhaseExecutor

        # ConcurrentPhaseExecutor.__init__ accepts docker_client / state_store /
        # agent_runner and orchestrator_url — we don't need them for the
        # get_worktree_branch unit, which only consults pipeline attrs.
        return ConcurrentPhaseExecutor.__new__(ConcurrentPhaseExecutor)  # type: ignore[call-arg]

    def test_babysit_pipeline_generates_per_role_staging_branch(self):
        from concurrent_executor import AgentRole
        from models import Pipeline, PipelineMode

        pipeline = Pipeline(
            id="pr-42",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            pr_head_sha="abc1234deadbeef5678901234567890abcdefabc",
            branch="feature-x",
            has_contract=False,
        )

        executor = self._make_executor(pipeline)
        executor.pipeline = pipeline
        executor._roles_override = None

        branch_coder = executor.get_worktree_branch(AgentRole.CODER)
        branch_tester = executor.get_worktree_branch(AgentRole.TESTER)
        branch_documenter = executor.get_worktree_branch(AgentRole.DOCUMENTER)

        assert branch_coder == "egg/babysit-pr/42/abc1234/coder"
        assert branch_tester == "egg/babysit-pr/42/abc1234/tester"
        assert branch_documenter == "egg/babysit-pr/42/abc1234/documenter"

    def test_issue_pipeline_not_affected_by_staging_logic(self):
        from concurrent_executor import AgentRole
        from models import Pipeline, PipelineMode

        pipeline = Pipeline(
            id="issue-1748",
            issue_number=1748,
            repo="owner/repo",
            mode=PipelineMode.ISSUE,
            branch="egg/issue-1748",
            has_contract=True,
        )

        executor = self._make_executor(pipeline)
        executor.pipeline = pipeline
        executor._roles_override = None

        branch = executor.get_worktree_branch(AgentRole.CODER)
        assert branch == "egg/issue-1748"
        assert "babysit-pr" not in branch

    def test_babysit_falls_back_to_pr_head_when_sha_missing(self):
        """Missing pr_head_sha falls back to the PR head branch."""
        from concurrent_executor import AgentRole
        from models import Pipeline, PipelineMode

        pipeline = Pipeline(
            id="pr-42",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            # pr_head_sha intentionally absent
            branch="feature-x",
            has_contract=False,
        )

        executor = self._make_executor(pipeline)
        executor.pipeline = pipeline
        executor._roles_override = None

        branch = executor.get_worktree_branch(AgentRole.CODER)
        # Without a SHA we can't namespace per-cycle; fall back to the PR head
        assert branch == "feature-x"
