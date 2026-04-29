"""Tests for source_branch parameter support (issue #1647).

Covers:
- Pipeline model accepts source_branch field
- source_branch threads through MCP → REST → StateStore → Pipeline
- _read_source_branch_artifacts reads plan/analysis from source branch
- Prefix fallback via git ls-tree when exact prefix doesn't match
- Inline plan/analysis values take precedence over source_branch
- Branch-exists check relaxation for terminal pipeline reuse
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_config import GATEWAY_PORT
from flask import Flask
from models import Pipeline, PipelineStatus
from state_store import StateStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_git():
    """Mock git operations for StateStore."""
    with patch.object(StateStore, "_run_git") as mock:
        mock.return_value = MagicMock(stdout="abc1234\n", returncode=0)
        yield mock


@pytest.fixture
def state_store(tmp_path, mock_git):
    """Create a state store for testing."""
    store = StateStore(tmp_path, worktree_dir=tmp_path)
    store._worktree = tmp_path
    return store


@pytest.fixture
def app():
    """Create a test Flask app with the pipelines blueprint."""
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. Pipeline model: source_branch field
# ---------------------------------------------------------------------------


class TestPipelineModelSourceBranch:
    """Test that the Pipeline model accepts and persists source_branch."""

    def test_source_branch_default_none(self):
        """source_branch should default to None when not provided."""
        pipeline = Pipeline(id="test-1", repo="owner/repo")
        assert pipeline.source_branch is None

    def test_source_branch_set(self):
        """source_branch should accept a string value."""
        pipeline = Pipeline(
            id="test-1",
            repo="owner/repo",
            source_branch="egg/issue-1570-v3",
        )
        assert pipeline.source_branch == "egg/issue-1570-v3"

    def test_source_branch_serialization(self):
        """source_branch should survive JSON round-trip via model_dump/model_validate."""
        pipeline = Pipeline(
            id="test-1",
            repo="owner/repo",
            source_branch="egg/issue-1570-v3",
        )
        data = pipeline.model_dump(mode="json")
        assert data["source_branch"] == "egg/issue-1570-v3"

        restored = Pipeline.model_validate(data)
        assert restored.source_branch == "egg/issue-1570-v3"

    def test_source_branch_none_serialization(self):
        """source_branch=None should serialize and restore correctly."""
        pipeline = Pipeline(id="test-1", repo="owner/repo")
        data = pipeline.model_dump(mode="json")
        restored = Pipeline.model_validate(data)
        assert restored.source_branch is None


# ---------------------------------------------------------------------------
# 2. StateStore: source_branch threading
# ---------------------------------------------------------------------------


class TestStateStoreSourceBranch:
    """Test source_branch passes through StateStore.create_pipeline."""

    def test_create_pipeline_with_source_branch(self, state_store):
        """source_branch should be stored on the Pipeline model."""
        pipeline = state_store.create_pipeline(
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
            source_branch="egg/issue-1570-v3",
        )
        assert pipeline.source_branch == "egg/issue-1570-v3"

    def test_create_pipeline_source_branch_persists(self, state_store):
        """source_branch should survive save → load cycle."""
        state_store.create_pipeline(
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
            source_branch="egg/issue-1570-v3",
        )
        loaded = state_store.load_pipeline("issue-1570")
        assert loaded.source_branch == "egg/issue-1570-v3"

    def test_create_pipeline_without_source_branch(self, state_store):
        """Omitting source_branch should default to None."""
        pipeline = state_store.create_pipeline(
            issue_number=1571,
            repo="owner/repo",
            branch="egg/issue-1571",
        )
        assert pipeline.source_branch is None


# ---------------------------------------------------------------------------
# 3. REST API: source_branch in create_pipeline
# ---------------------------------------------------------------------------


class TestCreatePipelineRESTSourceBranch:
    """Test source_branch passes through the REST /api/v1/pipelines endpoint."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_source_branch_forwarded_to_store(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """source_branch from the request body should reach store.create_pipeline."""
        mock_repo_path.return_value = tmp_path
        mock_store = MagicMock()
        mock_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
            source_branch="egg/issue-1570-v3",
        )
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_fn.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "source_branch": "egg/issue-1570-v3",
            },
        )

        assert response.status_code == 200
        # Verify source_branch was passed to store.create_pipeline
        call_kwargs = mock_store.create_pipeline.call_args
        assert call_kwargs.kwargs.get("source_branch") == "egg/issue-1570-v3"

    @pytest.mark.parametrize(
        "bad_branch",
        [
            "branch with spaces",
            "branch:ref",
            "branch..lock",
            "branch~1",
            "branch^2",
            "branch\\path",
            "../etc/passwd",
        ],
    )
    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_source_branch_invalid_returns_400(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path, bad_branch
    ):
        """Invalid source_branch values should be rejected with 400."""
        mock_repo_path.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "source_branch": bad_branch,
            },
        )

        assert response.status_code == 400

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_source_branch_omitted_defaults_none(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """Omitting source_branch should result in None being passed."""
        mock_repo_path.return_value = tmp_path
        mock_store = MagicMock()
        mock_pipeline = Pipeline(
            id="issue-1572",
            issue_number=1572,
            repo="owner/repo",
            branch="egg/issue-1572",
        )
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_fn.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1572,
                "repo": "owner/repo",
                "branch": "egg/issue-1572",
            },
        )

        assert response.status_code == 200
        call_kwargs = mock_store.create_pipeline.call_args
        assert call_kwargs.kwargs.get("source_branch") is None

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_source_branch_in_response_body(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """source_branch should appear in the pipeline data returned in the response."""
        mock_repo_path.return_value = tmp_path
        mock_store = MagicMock()
        mock_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
            source_branch="egg/issue-1570-v3",
        )
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_fn.return_value = mock_gw

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "source_branch": "egg/issue-1570-v3",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        pipeline_data = data["data"]["pipeline"]
        assert pipeline_data["source_branch"] == "egg/issue-1570-v3"


# ---------------------------------------------------------------------------
# 4. MCP tool: source_branch in submit_task
# ---------------------------------------------------------------------------


class TestMCPSubmitTaskSourceBranch:
    """Test source_branch parameter in the submit_task MCP tool."""

    @pytest.fixture
    def handler(self):
        from mcp_tools import PipelineToolHandler

        return PipelineToolHandler(
            orchestrator_url="http://localhost:9849",
            gateway_url=f"http://test-gateway:{GATEWAY_PORT}",
        )

    @patch("urllib.request.build_opener")
    def test_source_branch_included_in_request(self, mock_build_opener, handler):
        """source_branch should be forwarded in the POST body to the API."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"data": {"pipeline": {"id": "issue-1570"}}}
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response
        mock_build_opener.return_value = mock_opener

        handler.handle_tool_call(
            "submit_task",
            {
                "description": "Implement feature X",
                "repo": "owner/repo",
                "issue_number": 1570,
                "source_branch": "egg/issue-1570-v3",
            },
        )

        # Inspect the POST body sent to the API
        call_args = mock_opener.open.call_args_list[0]
        request_obj = call_args[0][0]
        body = json.loads(request_obj.data)
        assert body.get("source_branch") == "egg/issue-1570-v3"

    def test_source_branch_in_tool_schema(self):
        """submit_task tool schema should include source_branch property."""
        from mcp_tools import PIPELINE_TOOLS

        submit_tool = next(t for t in PIPELINE_TOOLS if t["name"] == "submit_task")
        props = submit_tool["inputSchema"]["properties"]
        assert "source_branch" in props, "source_branch missing from submit_task schema"
        assert props["source_branch"]["type"] == "string"

    @patch("urllib.request.build_opener")
    def test_source_branch_omitted_not_in_body(self, mock_build_opener, handler):
        """When source_branch is not provided, it should not appear in the POST body."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"data": {"pipeline": {"id": "issue-1573"}}}
        ).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response
        mock_build_opener.return_value = mock_opener

        handler.handle_tool_call(
            "submit_task",
            {
                "description": "Implement feature X",
                "repo": "owner/repo",
                "issue_number": 1573,
            },
        )

        call_args = mock_opener.open.call_args_list[0]
        request_obj = call_args[0][0]
        body = json.loads(request_obj.data)
        assert "source_branch" not in body


# ---------------------------------------------------------------------------
# 5. _read_source_branch_artifacts
# ---------------------------------------------------------------------------


class TestReadSourceBranchArtifacts:
    """Test the _read_source_branch_artifacts helper in routes/pipelines.py."""

    @pytest.fixture
    def worktree_path(self, tmp_path):
        """Create a temporary worktree path."""
        return tmp_path / "worktree"

    def _make_pipeline(
        self, source_branch: str | None = "egg/issue-1570-v3", **kwargs: object
    ) -> Pipeline:
        """Create a Pipeline with source_branch set."""
        defaults: dict[str, object] = {
            "id": "issue-1570",
            "issue_number": 1570,
            "repo": "owner/repo",
            "source_branch": source_branch,
        }
        defaults.update(kwargs)
        return Pipeline(**defaults)

    @patch("routes.pipelines._git_show_draft")
    def test_reads_plan_and_analysis(self, mock_git_show, worktree_path):
        """Should read plan and analysis from source branch via git show."""
        from routes.pipelines import _read_source_branch_artifacts

        plan_content = "# Plan\n## Tasks\n..."
        analysis_content = "# Analysis\n## Problem\n..."

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            if "plan.md" in rel_path:
                return plan_content
            elif "analysis.md" in rel_path:
                return analysis_content
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.plan == plan_content
        assert pipeline.analysis == analysis_content
        # source_branch should be cleared after successful artifact read
        assert pipeline.source_branch is None
        mock_store.save_pipeline.assert_called_once()

    @patch("routes.pipelines._git_show_draft")
    def test_inline_plan_takes_precedence(self, mock_git_show, worktree_path):
        """Inline plan/analysis should not be overwritten by source_branch artifacts."""
        from routes.pipelines import _read_source_branch_artifacts

        pipeline = self._make_pipeline(plan="inline plan", analysis="inline analysis")
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is False
        assert pipeline.plan == "inline plan"
        assert pipeline.analysis == "inline analysis"
        # Should not call git show if both fields are set
        mock_git_show.assert_not_called()
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.pipelines._git_show_draft")
    def test_inline_plan_partial_precedence(self, mock_git_show, worktree_path):
        """Only missing fields should be populated from source_branch."""
        from routes.pipelines import _read_source_branch_artifacts

        analysis_content = "# Analysis from branch"

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            if "analysis.md" in rel_path:
                return analysis_content
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline(plan="inline plan")
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.plan == "inline plan"  # preserved
        assert pipeline.analysis == analysis_content  # populated from branch
        mock_store.save_pipeline.assert_called_once()

    @patch("subprocess.run")
    @patch("routes.pipelines._git_show_draft")
    def test_prefix_fallback_via_ls_tree(self, mock_git_show, mock_run, worktree_path):
        """When exact prefix doesn't match, should fallback to ls-tree search."""
        from routes.pipelines import _read_source_branch_artifacts

        plan_content = "# Fallback plan"

        # Exact-path lookup uses "1570-analysis.md" / "1570-plan.md" → miss.
        # ls-tree fallback finds differently-named files that still match
        # the issue number and suffix (e.g. "1570-v2-analysis.md").

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            if "1570-v2-plan.md" in rel_path:
                return plan_content
            if "1570-v2-analysis.md" in rel_path:
                return "# Fallback analysis"
            return None

        mock_git_show.side_effect = fake_git_show

        # ls-tree returns files with a different naming convention
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="1570-v2-plan.md\n1570-v2-analysis.md\n", stderr=""
        )

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.plan == plan_content
        assert pipeline.analysis == "# Fallback analysis"
        # Verify ls-tree was called
        assert mock_run.called

    @patch("routes.pipelines._git_show_draft")
    def test_missing_source_branch_ref(self, mock_git_show, worktree_path):
        """Should handle missing source branch gracefully (no crash)."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = None

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        # Patch subprocess.run for ls-tree fallback
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 128, stdout="", stderr="fatal: invalid object name"
            )

            result = _read_source_branch_artifacts(
                repo_path=worktree_path,
                source_branch="egg/issue-1570-v3",
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
                store=mock_store,
                pipeline=pipeline,
            )

        assert result is False
        assert pipeline.plan is None
        assert pipeline.analysis is None
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.pipelines._git_show_draft")
    def test_returns_false_when_no_artifacts_found(self, mock_git_show, worktree_path):
        """Should return False when no artifacts could be read."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = None

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 128, stdout="", stderr="not found"
            )

            result = _read_source_branch_artifacts(
                repo_path=worktree_path,
                source_branch="egg/issue-1570-v3",
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
                store=mock_store,
                pipeline=pipeline,
            )

        assert result is False
        # source_branch should be preserved when no artifacts were found
        assert pipeline.source_branch == "egg/issue-1570-v3"
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.pipelines._git_show_draft")
    def test_only_plan_found(self, mock_git_show, worktree_path):
        """When only plan exists on source branch, only plan should be populated."""
        from routes.pipelines import _read_source_branch_artifacts

        plan_content = "# Plan only"

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            if "plan.md" in rel_path:
                return plan_content
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 128, stdout="", stderr="")

            result = _read_source_branch_artifacts(
                repo_path=worktree_path,
                source_branch="egg/issue-1570-v3",
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
                store=mock_store,
                pipeline=pipeline,
            )

        assert result is True
        assert pipeline.plan == plan_content
        assert pipeline.analysis is None
        mock_store.save_pipeline.assert_called_once()

    @patch("subprocess.run")
    @patch("routes.pipelines._git_show_draft")
    def test_fallback_filters_by_issue_number(self, mock_git_show, mock_run, worktree_path):
        """Fallback should filter matches by issue number when multiple files exist (#1654)."""
        from routes.pipelines import _read_source_branch_artifacts

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            # Exact-path attempts use the pipeline prefix (issue-1570/...) → miss.
            # Fallback attempts use the bare filename (1570-analysis.md) → hit.
            if "1570-analysis.md" in rel_path and not rel_path.startswith(
                ".egg-state/drafts/issue-"
            ):
                return "# Correct analysis for 1570"
            if "1570-plan.md" in rel_path and not rel_path.startswith(".egg-state/drafts/issue-"):
                return "# Correct plan for 1570"
            return None

        mock_git_show.side_effect = fake_git_show

        # ls-tree returns files from many issues (the bug scenario)
        mock_run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "1014-analysis.md\n1027-analysis.md\n1570-analysis.md\n"
                "1014-plan.md\n1027-plan.md\n1570-plan.md\n"
            ),
            stderr="",
        )

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.analysis == "# Correct analysis for 1570"
        assert pipeline.plan == "# Correct plan for 1570"

    @patch("subprocess.run")
    @patch("routes.pipelines._git_show_draft")
    def test_fallback_skips_when_no_issue_match(self, mock_git_show, mock_run, worktree_path):
        """When fallback finds no file for the issue number, should skip (not use wrong issue)."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = None

        # ls-tree returns files from other issues only — none for 1570
        mock_run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout="1014-analysis.md\n1027-analysis.md\n1014-plan.md\n1027-plan.md\n",
            stderr="",
        )

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is False
        assert pipeline.analysis is None
        assert pipeline.plan is None

    @patch("subprocess.run")
    @patch("routes.pipelines._git_show_draft")
    def test_fallback_single_file_wrong_issue_skips(self, mock_git_show, mock_run, worktree_path):
        """When ls-tree returns exactly one file from a different issue, should skip it (#1654)."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = None

        # ls-tree returns a single file, but it belongs to a different issue
        mock_run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout="1014-analysis.md\n1014-plan.md\n",
            stderr="",
        )

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is False
        assert pipeline.analysis is None
        assert pipeline.plan is None

    @patch("subprocess.run")
    @patch("routes.pipelines._git_show_draft")
    def test_fallback_no_issue_number_uses_first(self, mock_git_show, mock_run, worktree_path):
        """When issue_number is None, fallback should use first match (existing behavior)."""
        from routes.pipelines import _read_source_branch_artifacts

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            if "1014-analysis.md" in rel_path:
                return "# Analysis from 1014"
            if "1014-plan.md" in rel_path:
                return "# Plan from 1014"
            return None

        mock_git_show.side_effect = fake_git_show

        mock_run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout="1014-analysis.md\n1027-analysis.md\n1014-plan.md\n1027-plan.md\n",
            stderr="",
        )

        pipeline = self._make_pipeline(issue_number=None)
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=None,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.analysis == "# Analysis from 1014"
        assert pipeline.plan == "# Plan from 1014"

    @patch("routes.pipelines._git_show_draft")
    def test_pipeline_id_prefix_tried_before_issue_number(self, mock_git_show, worktree_path):
        """When pipeline_id differs from issue number, try pipeline_id prefix first."""
        from routes.pipelines import _read_source_branch_artifacts

        # Track which paths are attempted
        attempted_paths = []

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            attempted_paths.append(rel_path)
            if "issue-1570-v7-plan.md" in rel_path:
                return "# Plan from v7 prefix"
            if "issue-1570-v7-analysis.md" in rel_path:
                return "# Analysis from v7 prefix"
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline(id="issue-1570-v7")
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.analysis == "# Analysis from v7 prefix"
        assert pipeline.plan == "# Plan from v7 prefix"
        # pipeline_id prefix should be tried first
        assert attempted_paths[0] == ".egg-state/drafts/issue-1570-v7-analysis.md"

    @patch("routes.pipelines._git_show_draft")
    def test_falls_back_to_issue_number_prefix(self, mock_git_show, worktree_path):
        """When pipeline_id prefix misses, should try bare issue number prefix."""
        from routes.pipelines import _read_source_branch_artifacts

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            # pipeline_id prefix misses, bare issue number hits
            if "1570-plan.md" in rel_path and "issue-1570-v7" not in rel_path:
                return "# Plan from bare prefix"
            if "1570-analysis.md" in rel_path and "issue-1570-v7" not in rel_path:
                return "# Analysis from bare prefix"
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline(id="issue-1570-v7")
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
        )

        assert result is True
        assert pipeline.analysis == "# Analysis from bare prefix"
        assert pipeline.plan == "# Plan from bare prefix"

    @patch("routes.pipelines._git_show_draft")
    def test_source_artifact_prefix_override(self, mock_git_show, worktree_path):
        """Explicit source_artifact_prefix should override all default prefix logic."""
        from routes.pipelines import _read_source_branch_artifacts

        attempted_paths = []

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            attempted_paths.append(rel_path)
            if "issue-1570-v3-plan.md" in rel_path:
                return "# Plan from v3 override"
            if "issue-1570-v3-analysis.md" in rel_path:
                return "# Analysis from v3 override"
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline(id="issue-1570-v7")
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
            source_artifact_prefix="issue-1570-v3",
        )

        assert result is True
        assert pipeline.analysis == "# Analysis from v3 override"
        assert pipeline.plan == "# Plan from v3 override"
        # Only the override prefix should be tried (no pipeline_id or issue number)
        for path in attempted_paths:
            assert "issue-1570-v7" not in path
            assert path.startswith(".egg-state/drafts/issue-1570-v3-")

    @patch("routes.pipelines._git_show_draft")
    def test_source_artifact_prefix_clears_on_success(self, mock_git_show, worktree_path):
        """source_artifact_prefix should be cleared alongside source_branch on success."""
        from routes.pipelines import _read_source_branch_artifacts

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            if "plan.md" in rel_path:
                return "# Plan"
            if "analysis.md" in rel_path:
                return "# Analysis"
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = self._make_pipeline(id="issue-1570-v7", source_artifact_prefix="issue-1570-v3")
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
            source_artifact_prefix="issue-1570-v3",
        )

        assert result is True
        assert pipeline.source_branch is None
        assert pipeline.source_artifact_prefix is None

    @patch("routes.pipelines._git_show_draft")
    def test_uses_gateway_fetch_when_spawner_provided(self, mock_git_show, worktree_path):
        """Should use gateway.fetch_branch() instead of raw git fetch when spawner is provided."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = "# Plan content"

        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_spawner = MagicMock()
        mock_spawner.gateway.fetch_branch.return_value = True

        result = _read_source_branch_artifacts(
            repo_path=worktree_path,
            source_branch="egg/issue-1570-v3",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
            store=mock_store,
            pipeline=pipeline,
            spawner=mock_spawner,
            gateway_mode="public",
        )

        assert result is True
        mock_spawner.gateway.fetch_branch.assert_called_once_with(
            pipeline_id=pipeline.id,
            repo_path=str(worktree_path),
            args=["egg/issue-1570-v3"],
            mode="public",
        )

    @patch("routes.pipelines._git_show_draft")
    def test_logs_warning_when_no_artifacts_found(self, mock_git_show, worktree_path):
        """Should log a WARNING when no artifacts are found on the source branch."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = None

        pipeline = self._make_pipeline()
        mock_store = MagicMock()

        with patch("routes.pipelines.logger") as mock_logger:
            result = _read_source_branch_artifacts(
                repo_path=worktree_path,
                source_branch="egg/issue-1570-v3",
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
                store=mock_store,
                pipeline=pipeline,
            )

        assert result is False
        # source_branch should NOT be cleared when no artifacts found
        assert pipeline.source_branch == "egg/issue-1570-v3"
        # Verify the warning was actually logged
        mock_logger.warning.assert_any_call(
            "No artifacts found on source branch",
            source_branch="egg/issue-1570-v3",
            pipeline_id=pipeline.id,
            source_artifact_prefix=None,
        )


