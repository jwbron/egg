"""Tests for short flow contract population from pre-generated plan.

When submit_task includes `analysis` and `plan` fields with
start_phase=implement, the orchestrator should write draft files and
populate the contract from the plan's yaml-tasks appendix.

See: https://github.com/jwbron/egg/issues/1350
"""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from models import Pipeline
from state_store import StateStore

# Sample plan with yaml-tasks appendix matching the format parse_plan expects
SAMPLE_PLAN = textwrap.dedent("""\
    # Plan: Add retry logic to API client

    ## Summary

    Add exponential backoff retry logic to the API client for transient failures.

    ## Implementation

    ### Phase 1: Implement

    Add retry_with_backoff() and integrate with existing request methods.

    ```yaml
    # yaml-tasks
    pr:
      title: "Add retry logic to API client"
      description: |
        Adds exponential backoff retry for transient HTTP errors.
    phases:
      - id: 1
        name: Implement
        goal: Add retry logic to the API client
        tasks:
          - id: TASK-1-1
            description: "Add retry_with_backoff() function to api_client.py"
            acceptance: "Function retries up to 3 times with exponential backoff"
            files:
              - src/api_client.py
          - id: TASK-1-2
            description: "Integrate retry logic into existing request methods"
            acceptance: "All HTTP methods use retry_with_backoff for 5xx errors"
            files:
              - src/api_client.py
    ```
""")

SAMPLE_ANALYSIS = textwrap.dedent("""\
    # Analysis: API Client Reliability

    ## Problem

    The API client does not retry on transient failures, causing unnecessary errors.

    ## Recommendation

    Add exponential backoff retry logic with configurable max attempts.
""")


class TestPipelineModelFields:
    """Pipeline model accepts analysis and plan fields."""

    def test_pipeline_with_analysis_and_plan(self):
        """Pipeline model stores analysis and plan fields."""
        pipeline = Pipeline(
            id="pipeline-test-1",
            repo="owner/repo",
            analysis=SAMPLE_ANALYSIS,
            plan=SAMPLE_PLAN,
        )
        assert pipeline.analysis == SAMPLE_ANALYSIS
        assert pipeline.plan == SAMPLE_PLAN

    def test_pipeline_without_analysis_and_plan(self):
        """Pipeline model defaults analysis and plan to None."""
        pipeline = Pipeline(
            id="pipeline-test-2",
            repo="owner/repo",
        )
        assert pipeline.analysis is None
        assert pipeline.plan is None


class TestPopulateContractFromPlan:
    """_populate_contract_from_plan() populates contract from plan draft."""

    def test_populates_phases_from_yaml_tasks(self, tmp_path: Path):
        """Contract gets phases and tasks from plan's yaml-tasks appendix."""
        from egg_contracts.loader import create_contract, load_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-short-abc"

        # Create blank contract
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        # Write plan draft
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        plan_path = drafts_dir / f"{pipeline_id}-plan.md"
        plan_path.write_text(SAMPLE_PLAN)

        # Populate contract from plan
        _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        # Verify contract was populated
        contract = load_contract(pipeline_id, tmp_path)
        assert len(contract.phases) == 1
        assert contract.phases[0].name == "Implement"
        assert len(contract.phases[0].tasks) == 2
        assert contract.phases[0].tasks[0].id == "task-1-1"
        assert contract.phases[0].tasks[1].id == "task-1-2"
        assert "retry_with_backoff" in contract.phases[0].tasks[0].description

    def test_populates_pr_metadata(self, tmp_path: Path):
        """Contract gets PR title and description from plan's yaml-tasks."""
        from egg_contracts.loader import create_contract, load_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-short-pr"

        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        contract = load_contract(pipeline_id, tmp_path)
        assert contract.pr is not None
        assert contract.pr.title == "Add retry logic to API client"
        assert "exponential backoff" in contract.pr.description

    def test_no_plan_draft_is_noop(self, tmp_path: Path):
        """No error when plan draft doesn't exist."""
        from egg_contracts.loader import create_contract, load_contract
        from routes.pipelines import _populate_contract_from_plan

        pipeline_id = "pipeline-no-plan"

        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)
        _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        contract = load_contract(pipeline_id, tmp_path)
        assert len(contract.phases) == 0  # Still empty


