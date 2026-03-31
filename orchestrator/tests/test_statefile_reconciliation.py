"""Tests for statefile reconciliation: _ensure_statefiles_on_branch."""

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes.pipelines import _ensure_statefiles_on_branch

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
    ```
""")

SAMPLE_ANALYSIS = textwrap.dedent("""\
    # Analysis: API Client Reliability

    ## Problem

    The API client does not retry on transient failures, causing unnecessary errors.

    ## Recommendation

    Add exponential backoff retry logic with configurable max attempts.
""")


def _make_pipeline(
    pipeline_id: str = "pipe-1",
    issue_number: int | None = 42,
    repo: str = "owner/repo",
    prompt: str = "test prompt",
    mode: str | None = None,
    branch: str | None = None,
    plan: str | None = None,
    analysis: str | None = None,
) -> MagicMock:
    """Create a mock Pipeline object."""
    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.issue_number = issue_number
    pipeline.repo = repo
    pipeline.prompt = prompt
    pipeline.mode = mode
    pipeline.branch = branch
    pipeline.plan = plan
    pipeline.analysis = analysis
    return pipeline


class TestEnsureStatefilesOnBranch:
    """Tests for _ensure_statefiles_on_branch."""

    def test_returns_true_when_contract_exists(self, tmp_path: Path):
        """When the contract file already exists, return True without re-creating."""
        pipeline = _make_pipeline()
        # Create the expected contract file
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.json"
        contract_file.write_text("{}")

        result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

    def test_recreates_contract_when_missing_issue_number(self, tmp_path: Path):
        """When the contract file is missing and pipeline has issue_number, re-create it."""
        pipeline = _make_pipeline(issue_number=42)

        with (
            patch("egg_contracts.loader.create_contract") as mock_create,
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_called_once_with(
            issue_number=42,
            title="Issue #42",
            url="https://github.com/owner/repo/issues/42",
            repo_root=tmp_path,
        )
        mock_populate.assert_called_once_with(
            tmp_path,
            pipeline.id,
            pipeline.mode or "local",
            42,
        )
        mock_commit.assert_called_once_with(
            tmp_path,
            "Restore missing contract for 42",
            pipeline_identifier=42,
        )

    def test_recreates_contract_when_missing_pipeline_id(self, tmp_path: Path):
        """When no issue_number, uses pipeline_id to create contract."""
        pipeline = _make_pipeline(issue_number=None, pipeline_id="pipe-abc")

        with (
            patch("egg_contracts.loader.create_contract") as mock_create,
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_called_once_with(
            pipeline_id="pipe-abc",
            title="test prompt",
            repo_root=tmp_path,
        )
        mock_populate.assert_called_once_with(
            tmp_path,
            "pipe-abc",
            pipeline.mode or "local",
            None,
        )
        mock_commit.assert_called_once_with(
            tmp_path,
            "Restore missing contract for pipe-abc",
            pipeline_identifier="pipe-abc",
        )

    def test_canonical_path_guard_prevents_recreation(self, tmp_path: Path):
        """When hardcoded path is missing but canonical path exists, skip recreation."""
        pipeline = _make_pipeline()
        # Do NOT create the hardcoded contract file — simulate path drift where
        # get_contract_path resolves to a different location that does exist.
        alt_dir = tmp_path / ".egg-state" / "alt-contracts"
        alt_dir.mkdir(parents=True)
        alt_contract = alt_dir / "42.json"
        alt_contract.write_text("{}")

        with (
            patch("egg_contracts.loader.get_contract_path", return_value=alt_contract),
            patch("egg_contracts.loader.create_contract") as mock_create,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_not_called()

    def test_returns_false_when_restoration_fails(self, tmp_path: Path):
        """When contract re-creation raises an exception, return False."""
        pipeline = _make_pipeline()

        with patch(
            "egg_contracts.loader.create_contract",
            side_effect=RuntimeError("disk full"),
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is False


class TestEnsureStatefilesFallbackToPipelineModel:
    """_ensure_statefiles_on_branch falls back to pipeline.plan/analysis fields.

    When the plan draft cannot be restored from the remote branch (e.g.,
    it was never pushed in the first place), the function should write
    the plan content from the Pipeline model to disk before populating
    the contract.

    See: https://github.com/jwbron/egg/issues/1460
    """

    def test_falls_back_to_pipeline_plan_when_remote_fails(self, tmp_path: Path):
        """Pipeline.plan is written to disk when git show fails."""
        from routes.pipelines import _build_pr_body

        pipeline = _make_pipeline(
            pipeline_id="pipe-fallback",
            issue_number=None,
            branch="egg/pipe-fallback",
            plan=SAMPLE_PLAN,
        )

        def failing_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            if "show" in cmd:
                result.returncode = 128
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

        assert result is True

        # Verify plan draft was written from pipeline model
        plan_path = tmp_path / ".egg-state" / "drafts" / "pipe-fallback-plan.md"
        assert plan_path.exists()
        assert plan_path.read_text() == SAMPLE_PLAN

        # Verify contract has PR metadata from the plan
        title, body = _build_pr_body(pipeline, tmp_path)
        assert title == "Add retry logic to API client"
        assert "exponential backoff" in body

    def test_falls_back_to_pipeline_analysis_when_remote_fails(self, tmp_path: Path):
        """Pipeline.analysis is written to disk when git show fails."""
        pipeline = _make_pipeline(
            pipeline_id="pipe-analysis",
            issue_number=None,
            branch="egg/pipe-analysis",
            analysis=SAMPLE_ANALYSIS,
        )

        def failing_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            if "show" in cmd:
                result.returncode = 128
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

        assert result is True

        # Verify analysis draft was written from pipeline model
        analysis_path = tmp_path / ".egg-state" / "drafts" / "pipe-analysis-analysis.md"
        assert analysis_path.exists()
        assert analysis_path.read_text() == SAMPLE_ANALYSIS

    def test_no_fallback_when_pipeline_fields_empty(self, tmp_path: Path):
        """No drafts written when pipeline has no plan/analysis fields."""
        pipeline = _make_pipeline(
            pipeline_id="pipe-empty",
            issue_number=None,
            branch="egg/pipe-empty",
            plan=None,
            analysis=None,
        )

        def failing_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            if "show" in cmd:
                result.returncode = 128
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

        assert result is True

        # No draft files should exist
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        if drafts_dir.exists():
            assert list(drafts_dir.iterdir()) == []
