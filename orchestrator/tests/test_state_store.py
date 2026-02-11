"""
Tests for state store.

Note: Git operations are mocked since git init is not available in the sandbox.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus
from state_store import (
    PipelineNotFoundError,
    StateStore,
    StateValidationError,
    get_state_store,
)


@pytest.fixture
def mock_git():
    """Mock git operations."""
    with patch.object(StateStore, "_run_git") as mock:
        mock.return_value = MagicMock(stdout="abc1234\n", returncode=0)
        yield mock


@pytest.fixture
def state_store(tmp_path, mock_git):
    """Create a state store for testing."""
    return StateStore(tmp_path)


class TestStateStoreBasics:
    """Basic state store tests."""

    def test_pipeline_not_exists(self, state_store):
        """Test checking non-existent pipeline."""
        assert not state_store.pipeline_exists("issue-999")

    def test_load_nonexistent_pipeline(self, state_store):
        """Test loading non-existent pipeline raises error."""
        with pytest.raises(PipelineNotFoundError):
            state_store.load_pipeline("issue-999")

    def test_list_empty_pipelines(self, state_store):
        """Test listing pipelines when none exist."""
        assert state_store.list_pipelines() == []


class TestPipelineCreation:
    """Tests for creating pipelines."""

    def test_create_pipeline(self, state_store):
        """Test creating a new pipeline."""
        pipeline = state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        assert pipeline.id == "issue-496"
        assert pipeline.issue_number == 496
        assert pipeline.repo == "owner/repo"
        assert pipeline.status == PipelineStatus.PENDING

    def test_create_pipeline_persists(self, state_store):
        """Test that created pipeline is persisted."""
        state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        assert state_store.pipeline_exists("issue-496")

        # Reload and verify
        loaded = state_store.load_pipeline("issue-496")
        assert loaded.issue_number == 496

    def test_create_pipeline_with_config(self, state_store):
        """Test creating pipeline with custom config."""
        pipeline = state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
            config={"auto_create_pr": False, "max_review_cycles": 5},
        )
        assert pipeline.config.auto_create_pr is False
        assert pipeline.config.max_review_cycles == 5

    def test_create_duplicate_pipeline_fails(self, state_store):
        """Test creating duplicate pipeline raises error."""
        state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        with pytest.raises(Exception):
            state_store.create_pipeline(
                issue_number=496,
                repo="owner/repo",
                branch="egg/issue-496",
            )


class TestPipelinePersistence:
    """Tests for pipeline persistence."""

    def test_save_and_load_pipeline(self, state_store):
        """Test saving and loading pipeline."""
        pipeline = Pipeline(
            id="issue-123",
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PLAN,
        )
        state_store.save_pipeline(pipeline)

        loaded = state_store.load_pipeline("issue-123")
        assert loaded.status == PipelineStatus.RUNNING
        assert loaded.current_phase == PipelinePhase.PLAN

    def test_save_updates_timestamp(self, state_store):
        """Test that saving updates the timestamp."""
        pipeline = state_store.create_pipeline(
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
        )
        original_time = pipeline.updated_at

        # Update and save
        pipeline.status = PipelineStatus.RUNNING
        state_store.save_pipeline(pipeline)

        loaded = state_store.load_pipeline("issue-123")
        assert loaded.updated_at >= original_time

    def test_save_calls_git(self, state_store, mock_git):
        """Test that saving calls git operations."""
        pipeline = state_store.create_pipeline(
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
        )

        # Git should have been called
        assert mock_git.called

    def test_save_without_commit(self, state_store, mock_git):
        """Test saving without committing."""
        pipeline = Pipeline(
            id="issue-456",
            issue_number=456,
            repo="owner/repo",
            branch="egg/issue-456",
        )
        state_store.save_pipeline(pipeline, commit=False)

        # File should exist
        assert state_store.pipeline_exists("issue-456")

        # Git commit should not have been called
        commit_calls = [c for c in mock_git.call_args_list if "commit" in c[0]]
        assert len(commit_calls) == 0


class TestPipelineUpdate:
    """Tests for updating pipelines."""

    def test_update_simple_field(self, state_store):
        """Test updating a simple field."""
        state_store.create_pipeline(
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
        )

        updated = state_store.update_pipeline(
            "issue-123",
            {"status": "running"},
        )
        assert updated.status == PipelineStatus.RUNNING

    def test_update_persists(self, state_store):
        """Test that updates are persisted."""
        state_store.create_pipeline(
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
        )

        state_store.update_pipeline("issue-123", {"status": "running"})

        loaded = state_store.load_pipeline("issue-123")
        assert loaded.status == PipelineStatus.RUNNING

    def test_update_nonexistent_fails(self, state_store):
        """Test updating non-existent pipeline fails."""
        with pytest.raises(PipelineNotFoundError):
            state_store.update_pipeline("issue-999", {"status": "running"})


class TestPipelineDeletion:
    """Tests for deleting pipelines."""

    def test_delete_pipeline(self, state_store):
        """Test deleting a pipeline."""
        state_store.create_pipeline(
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
        )
        assert state_store.pipeline_exists("issue-123")

        state_store.delete_pipeline("issue-123")
        assert not state_store.pipeline_exists("issue-123")

    def test_delete_nonexistent_fails(self, state_store):
        """Test deleting non-existent pipeline raises error."""
        with pytest.raises(PipelineNotFoundError):
            state_store.delete_pipeline("issue-999")


class TestPipelineListing:
    """Tests for listing pipelines."""

    def test_list_multiple_pipelines(self, state_store):
        """Test listing multiple pipelines."""
        state_store.create_pipeline(
            issue_number=1,
            repo="owner/repo",
            branch="egg/issue-1",
        )
        state_store.create_pipeline(
            issue_number=2,
            repo="owner/repo",
            branch="egg/issue-2",
        )
        state_store.create_pipeline(
            issue_number=3,
            repo="owner/repo",
            branch="egg/issue-3",
        )

        pipelines = state_store.list_pipelines()
        assert len(pipelines) == 3
        assert "issue-1" in pipelines
        assert "issue-2" in pipelines
        assert "issue-3" in pipelines

    def test_get_active_pipelines(self, state_store):
        """Test getting active pipelines."""
        state_store.create_pipeline(
            issue_number=1,
            repo="owner/repo",
            branch="egg/issue-1",
        )
        state_store.create_pipeline(
            issue_number=2,
            repo="owner/repo",
            branch="egg/issue-2",
        )
        state_store.create_pipeline(
            issue_number=3,
            repo="owner/repo",
            branch="egg/issue-3",
        )

        # Complete one pipeline
        state_store.update_pipeline("issue-2", {"status": "complete"})

        active = state_store.get_active_pipelines()
        assert len(active) == 2
        assert all(p.status != PipelineStatus.COMPLETE for p in active)


class TestStateValidation:
    """Tests for state validation."""

    def test_load_invalid_json_fails(self, state_store, tmp_path):
        """Test loading invalid JSON fails."""
        # Create directory and write invalid JSON
        pipelines_dir = tmp_path / ".egg-state" / "pipelines"
        pipelines_dir.mkdir(parents=True)
        (pipelines_dir / "issue-bad.json").write_text("not valid json")

        with pytest.raises(StateValidationError):
            state_store.load_pipeline("issue-bad")

    def test_load_invalid_schema_fails(self, state_store, tmp_path):
        """Test loading invalid schema fails."""
        pipelines_dir = tmp_path / ".egg-state" / "pipelines"
        pipelines_dir.mkdir(parents=True)
        (pipelines_dir / "issue-bad.json").write_text('{"id": "issue-bad"}')

        with pytest.raises(StateValidationError):
            state_store.load_pipeline("issue-bad")


class TestGetStateStore:
    """Tests for get_state_store helper."""

    def test_get_state_store_with_path(self, tmp_path, mock_git):
        """Test getting state store with Path."""
        store = get_state_store(tmp_path)
        assert store.repo_path == tmp_path

    def test_get_state_store_with_string(self, tmp_path, mock_git):
        """Test getting state store with string path."""
        store = get_state_store(str(tmp_path))
        assert store.repo_path == tmp_path


class TestGenerateCommitMessage:
    """Tests for commit message generation."""

    def test_generate_commit_message(self, state_store):
        """Test commit message generation."""
        pipeline = Pipeline(
            id="issue-123",
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
            status=PipelineStatus.RUNNING,
        )
        message = state_store._generate_commit_message(pipeline)
        assert "issue-123" in message
        assert "running" in message