class TestEnsureStatefilesRestoresPRMetadata:
    """_ensure_statefiles_on_branch re-populates PR metadata from plan draft.

    When a contract file goes missing (e.g., push failure during init) and
    is recreated by the safety net, the planner-generated PR metadata must
    be restored from the plan draft still on disk.

    See: https://github.com/jwbron/egg/issues/1432
    """

    def test_restored_contract_has_pr_metadata(self, tmp_path: Path):
        """After _ensure_statefiles_on_branch, _build_pr_body uses plan PR metadata."""
        from routes.pipelines import _build_pr_body, _ensure_statefiles_on_branch

        pipeline_id = "pipeline-short-restore"

        # Write the plan draft (as if it was committed to git during init)
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text(SAMPLE_PLAN)

        # Do NOT create the contract file — simulate it being missing
        pipeline = MagicMock()
        pipeline.id = pipeline_id
        pipeline.issue_number = None
        pipeline.repo = "owner/repo"
        pipeline.prompt = "Test task"
        pipeline.mode = "local"

        with patch("routes.pipelines._commit_statefiles_to_worktree"):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

        # Now verify _build_pr_body picks up the PR metadata
        title, body = _build_pr_body(pipeline, tmp_path)

        assert title == "Add retry logic to API client"
        assert "exponential backoff" in body

    def test_restored_contract_with_issue_number_has_pr_metadata(self, tmp_path: Path):
        """Same as above but with issue_number-based contract identifier."""
        from routes.pipelines import _build_pr_body, _ensure_statefiles_on_branch

        issue_number = 99

        # Write the plan draft using issue_number-based name
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{issue_number}-plan.md").write_text(SAMPLE_PLAN)

        pipeline = MagicMock()
        pipeline.id = "pipeline-issue-99"
        pipeline.issue_number = issue_number
        pipeline.repo = "owner/repo"
        pipeline.prompt = "Test task"
        pipeline.mode = "local"

        with patch("routes.pipelines._commit_statefiles_to_worktree"):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

        title, body = _build_pr_body(pipeline, tmp_path)

        assert title == "Add retry logic to API client"
        assert "exponential backoff" in body