# ---------------------------------------------------------------------------
# 6. Branch-exists relaxation
# ---------------------------------------------------------------------------


class TestBranchExistsRelaxation:
    """Test that create_pipeline allows branch reuse when pipeline is terminal."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_exists_active_pipeline_returns_409(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """Should return 409 when branch exists AND an active pipeline exists."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        # Mock store showing an active pipeline exists
        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        existing_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            status=PipelineStatus.RUNNING,
        )
        mock_store.load_pipeline.return_value = existing_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "pipeline_id": "issue-1570",
            },
        )

        assert response.status_code == 409

    @pytest.mark.parametrize(
        "active_status",
        [
            PipelineStatus.RUNNING,
            PipelineStatus.AWAITING_HUMAN,
            PipelineStatus.PENDING,
        ],
    )
    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_exists_all_active_statuses_return_409(
        self,
        mock_repo_path,
        mock_get_store,
        mock_gw_fn,
        client,
        tmp_path,
        active_status,
    ):
        """All non-terminal statuses should trigger 409 when branch exists."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        existing_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            status=active_status,
        )
        mock_store.load_pipeline.return_value = existing_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "pipeline_id": "issue-1570",
            },
        )

        assert response.status_code == 409

    @pytest.mark.parametrize(
        "terminal_status",
        [
            PipelineStatus.CANCELLED,
            PipelineStatus.FAILED,
            PipelineStatus.COMPLETE,
        ],
    )
    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_exists_terminal_pipeline_allows_reuse(
        self,
        mock_repo_path,
        mock_get_store,
        mock_gw_fn,
        client,
        tmp_path,
        terminal_status,
    ):
        """Should allow creation when branch exists but pipeline is terminal."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        # Mock store: first call for branch-exists check, second for create_pipeline
        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        existing_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            status=terminal_status,
        )
        mock_store.load_pipeline.return_value = existing_pipeline

        # create_pipeline should succeed since the existing pipeline is terminal
        new_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
        )
        mock_store.create_pipeline.return_value = new_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "pipeline_id": "issue-1570",
            },
        )

        assert response.status_code == 200

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_exists_no_pipeline_allows_reuse(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """Should allow creation when branch exists but no pipeline state found."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        # Mock store showing no pipeline exists
        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = False

        new_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
        )
        mock_store.create_pipeline.return_value = new_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "pipeline_id": "issue-1570",
            },
        )

        assert response.status_code == 200

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_does_not_exist_proceeds_normally(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """When branch doesn't exist on remote, should proceed with creation."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = False
        mock_gw_fn.return_value = mock_gw

        mock_store = MagicMock()
        new_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
        )
        mock_store.create_pipeline.return_value = new_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
            },
        )

        assert response.status_code == 200
        # pipeline_exists should NOT be checked when branch doesn't exist
        mock_store.pipeline_exists.assert_not_called()

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_branch_exists_no_pipeline_id_skips_pipeline_check(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """When no pipeline_id is provided and branch exists, should skip
        pipeline existence check (since we don't know what pipeline to look up)."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        mock_store = MagicMock()
        new_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
        )
        mock_store.create_pipeline.return_value = new_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                # No pipeline_id — auto-generated
            },
        )

        # Without pipeline_id in request, the branch-exists check should
        # allow reuse since there's no pipeline to check against
        # (pipeline_id is None at that point in the code)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 7. Integration: source_branch with branch-exists relaxation
# ---------------------------------------------------------------------------


class TestSourceBranchWithBranchReuse:
    """Combined scenario: resubmit with source_branch pointing to prior run."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_resubmit_with_source_branch_after_cancellation(
        self, mock_repo_path, mock_get_store, mock_gw_fn, client, tmp_path
    ):
        """Resubmitting after cancel with source_branch should work end-to-end."""
        mock_repo_path.return_value = tmp_path

        mock_gw = MagicMock()
        mock_gw.ls_remote_branch.return_value = True  # Branch exists from prior run
        mock_gw_fn.return_value = mock_gw

        # Prior pipeline is cancelled
        mock_store = MagicMock()
        mock_store.pipeline_exists.return_value = True
        cancelled_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            status=PipelineStatus.CANCELLED,
        )
        mock_store.load_pipeline.return_value = cancelled_pipeline

        new_pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            branch="egg/issue-1570",
            source_branch="egg/issue-1570-v3",
        )
        mock_store.create_pipeline.return_value = new_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 1570,
                "repo": "owner/repo",
                "branch": "egg/issue-1570",
                "pipeline_id": "issue-1570",
                "source_branch": "egg/issue-1570-v3",
                "config": {"start_phase": "implement"},
            },
        )

        assert response.status_code == 200
        call_kwargs = mock_store.create_pipeline.call_args
        assert call_kwargs.kwargs.get("source_branch") == "egg/issue-1570-v3"