class TestEnsureStatefilesRestoresDraftFromRemote:
    """_ensure_statefiles_on_branch restores plan draft from remote branch.

    When both the contract and plan draft are missing from the worktree
    (e.g., after agent activity during implement phase), the restoration
    path should fetch the plan draft from the remote branch before
    re-populating the contract.

    See: https://github.com/jwbron/egg/issues/1454
    """

    def test_restores_plan_draft_from_remote(self, tmp_path: Path):
        """Plan draft is fetched from origin/{branch} when missing locally."""
        from routes.pipelines import _build_pr_body, _ensure_statefiles_on_branch

        pipeline_id = "pipeline-short-remote"

        # Do NOT write plan draft locally — it was lost from the worktree.
        # Simulate git show returning the plan from remote.
        pipeline = MagicMock()
        pipeline.id = pipeline_id
        pipeline.issue_number = None
        pipeline.repo = "owner/repo"
        pipeline.prompt = "Test task"
        pipeline.mode = "local"
        pipeline.branch = "egg/pipeline-short-remote"

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            if "show" in cmd:
                # Check the git show ref argument (e.g. "origin/branch:path")
                ref_arg = next((a for a in cmd if a.startswith("origin/")), "")
                if "-plan.md" in ref_arg:
                    result.returncode = 0
                    result.stdout = SAMPLE_PLAN
                else:
                    result.returncode = 1
                    result.stdout = ""
                return result
            # Default: success (for git fetch, git add, commit, etc.)
            result.returncode = 0
            result.stdout = ""
            return result

        with (
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.subprocess.run", side_effect=fake_subprocess_run),
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

        # Verify plan draft was written to disk
        plan_path = tmp_path / ".egg-state" / "drafts" / f"{pipeline_id}-plan.md"
        assert plan_path.exists()
        assert plan_path.read_text() == SAMPLE_PLAN

        # Verify contract has PR metadata from the restored plan
        title, body = _build_pr_body(pipeline, tmp_path)
        assert title == "Add retry logic to API client"
        assert "exponential backoff" in body

    def test_no_branch_skips_remote_restoration(self, tmp_path: Path):
        """When pipeline has no branch, draft restoration from remote is skipped."""
        from routes.pipelines import _ensure_statefiles_on_branch

        pipeline_id = "pipeline-no-branch"

        pipeline = MagicMock()
        pipeline.id = pipeline_id
        pipeline.issue_number = None
        pipeline.repo = "owner/repo"
        pipeline.prompt = "Test task"
        pipeline.mode = "local"
        pipeline.branch = None  # No branch

        with patch("routes.pipelines._commit_statefiles_to_worktree"):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

        # Plan draft should NOT exist (no remote to fetch from)
        plan_path = tmp_path / ".egg-state" / "drafts" / f"{pipeline_id}-plan.md"
        assert not plan_path.exists()

    def test_git_show_failure_is_non_fatal(self, tmp_path: Path):
        """When git show fails, restoration continues without the draft."""
        from routes.pipelines import _ensure_statefiles_on_branch

        pipeline_id = "pipeline-git-fail"

        pipeline = MagicMock()
        pipeline.id = pipeline_id
        pipeline.issue_number = None
        pipeline.repo = "owner/repo"
        pipeline.prompt = "Test task"
        pipeline.mode = "local"
        pipeline.branch = "egg/pipeline-git-fail"

        def failing_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            if "show" in cmd:
                result.returncode = 128  # git error
                result.stdout = ""
                return result
            result.returncode = 0
            result.stdout = ""
            return result

        with (
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.subprocess.run", side_effect=failing_subprocess_run),
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        # Should still succeed (contract created, just no plan metadata)
        assert result is True

    def test_restores_plan_draft_with_issue_number(self, tmp_path: Path):
        """Plan draft is restored using issue_number-based path when set."""
        from routes.pipelines import _build_pr_body, _ensure_statefiles_on_branch

        pipeline_id = "pipeline-issue-remote"
        issue_number = 42

        pipeline = MagicMock()
        pipeline.id = pipeline_id
        pipeline.issue_number = issue_number
        pipeline.repo = "owner/repo"
        pipeline.prompt = "Test task"
        pipeline.mode = "local"
        pipeline.branch = "egg/pipeline-issue-remote"

        def fake_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            if "show" in cmd:
                ref_arg = next((a for a in cmd if a.startswith("origin/")), "")
                if "-plan.md" in ref_arg:
                    result.returncode = 0
                    result.stdout = SAMPLE_PLAN
                else:
                    result.returncode = 1
                    result.stdout = ""
                return result
            result.returncode = 0
            result.stdout = ""
            return result

        with (
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.subprocess.run", side_effect=fake_subprocess_run),
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

        # Verify plan draft was written using issue_number as prefix
        plan_path = tmp_path / ".egg-state" / "drafts" / f"{issue_number}-plan.md"
        assert plan_path.exists()
        assert plan_path.read_text() == SAMPLE_PLAN

        # Verify contract has PR metadata from the restored plan
        title, body = _build_pr_body(pipeline, tmp_path)
        assert title == "Add retry logic to API client"
        assert "exponential backoff" in body


class TestMCPToolForwarding:
    """submit_task MCP tool forwards analysis and plan fields."""

    def test_handle_submit_task_forwards_fields(self):
        """_handle_submit_task includes analysis and plan in request data."""
        from mcp_tools import PipelineToolHandler

        handler = PipelineToolHandler.__new__(PipelineToolHandler)

        captured_data = {}
        call_count = 0

        def mock_request(path, method="GET", data=None, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                captured_data.update(data or {})
                return {"data": {"pipeline": {"id": "pipeline-test"}}}
            return {}

        handler._make_request = mock_request

        handler._handle_submit_task(
            {
                "description": "Test task",
                "repo": "owner/repo",
                "config": {"start_phase": "implement"},
                "analysis": SAMPLE_ANALYSIS,
                "plan": SAMPLE_PLAN,
            }
        )

        assert "analysis" in captured_data
        assert "plan" in captured_data
        assert captured_data["analysis"] == SAMPLE_ANALYSIS
        assert captured_data["plan"] == SAMPLE_PLAN

    def test_handle_submit_task_omits_empty_fields(self):
        """_handle_submit_task does not include analysis/plan when not provided."""
        from mcp_tools import PipelineToolHandler

        handler = PipelineToolHandler.__new__(PipelineToolHandler)

        captured_data = {}
        call_count = 0

        def mock_request(path, method="GET", data=None, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                captured_data.update(data or {})
                return {"data": {"pipeline": {"id": "pipeline-test"}}}
            return {}

        handler._make_request = mock_request

        handler._handle_submit_task(
            {
                "description": "Test task",
                "repo": "owner/repo",
            }
        )

        assert "analysis" not in captured_data
        assert "plan" not in captured_data


@pytest.fixture
def mock_git():
    """Mock git operations."""
    with patch.object(StateStore, "_run_git") as mock:
        mock.return_value = MagicMock(stdout="abc1234\n", returncode=0)
        yield mock


@pytest.fixture
def state_store(tmp_path, mock_git):
    """Create a state store with mocked git."""
    store = StateStore(tmp_path, worktree_dir=tmp_path)
    store._worktree = tmp_path
    return store


class TestStateStoreCreatePipeline:
    """State store accepts and stores analysis and plan fields."""

    def test_create_pipeline_with_plan(self, state_store):
        """create_pipeline stores analysis and plan on Pipeline model."""
        pipeline = state_store.create_pipeline(
            repo="owner/repo",
            prompt="Test task",
            analysis=SAMPLE_ANALYSIS,
            plan=SAMPLE_PLAN,
        )

        assert pipeline.analysis == SAMPLE_ANALYSIS
        assert pipeline.plan == SAMPLE_PLAN

        # Verify it persists through load
        loaded = state_store.load_pipeline(pipeline.id)
        assert loaded.analysis == SAMPLE_ANALYSIS
        assert loaded.plan == SAMPLE_PLAN

    def test_create_pipeline_without_plan(self, state_store):
        """create_pipeline works without analysis/plan (backwards compatible)."""
        pipeline = state_store.create_pipeline(
            repo="owner/repo",
            prompt="Test task",
        )

        assert pipeline.analysis is None
        assert pipeline.plan is None