# ---------------------------------------------------------------------------
# 8. Edge cases and error handling
# ---------------------------------------------------------------------------


class TestSourceBranchEdgeCases:
    """Edge cases and error handling for source_branch."""

    @patch("routes.pipelines._git_show_draft")
    def test_empty_string_field_not_overwritten(self, mock_git_show, tmp_path):
        """An empty-string field value should not be overwritten by source artifacts.

        Uses ``is not None`` so that empty strings are treated as explicitly set.
        """
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = "# From source branch"

        pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            source_branch="egg/issue-1570-v3",
            plan="",  # explicitly set to empty string
            analysis="",
        )
        mock_store = MagicMock()

        result = _read_source_branch_artifacts(
            repo_path=tmp_path,
            source_branch="egg/issue-1570-v3",
            issue_number=1570,
            pipeline_id="issue-1570",
            store=mock_store,
            pipeline=pipeline,
        )

        # Empty strings are not None, so they should not be overwritten
        assert result is False
        assert pipeline.plan == ""
        assert pipeline.analysis == ""
        mock_git_show.assert_not_called()

    @patch("routes.pipelines._git_show_draft")
    def test_empty_string_content_not_treated_as_artifact(self, mock_git_show, tmp_path):
        """Empty string from git show should not be set as artifact content."""
        from routes.pipelines import _read_source_branch_artifacts

        mock_git_show.return_value = ""

        pipeline = Pipeline(
            id="issue-1570",
            issue_number=1570,
            repo="owner/repo",
            source_branch="egg/issue-1570-v3",
        )
        mock_store = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 128, stdout="", stderr="")

            result = _read_source_branch_artifacts(
                repo_path=tmp_path,
                source_branch="egg/issue-1570-v3",
                issue_number=1570,
                pipeline_id="issue-1570",
                store=mock_store,
                pipeline=pipeline,
            )

        # Empty string should be treated as no content (truthy check in code)
        assert result is False

    @patch("routes.pipelines._git_show_draft")
    def test_pipeline_id_without_issue_number_uses_pipeline_id_prefix(
        self, mock_git_show, tmp_path
    ):
        """When issue_number is None, pipeline_id should be used as the prefix."""
        from routes.pipelines import _read_source_branch_artifacts

        plan_content = "# Plan for custom pipeline"

        def fake_git_show(repo_path, branch, rel_path, timeout=15):
            # Should use pipeline_id as prefix since issue_number is None
            if "custom-pipeline-plan.md" in rel_path:
                return plan_content
            return None

        mock_git_show.side_effect = fake_git_show

        pipeline = Pipeline(
            id="custom-pipeline",
            repo="owner/repo",
            source_branch="egg/prior-run",
        )
        mock_store = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 128, stdout="", stderr="")

            result = _read_source_branch_artifacts(
                repo_path=tmp_path,
                source_branch="egg/prior-run",
                issue_number=None,
                pipeline_id="custom-pipeline",
                store=mock_store,
                pipeline=pipeline,
            )

        assert result is True
        assert pipeline.plan == plan_content


# ---------------------------------------------------------------------------
# 6. _pull_contract_from_source_branch (#2035)
# ---------------------------------------------------------------------------


class TestPullContractFromSourceBranch:
    """Test that _pull_contract_from_source_branch carries over resolved
    HITL decisions from ``origin/<source_branch>`` instead of letting
    ``_run_pipeline`` overwrite them with a fresh zero-state contract.
    """

    def _make_contract(self, pipeline_id="issue-1570", decisions=None, phases=None):
        """Build a Contract with the given decisions/phases preserved."""
        from egg_contracts.models import Contract, IssueInfo

        issue_number = None
        if pipeline_id.startswith("issue-"):
            try:
                issue_number = int(pipeline_id.split("-")[1])
            except IndexError, ValueError:
                pass

        return Contract(
            issue=IssueInfo(number=issue_number, title="t", url="") if issue_number else None,
            pipeline_id=pipeline_id,
            decisions=decisions or [],
            phases=phases or [],
        )

    def test_pulls_contract_with_resolved_decisions(self, tmp_path):
        """Contract with resolved HITL decisions should be loaded and saved."""
        from egg_contracts.models import Decision, DecisionType
        from routes.pipelines import _pull_contract_from_source_branch

        source_contract = self._make_contract(
            pipeline_id="issue-1570",
            decisions=[
                Decision(
                    id="decision-1",
                    question="Which DB driver?",
                    type=DecisionType.HITL,
                    options=[],
                    resolved=True,
                    resolution="asyncpg",
                    resolved_by="human",
                )
            ],
        )

        saved = {}

        def fake_load(identifier, repo_path, branch=None):
            assert identifier == 1570
            assert branch == "origin/egg/issue-1570-v3"
            return source_contract

        def fake_save(contract, repo_root):
            saved["contract"] = contract
            saved["repo_root"] = repo_root
            return repo_root / "contract.json"

        with (
            patch("subprocess.run") as mock_subprocess,
            patch("egg_contracts.loader.load_contract_from_branch", side_effect=fake_load),
            patch("egg_contracts.loader.save_contract", side_effect=fake_save),
        ):
            mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            result = _pull_contract_from_source_branch(
                repo_path=tmp_path,
                source_branch="egg/issue-1570-v3",
                issue_number=1570,
                pipeline_id="issue-1570",
            )

        assert result is True
        assert "contract" in saved
        # The pulled contract's decisions must survive the save.
        assert len(saved["contract"].decisions) == 1
        assert saved["contract"].decisions[0].resolved is True
        assert saved["contract"].decisions[0].resolution == "asyncpg"

    def test_rebinds_pipeline_id_when_forking(self, tmp_path):
        """When forking (source pipeline_id != new pipeline_id), save under the NEW id."""
        from routes.pipelines import _pull_contract_from_source_branch

        source_contract = self._make_contract(pipeline_id="issue-1570")

        saved = {}

        def fake_load(identifier, repo_path, branch=None):
            assert identifier == 1570
            return source_contract

        with (
            patch("subprocess.run") as mock_subprocess,
            patch(
                "egg_contracts.loader.load_contract_from_branch",
                side_effect=fake_load,
            ),
            patch("egg_contracts.loader.save_contract") as mock_save,
        ):
            mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            mock_save.side_effect = lambda c, r: saved.setdefault("contract", c)
            result = _pull_contract_from_source_branch(
                repo_path=tmp_path,
                source_branch="egg/issue-1570",
                issue_number=1570,
                pipeline_id="issue-1570-v2",
            )

        assert result is True
        # Saved contract must have been rebound to the new pipeline_id so
        # save_contract writes under the canonical key for the new pipeline.
        assert saved["contract"].pipeline_id == "issue-1570-v2"

    def test_returns_false_when_no_contract_on_branch(self, tmp_path):
        """Missing contract on source branch should yield False (not raise)."""
        from egg_contracts.loader import ContractNotFoundError
        from routes.pipelines import _pull_contract_from_source_branch

        with (
            patch("subprocess.run") as mock_subprocess,
            patch(
                "egg_contracts.loader.load_contract_from_branch",
                side_effect=ContractNotFoundError(1570, tmp_path / "missing.json"),
            ),
        ):
            mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            result = _pull_contract_from_source_branch(
                repo_path=tmp_path,
                source_branch="egg/issue-1570-v3",
                issue_number=1570,
                pipeline_id="issue-1570",
            )

        assert result is False

    def test_returns_false_on_invalid_contract(self, tmp_path):
        """Invalid contract on source branch should yield False (best-effort)."""
        from egg_contracts.loader import ContractValidationError
        from routes.pipelines import _pull_contract_from_source_branch

        with (
            patch("subprocess.run") as mock_subprocess,
            patch(
                "egg_contracts.loader.load_contract_from_branch",
                side_effect=ContractValidationError(1570, ["Invalid JSON: x"]),
            ),
        ):
            mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            result = _pull_contract_from_source_branch(
                repo_path=tmp_path,
                source_branch="egg/issue-1570-v3",
                issue_number=1570,
                pipeline_id="issue-1570",
            )

        assert result is False

    def test_uses_gateway_fetch_when_spawner_present(self, tmp_path):
        """When spawner is provided, fetch must go through the gateway (credentialed)."""
        from routes.pipelines import _pull_contract_from_source_branch

        mock_spawner = MagicMock()
        mock_spawner.gateway.fetch_branch = MagicMock()

        with (
            patch(
                "egg_contracts.loader.load_contract_from_branch",
                return_value=self._make_contract(),
            ),
            patch("egg_contracts.loader.save_contract"),
        ):
            _pull_contract_from_source_branch(
                repo_path=tmp_path,
                source_branch="egg/issue-1570-v3",
                issue_number=1570,
                pipeline_id="issue-1570",
                spawner=mock_spawner,
                gateway_mode="private",
            )

        mock_spawner.gateway.fetch_branch.assert_called_once()
        call_kwargs = mock_spawner.gateway.fetch_branch.call_args.kwargs
        assert call_kwargs["args"] == ["egg/issue-1570-v3"]
        assert call_kwargs["mode"] == "private"

    def test_falls_back_to_pipeline_id_when_no_issue_number(self, tmp_path):
        """When issue_number is None, identifier should fall back to pipeline_id."""
        from routes.pipelines import _pull_contract_from_source_branch

        source_contract = self._make_contract(pipeline_id="run-abc123")

        def fake_load(identifier, repo_path, branch=None):
            assert identifier == "run-abc123"
            return source_contract

        saved = {}

        with (
            patch("subprocess.run") as mock_subprocess,
            patch(
                "egg_contracts.loader.load_contract_from_branch",
                side_effect=fake_load,
            ),
            patch("egg_contracts.loader.save_contract") as mock_save,
        ):
            mock_subprocess.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            mock_save.side_effect = lambda c, r: saved.setdefault("contract", c)
            result = _pull_contract_from_source_branch(
                repo_path=tmp_path,
                source_branch="egg/run-abc123",
                issue_number=None,
                pipeline_id="run-abc123",
            )

        assert result is True
        assert saved["contract"].pipeline_id == "run-abc123"
