"""
Tests for state store.

Note: Git operations are mocked since git init is not available in the sandbox.
"""

import os
import shutil
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest
from gateway_client import PushResult
from models import Pipeline, PipelinePhase, PipelineStatus
from state_store import (
    GitOperationError,
    InvalidPipelineIdError,
    PipelineNotFoundError,
    StateStore,
    StateStoreError,
    StateValidationError,
    VersionConflictError,
    _validate_pipeline_id,
    get_pipeline_state_lock,
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
    """Create a state store for testing.

    The worktree is mocked out so file I/O goes to tmp_path directly.
    """
    store = StateStore(tmp_path, worktree_dir=tmp_path)
    # Bypass lazy worktree init since git is mocked
    store._worktree = tmp_path
    return store


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
            config={"max_review_cycles": 5},
        )
        assert pipeline.config.max_review_cycles == 5

    def test_create_pipeline_with_start_phase_sets_current_phase(self, state_store):
        """Test that start_phase sets current_phase at creation time."""
        pipeline = state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
            config={"start_phase": "implement"},
        )
        assert pipeline.current_phase == PipelinePhase.IMPLEMENT

    def test_create_pipeline_without_start_phase_defaults_to_refine(self, state_store):
        """Test that omitting start_phase keeps default REFINE phase."""
        pipeline = state_store.create_pipeline(
            issue_number=497,
            repo="owner/repo",
            branch="egg/issue-497",
        )
        assert pipeline.current_phase == PipelinePhase.REFINE

    def test_create_duplicate_pipeline_fails(self, state_store):
        """Test creating duplicate running pipeline raises error."""
        state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        with pytest.raises(StateStoreError, match="already exists"):
            state_store.create_pipeline(
                issue_number=496,
                repo="owner/repo",
                branch="egg/issue-496",
            )

    @pytest.mark.parametrize(
        "terminal_status",
        [
            PipelineStatus.CANCELLED,
            PipelineStatus.FAILED,
            PipelineStatus.COMPLETE,
        ],
    )
    def test_create_replaces_terminal_pipeline(self, state_store, terminal_status):
        """Test creating pipeline replaces existing one in terminal state."""
        pipeline = state_store.create_pipeline(
            issue_number=500,
            repo="owner/repo",
            branch="egg/issue-500",
        )
        # Transition to terminal status
        state_store.update_pipeline(pipeline.id, {"status": terminal_status.value})

        # Creating again should succeed by replacing the old one
        new_pipeline = state_store.create_pipeline(
            issue_number=500,
            repo="owner/repo",
            branch="egg/issue-500-v2",
        )
        assert new_pipeline.id == "issue-500"
        assert new_pipeline.status == PipelineStatus.PENDING
        assert new_pipeline.branch == "egg/issue-500-v2"
        # Fresh pipeline starts at version=1, save_pipeline increments to 2.
        # If old state leaked, this would be higher (old version + 1).
        assert new_pipeline.version == 2

    @pytest.mark.parametrize(
        "active_status",
        [
            PipelineStatus.RUNNING,
            PipelineStatus.AWAITING_HUMAN,
            PipelineStatus.PENDING,
        ],
    )
    def test_create_does_not_replace_active_pipeline(self, state_store, active_status):
        """Test creating pipeline does NOT replace an active one."""
        pipeline = state_store.create_pipeline(
            issue_number=501,
            repo="owner/repo",
            branch="egg/issue-501",
        )
        if active_status != PipelineStatus.PENDING:
            state_store.update_pipeline(pipeline.id, {"status": active_status.value})

        with pytest.raises(StateStoreError, match="already exists"):
            state_store.create_pipeline(
                issue_number=501,
                repo="owner/repo",
                branch="egg/issue-501",
            )

    def test_create_pipeline_with_network_mode(self, state_store):
        """Test creating pipeline with network_mode persists correctly."""
        pipeline = state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
            network_mode="private",
        )
        assert pipeline.network_mode == "private"

        # Reload and verify persistence
        loaded = state_store.load_pipeline("issue-496")
        assert loaded.network_mode == "private"

    def test_create_pipeline_network_mode_default_none(self, state_store):
        """Test creating pipeline without network_mode defaults to None."""
        pipeline = state_store.create_pipeline(
            issue_number=497,
            repo="owner/repo",
            branch="egg/issue-497",
        )
        assert pipeline.network_mode is None


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
        state_store.create_pipeline(
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
        # Use a valid pipeline ID format (issue-{number}) but with invalid JSON content
        pipelines_dir = tmp_path / ".egg-state" / "pipelines"
        pipelines_dir.mkdir(parents=True)
        (pipelines_dir / "issue-9999.json").write_text("not valid json")

        with pytest.raises(StateValidationError):
            state_store.load_pipeline("issue-9999")

    def test_load_invalid_schema_fails(self, state_store, tmp_path):
        """Test loading invalid schema fails."""
        # Use a valid pipeline ID format but with an invalid enum value for status
        pipelines_dir = tmp_path / ".egg-state" / "pipelines"
        pipelines_dir.mkdir(parents=True)
        (pipelines_dir / "issue-9998.json").write_text(
            '{"id": "issue-9998", "status": "not-a-valid-status"}'
        )

        with pytest.raises(StateValidationError):
            state_store.load_pipeline("issue-9998")


class TestGetStateStore:
    """Tests for get_state_store helper."""

    def test_get_state_store_with_path(self, tmp_path, mock_git):
        """Test getting state store with Path (git repo)."""
        (tmp_path / ".git").mkdir()
        store = get_state_store(tmp_path)
        assert store.repo_path == tmp_path

    def test_get_state_store_with_string(self, tmp_path, mock_git):
        """Test getting state store with string path (git repo)."""
        (tmp_path / ".git").mkdir()
        store = get_state_store(str(tmp_path))
        assert store.repo_path == tmp_path

    def test_get_state_store_rejects_non_git_dir(self, tmp_path):
        """Test that get_state_store raises for non-git directories."""
        with pytest.raises(StateStoreError, match="non-git directory"):
            get_state_store(tmp_path)

    def test_single_child_repo_uses_default_worktree(self, tmp_path, mock_git, monkeypatch):
        """When EGG_REPO_PATH is parent with a single child repo, use default worktree."""
        parent = tmp_path / "repos"
        parent.mkdir()
        child = parent / "myrepo"
        child.mkdir()
        (child / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))
        store = get_state_store(child)
        # Should use the default worktree path, NOT the per-repo suffix
        from state_store import _DEFAULT_WORKTREE_DIR

        assert store._worktree_dir == _DEFAULT_WORKTREE_DIR

    def test_multi_repo_uses_per_repo_worktree(self, tmp_path, mock_git, monkeypatch):
        """When EGG_REPO_PATH has multiple child repos, each gets a unique worktree."""
        parent = tmp_path / "repos"
        parent.mkdir()
        for name in ("repo-a", "repo-b"):
            child = parent / name
            child.mkdir()
            (child / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))
        monkeypatch.setenv("EGG_STATE_DIR", str(tmp_path / "state"))
        store_a = get_state_store(parent / "repo-a")
        store_b = get_state_store(parent / "repo-b")
        assert store_a._worktree_dir == tmp_path / "state" / "pipeline-worktree-repo-a"
        assert store_b._worktree_dir == tmp_path / "state" / "pipeline-worktree-repo-b"


class TestDiscoverRepoPaths:
    """Tests for discover_repo_paths helper."""

    def test_single_git_repo(self, tmp_path):
        """Single git repo returns [path]."""
        from state_store import discover_repo_paths

        (tmp_path / ".git").mkdir()
        assert discover_repo_paths(tmp_path) == [tmp_path]

    def test_parent_with_child_repos(self, tmp_path):
        """Parent dir returns child git repos."""
        from state_store import discover_repo_paths

        (tmp_path / "repo-a" / ".git").mkdir(parents=True)
        (tmp_path / "repo-b" / ".git").mkdir(parents=True)
        (tmp_path / "not-a-repo").mkdir()
        result = discover_repo_paths(tmp_path)
        assert len(result) == 2
        assert tmp_path / "not-a-repo" not in result

    def test_empty_dir(self, tmp_path):
        """Empty dir returns []."""
        from state_store import discover_repo_paths

        assert discover_repo_paths(tmp_path) == []


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


class TestPipelineIdValidation:
    """Tests for pipeline ID validation."""

    def test_valid_pipeline_id(self):
        """Test valid pipeline ID format."""
        _validate_pipeline_id("issue-123")  # Should not raise
        _validate_pipeline_id("issue-1")  # Should not raise
        _validate_pipeline_id("issue-999999")  # Should not raise

    def test_invalid_empty_id(self):
        """Test empty pipeline ID raises error."""
        with pytest.raises(InvalidPipelineIdError) as exc_info:
            _validate_pipeline_id("")
        assert "Invalid pipeline ID format" in str(exc_info.value)

    def test_invalid_none_id(self):
        """Test None pipeline ID raises error."""
        with pytest.raises(InvalidPipelineIdError) as exc_info:
            _validate_pipeline_id(None)  # type: ignore
        assert "Invalid pipeline ID format" in str(exc_info.value)

    def test_invalid_path_traversal_dotdot(self):
        """Test pipeline ID with path traversal (../) is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("../../../etc/passwd")

    def test_invalid_path_traversal_absolute(self):
        """Test pipeline ID with absolute path is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("/etc/passwd")

    def test_invalid_missing_prefix(self):
        """Test pipeline ID without 'issue-' prefix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("123")

    def test_valid_local_prefix(self):
        """Test pipeline ID with local- prefix is accepted."""
        _validate_pipeline_id("local-a1b2c3d4")  # Should not raise

    def test_valid_pipeline_prefix(self):
        """Test pipeline ID with pipeline- prefix is accepted (prompt-driven)."""
        _validate_pipeline_id("pipeline-85170faf")  # Should not raise
        _validate_pipeline_id("pipeline-a1b2c3d4")  # Should not raise

    def test_invalid_pipeline_prefix_non_hex(self):
        """Test pipeline ID with non-hex suffix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("pipeline-ZZZZZZZZ")

    def test_invalid_pipeline_prefix_too_short(self):
        """Test pipeline ID with too-short hex suffix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("pipeline-123")

    def test_invalid_pipeline_prefix_too_long(self):
        """Test pipeline ID with too-long hex suffix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("pipeline-a1b2c3d4e5")

    def test_valid_pr_prefix(self):
        """Test pipeline ID with pr- prefix is accepted (babysit mode)."""
        _validate_pipeline_id("pr-123")  # Should not raise

    def test_invalid_wrong_prefix(self):
        """Test pipeline ID with wrong prefix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("xyz-123")

    def test_invalid_special_characters(self):
        """Test pipeline ID with special characters is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("issue-123;rm -rf /")

    def test_invalid_command_injection(self):
        """Test pipeline ID with command injection attempt is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("issue-$(whoami)")

    def test_invalid_negative_number(self):
        """Test pipeline ID with negative number is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("issue--123")

    def test_invalid_non_numeric_suffix(self):
        """Test pipeline ID with non-numeric suffix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("issue-abc")

    def test_operations_with_invalid_pipeline_id(self, state_store):
        """Test that operations reject invalid pipeline IDs."""
        invalid_id = "../etc/passwd"

        with pytest.raises(InvalidPipelineIdError):
            state_store.pipeline_exists(invalid_id)

        with pytest.raises(InvalidPipelineIdError):
            state_store.load_pipeline(invalid_id)

        with pytest.raises(InvalidPipelineIdError):
            state_store.delete_pipeline(invalid_id)


class TestVersionConflict:
    """Tests for optimistic locking with version conflicts."""

    def test_save_with_matching_version(self, state_store):
        """Test save succeeds when expected version matches."""
        # Create pipeline
        pipeline = state_store.create_pipeline(
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
        )
        initial_version = pipeline.version

        # Save with matching expected version
        pipeline.status = PipelineStatus.RUNNING
        state_store.save_pipeline(pipeline, expected_version=initial_version)

        # Verify save succeeded
        loaded = state_store.load_pipeline("issue-123")
        assert loaded.status == PipelineStatus.RUNNING
        assert loaded.version == initial_version + 1

    def test_save_with_version_conflict(self, state_store):
        """Test save fails when expected version doesn't match."""
        # Create pipeline
        pipeline = state_store.create_pipeline(
            issue_number=456,
            repo="owner/repo",
            branch="egg/issue-456",
        )

        # Simulate concurrent modification by saving again
        pipeline.status = PipelineStatus.RUNNING
        state_store.save_pipeline(pipeline)  # Version is now 2

        # Try to save with outdated expected version
        pipeline.status = PipelineStatus.COMPLETE
        with pytest.raises(VersionConflictError) as exc_info:
            state_store.save_pipeline(pipeline, expected_version=1)

        assert "Version conflict" in str(exc_info.value)
        assert "expected version 1" in str(exc_info.value)

    def test_save_without_expected_version_always_succeeds(self, state_store):
        """Test save without expected_version always succeeds."""
        # Create pipeline
        pipeline = state_store.create_pipeline(
            issue_number=789,
            repo="owner/repo",
            branch="egg/issue-789",
        )

        # Multiple saves without version check should all succeed
        pipeline.status = PipelineStatus.RUNNING
        state_store.save_pipeline(pipeline)  # No expected_version

        pipeline.status = PipelineStatus.COMPLETE
        state_store.save_pipeline(pipeline)  # No expected_version

        loaded = state_store.load_pipeline("issue-789")
        assert loaded.status == PipelineStatus.COMPLETE

    def test_save_new_pipeline_with_expected_version(self, state_store):
        """Test saving a new pipeline with expected_version works."""
        # Create a new pipeline object (not persisted yet)
        pipeline = Pipeline(
            id="issue-999",
            issue_number=999,
            repo="owner/repo",
            branch="egg/issue-999",
        )

        # Save with expected_version should work for new pipelines
        # since there's nothing to conflict with
        state_store.save_pipeline(pipeline, expected_version=0)

        loaded = state_store.load_pipeline("issue-999")
        assert loaded.id == "issue-999"

    def test_version_increments_on_save(self, state_store):
        """Test that version increments with each save."""
        pipeline = state_store.create_pipeline(
            issue_number=111,
            repo="owner/repo",
            branch="egg/issue-111",
        )
        version_after_create = pipeline.version

        # Each save should increment version
        for _i in range(3):
            loaded = state_store.load_pipeline("issue-111")
            loaded.status = PipelineStatus.RUNNING
            state_store.save_pipeline(loaded)

        final = state_store.load_pipeline("issue-111")
        assert final.version == version_after_create + 3


class TestUnbornBranchEdgeCases:
    """Tests for edge cases with unborn (orphan) branches."""

    def test_commit_state_handles_unborn_branch_no_changes(self, state_store, mock_git):
        """Test that _commit_state handles unborn branch with no staged changes.

        When the orphan branch is first created but HEAD doesn't exist yet,
        and there are no staged changes, rev-parse HEAD would fail. The code
        should handle this gracefully by returning an empty string.
        """
        from unittest.mock import MagicMock

        from models import Pipeline

        pipeline = Pipeline(
            id="issue-999",
            issue_number=999,
            repo="owner/repo",
            branch="egg/issue-999",
        )

        # Simulate: no staged changes (returncode=0) and unborn branch (HEAD fails)
        def mock_git_responses(*args, **kwargs):
            result = MagicMock()
            if args[0] == "diff" and "--cached" in args:
                result.returncode = 0  # No staged changes
                result.stdout = ""
            elif args[0] == "rev-parse" and "HEAD" in args:
                result.returncode = 128  # HEAD doesn't exist on unborn branch
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = "abc1234\n"
            return result

        mock_git.side_effect = mock_git_responses

        # This should not raise - should return empty string for unborn branch
        sha = state_store._commit_state(pipeline)
        assert sha == ""

    def test_commit_state_returns_sha_after_commit(self, state_store, mock_git):
        """Test that _commit_state returns SHA after successful commit."""
        from unittest.mock import MagicMock

        from models import Pipeline

        pipeline = Pipeline(
            id="issue-888",
            issue_number=888,
            repo="owner/repo",
            branch="egg/issue-888",
        )

        expected_sha = "def5678"

        # Simulate: staged changes exist, commit succeeds, rev-parse returns SHA
        def mock_git_responses(*args, **kwargs):
            result = MagicMock()
            if args[0] == "diff" and "--cached" in args:
                result.returncode = 1  # Changes are staged
                result.stdout = ""
            elif args[0] == "commit":
                result.returncode = 0
                result.stdout = ""
            elif args[0] == "rev-parse" and "HEAD" in args:
                result.returncode = 0
                result.stdout = f"{expected_sha}\n"
            else:
                result.returncode = 0
                result.stdout = ""
            return result

        mock_git.side_effect = mock_git_responses

        sha = state_store._commit_state(pipeline)
        assert sha == expected_sha


class TestDecisionPersistenceRegression:
    """Regression tests for HITL decision persistence.

    Reproduces the bug where saving a stale pipeline object after
    queue_decision() overwrites the newly persisted decision, causing
    "Decision decision-1 not found" errors during phase transitions.
    """

    def test_stale_pipeline_save_overwrites_decision(self, state_store):
        """Saving a pre-queue_decision pipeline must not erase the decision.

        This is the exact sequence that caused the issue-530 pipeline failure:
        1. Load pipeline (decisions=[])
        2. queue_decision() saves pipeline with decision-1
        3. Save the stale pipeline object from step 1 → decision lost
        """
        from unittest.mock import patch

        from decision_queue import DecisionQueue

        # Create a pipeline
        pipeline = state_store.create_pipeline(
            issue_number=530,
            repo="owner/repo",
            branch="egg/issue-530",
        )
        assert len(pipeline.decisions) == 0

        # Patch get_state_store so DecisionQueue uses the test's mocked store
        with patch("decision_queue.get_state_store", return_value=state_store):
            dq = DecisionQueue("issue-530", state_store.repo_path)
            decision = dq.queue_decision(
                question="Approve refine phase?",
                options=["approve"],
            )
            assert decision.id == "decision-1"

        # Verify decision was persisted
        reloaded = state_store.load_pipeline("issue-530")
        assert len(reloaded.decisions) == 1

        # BUG scenario: save the stale object (from before queue_decision)
        pipeline.status = PipelineStatus.AWAITING_HUMAN
        state_store.save_pipeline(pipeline)

        # The stale save overwrites the decision
        final = state_store.load_pipeline("issue-530")
        assert len(final.decisions) == 0, (
            "Stale save erased decision — callers must reload after queue_decision()"
        )

    def test_reload_after_queue_decision_preserves_decision(self, state_store):
        """Reloading pipeline after queue_decision() preserves the decision.

        This is the fix: reload from the store after queue_decision()
        before saving status changes.
        """
        from unittest.mock import patch

        from decision_queue import DecisionQueue

        state_store.create_pipeline(
            issue_number=531,
            repo="owner/repo",
            branch="egg/issue-531",
        )

        with patch("decision_queue.get_state_store", return_value=state_store):
            dq = DecisionQueue("issue-531", state_store.repo_path)
            dq.queue_decision(
                question="Approve refine phase?",
                options=["approve"],
            )

        # Fix: reload pipeline after queue_decision before saving status
        pipeline = state_store.load_pipeline("issue-531")
        pipeline.status = PipelineStatus.AWAITING_HUMAN
        state_store.save_pipeline(pipeline)

        final = state_store.load_pipeline("issue-531")
        assert len(final.decisions) == 1
        assert final.decisions[0].id == "decision-1"
        assert final.status == PipelineStatus.AWAITING_HUMAN


class TestUpdatePipelineDecisionRace:
    """Regression test for the race between update_pipeline and resolve_decision.

    Reproduces the bug where a concurrent update_pipeline (PATCH) overwrites
    a decision resolution because both do unsynchronized load-modify-save
    cycles on the same pipeline state file.

    Fix: Both update_pipeline and DecisionQueue now share a per-pipeline
    lock (get_pipeline_state_lock) so their load-modify-save cycles are
    mutually exclusive.
    """

    def test_concurrent_update_does_not_overwrite_resolved_decision(self, state_store):
        """Verify the per-pipeline lock serializes update_pipeline and resolve_decision.

        We patch save_pipeline inside Thread A's update_pipeline to pause
        just before writing, giving Thread B a window to call resolve_decision.
        Because both acquire get_pipeline_state_lock, Thread B blocks until
        Thread A releases the lock.  Thread B then loads fresh state (with
        Thread A's changes) and resolves on top — both effects survive.

        If the lock were removed from update_pipeline, Thread B would complete
        its resolve during Thread A's pause, and Thread A's subsequent save
        would overwrite the resolution with stale data (the original bug).
        """
        import threading
        import time
        from unittest.mock import patch as mock_patch

        from decision_queue import DecisionQueue
        from models import DecisionStatus

        pipeline_id = "issue-647"

        # Create pipeline and queue a decision
        state_store.create_pipeline(
            issue_number=647,
            repo="owner/repo",
            branch="egg/issue-647",
        )
        with mock_patch("decision_queue.get_state_store", return_value=state_store):
            dq = DecisionQueue(pipeline_id, state_store.repo_path)
            dq.queue_decision(
                question="Approve plan?",
                options=["approve", "request changes"],
            )

        a_about_to_save = threading.Event()
        errors: list[Exception] = []
        original_save = state_store.save_pipeline

        def delayed_save(pipeline, **kwargs):
            """Pause before writing to widen the concurrency window.

            Thread A holds the lock while sleeping.  Thread B attempts to
            acquire the same lock during this window and blocks.  After the
            sleep, Thread A saves and releases the lock, allowing Thread B
            to load the fresh state.
            """
            a_about_to_save.set()
            time.sleep(0.2)
            original_save(pipeline, **kwargs)

        def thread_a_update():
            """Simulate PATCH /pipelines — update_pipeline with delayed save."""
            try:
                with mock_patch.object(state_store, "save_pipeline", side_effect=delayed_save):
                    state_store.update_pipeline(pipeline_id, {"status": "awaiting_human"})
            except Exception as exc:
                errors.append(exc)

        def thread_b_resolve():
            """Simulate POST /decisions/resolve — attempts during A's save window."""
            try:
                a_about_to_save.wait(timeout=5)
                with mock_patch("decision_queue.get_state_store", return_value=state_store):
                    dq_b = DecisionQueue(pipeline_id, state_store.repo_path)
                    dq_b.resolve_decision("decision-1", "Approved")
            except Exception as exc:
                errors.append(exc)

        t_a = threading.Thread(target=thread_a_update)
        t_b = threading.Thread(target=thread_b_resolve)
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"

        # Both effects must be present: status update AND decision resolution
        final = state_store.load_pipeline(pipeline_id)
        assert final.status.value == "awaiting_human", "Thread A's status update was lost"
        assert len(final.decisions) == 1
        assert final.decisions[0].status == DecisionStatus.RESOLVED, (
            "update_pipeline overwrote resolved decision — "
            "the per-pipeline state lock is not preventing the race"
        )
        assert final.decisions[0].resolution == "Approved"

    def test_shared_lock_between_decision_queue_and_state_store(self):
        """DecisionQueue and update_pipeline must use the same lock instance."""
        from decision_queue import DecisionQueue

        pipeline_id = "issue-999"
        dq = DecisionQueue(pipeline_id, "/tmp/fake-repo")
        state_lock = get_pipeline_state_lock(pipeline_id)

        assert dq._lock is state_lock, (
            "DecisionQueue must use get_pipeline_state_lock so it shares "
            "the same lock as StateStore.update_pipeline"
        )

    def test_release_pipeline_state_lock_on_delete(self, state_store):
        """Deleting a pipeline should clean up its state lock."""
        from state_store import _pipeline_state_locks

        pipeline_id = "issue-888"
        state_store.create_pipeline(
            issue_number=888,
            repo="owner/repo",
            branch="egg/issue-888",
        )

        # Access the lock to create it
        get_pipeline_state_lock(pipeline_id)
        assert pipeline_id in _pipeline_state_locks

        # Delete pipeline — lock should be cleaned up
        state_store.delete_pipeline(pipeline_id)
        assert pipeline_id not in _pipeline_state_locks


class TestRunGitLocking:
    """Tests for cross-process file locking and retry logic in _run_git."""

    @pytest.fixture(autouse=True)
    def reset_flock_state(self):
        yield
        StateStore._flock_depth = 0
        for fd in StateStore._flock_fds.values():
            os.close(fd)
        StateStore._flock_fds.clear()

    def test_retry_succeeds_after_index_lock_error(self, tmp_path):
        """Test that _run_git retries on index.lock contention and succeeds."""
        store = StateStore(tmp_path, worktree_dir=tmp_path)
        store._worktree = tmp_path

        lock_error = subprocess.CalledProcessError(
            128,
            ["git", "add", "."],
            stderr="fatal: Unable to create 'index.lock': File exists.\n",
        )
        success = MagicMock(stdout="ok\n", returncode=0)

        with (
            patch("subprocess.run", side_effect=[lock_error, success]) as mock_run,
            patch("state_store.time.sleep"),
        ):
            result = store._run_git("add", ".")
            assert result.stdout == "ok\n"
            assert mock_run.call_count == 2

    def test_retry_exhausted_raises_git_error(self, tmp_path):
        """Test that _run_git raises after exhausting retries."""
        store = StateStore(tmp_path, worktree_dir=tmp_path)
        store._worktree = tmp_path

        lock_error = subprocess.CalledProcessError(
            128,
            ["git", "add", "."],
            stderr="fatal: Unable to create 'index.lock': File exists.\n",
        )

        with patch("subprocess.run", side_effect=[lock_error] * 3), patch("state_store.time.sleep"):
            with pytest.raises(GitOperationError, match="index.lock"):
                store._run_git("add", ".")

    def test_non_lock_error_not_retried(self, tmp_path):
        """Test that non-index.lock errors are raised immediately."""
        store = StateStore(tmp_path, worktree_dir=tmp_path)
        store._worktree = tmp_path

        other_error = subprocess.CalledProcessError(
            1,
            ["git", "commit"],
            stderr="nothing to commit\n",
        )

        with patch("subprocess.run", side_effect=other_error) as mock_run:
            with pytest.raises(GitOperationError, match="nothing to commit"):
                store._run_git("commit")
            assert mock_run.call_count == 1

    def test_cleanup_stale_locks(self, tmp_path):
        """Test that stale lock files are cleaned up."""
        store = StateStore(tmp_path, worktree_dir=tmp_path)

        # Create a fake .git/worktrees/pipeline-worktree/index.lock
        git_dir = tmp_path / ".git" / "worktrees" / "pipeline-worktree"
        git_dir.mkdir(parents=True)
        lock_file = git_dir / "index.lock"
        lock_file.touch()

        # Make it look old (>60s)
        import os

        old_time = os.path.getmtime(str(lock_file)) - 120
        os.utime(str(lock_file), (old_time, old_time))

        store._cleanup_stale_locks()
        assert not lock_file.exists()

    def test_cleanup_preserves_fresh_locks(self, tmp_path):
        """Test that fresh lock files are not removed."""
        store = StateStore(tmp_path, worktree_dir=tmp_path)

        git_dir = tmp_path / ".git" / "worktrees" / "pipeline-worktree"
        git_dir.mkdir(parents=True)
        lock_file = git_dir / "index.lock"
        lock_file.touch()  # Fresh — just created

        store._cleanup_stale_locks()
        assert lock_file.exists()

    def test_git_op_creates_lock_file(self, tmp_path):
        """Test that _git_op creates the flock lock file on disk."""
        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()
        store = StateStore(tmp_path, worktree_dir=worktree_dir)
        store._worktree = worktree_dir

        # Lock file lives inside the bare repo's .git/ so the gateway pod
        # (which mounts /home/egg/repos/ from the same hostPath) sees the
        # same inode and flock serialises both processes (#2311).
        lock_file = tmp_path / ".git" / ".egg-cross-process.lock"
        assert not lock_file.exists()

        with store._git_op():
            assert lock_file.exists()

    def test_git_op_reentrant(self, tmp_path):
        """Test that _git_op can be nested without deadlocking."""
        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()
        store = StateStore(tmp_path, worktree_dir=worktree_dir)
        store._worktree = worktree_dir

        # Nested calls should succeed (reentrant RLock + flock depth tracking)
        with store._git_op():
            with store._git_op():
                with store._git_op():
                    pass
            # Depth should still be >0 here, flock not yet released
        # After all nesting exits, flock is released

    def test_commit_state_holds_lock_across_git_calls(self, tmp_path):
        """Test that _commit_state holds the lock for entire add→diff→commit."""
        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()
        store = StateStore(tmp_path, worktree_dir=worktree_dir)
        store._worktree = worktree_dir

        # Set up a pipeline file so _get_pipeline_path works
        pipelines_dir = worktree_dir / ".egg-state" / "pipelines"
        pipelines_dir.mkdir(parents=True)
        (pipelines_dir / "issue-100.json").write_text("{}")

        from models import Pipeline

        pipeline = Pipeline(id="issue-100", issue_number=100, repo="test/repo", branch="egg/test")

        depth_during_calls = []

        def tracking_run(*args, **kwargs):
            # Record the flock nesting depth during each subprocess call.
            # If compound locking works, depth should be >= 2 (outer _commit_state
            # + inner _run_git).
            depth_during_calls.append(StateStore._flock_depth)
            return MagicMock(stdout="abc1234\n", returncode=0)

        with patch("subprocess.run", side_effect=tracking_run):
            store._commit_state(pipeline)

        # All git calls should have happened at depth >= 2 (compound + inner)
        assert len(depth_during_calls) >= 2  # at least add + diff (or add + diff + commit)
        assert all(d >= 2 for d in depth_during_calls)


class TestRemoteSync:
    """Tests for remote sync (push/restore) of the state branch."""

    def test_sync_to_remote_calls_gateway_client(self, state_store):
        """sync_to_remote calls push_worktree_branch on the gateway client.

        Regression for #1808: must push via the main repo path (shared
        hostPath) with an explicit ``ref``, not the state worktree path
        which lives in the orchestrator pod's unshared emptyDir.
        """
        mock_client = MagicMock()
        mock_client.push_worktree_branch.return_value = PushResult(ok=True)

        with patch("gateway_client.get_gateway_client", return_value=mock_client):
            # Import inside so the lazy import in sync_to_remote resolves
            result = state_store.sync_to_remote()

        assert result is True
        mock_client.push_worktree_branch.assert_called_once_with(
            pipeline_id="state-sync",
            repo_path=str(state_store.repo_path),
            branch="egg/pipeline-state",
            mode="public",
            ref="egg/pipeline-state",
        )

    def test_sync_to_remote_returns_false_on_failure(self, state_store):
        """sync_to_remote returns False when the gateway push fails."""
        mock_client = MagicMock()
        mock_client.push_worktree_branch.return_value = PushResult(
            ok=False, category="test", detail="mock failure"
        )

        with patch("gateway_client.get_gateway_client", return_value=mock_client):
            result = state_store.sync_to_remote()

        assert result is False

    def test_sync_to_remote_catches_exceptions(self, state_store):
        """sync_to_remote catches exceptions and returns False."""
        with patch("gateway_client.get_gateway_client", side_effect=Exception("no gateway")):
            result = state_store.sync_to_remote()

        assert result is False

    def test_sync_to_remote_async_debounces_and_retries(self, state_store):
        """_sync_to_remote_async retries after in-flight push when pending flag is set."""
        import time

        call_count = 0
        original_in_flight = StateStore._push_in_flight
        original_pending = StateStore._push_pending
        StateStore._push_in_flight = False
        StateStore._push_pending = False

        def slow_sync():
            nonlocal call_count
            call_count += 1
            time.sleep(0.1)
            return True

        try:
            with patch.object(state_store, "sync_to_remote", side_effect=slow_sync):
                # First call starts the thread
                state_store._sync_to_remote_async()
                # Small delay to ensure thread starts and sets flag
                time.sleep(0.02)
                # Second call should set _push_pending (not start a new thread)
                state_store._sync_to_remote_async()
                # Wait for both pushes to complete (initial + retry)
                time.sleep(0.4)

            # Two pushes: original + retry triggered by pending flag
            assert call_count == 2
        finally:
            StateStore._push_in_flight = original_in_flight
            StateStore._push_pending = original_pending

    def test_sync_to_remote_async_no_retry_without_pending(self, state_store):
        """_sync_to_remote_async does not retry when no pending commits arrived."""
        import time

        call_count = 0
        original_in_flight = StateStore._push_in_flight
        original_pending = StateStore._push_pending
        StateStore._push_in_flight = False
        StateStore._push_pending = False

        def slow_sync():
            nonlocal call_count
            call_count += 1
            time.sleep(0.05)
            return True

        try:
            with patch.object(state_store, "sync_to_remote", side_effect=slow_sync):
                # Single call with no concurrent calls
                state_store._sync_to_remote_async()
                # Wait for push to complete
                time.sleep(0.2)

            # Only one push — no pending flag was set
            assert call_count == 1
        finally:
            StateStore._push_in_flight = original_in_flight
            StateStore._push_pending = original_pending

    def test_restore_from_remote_when_branch_exists(self, state_store):
        """_restore_from_remote fetches when remote branch exists."""
        mock_client = MagicMock()
        mock_client.ls_remote_branch.return_value = True
        mock_client.fetch_branch.return_value = True

        with patch("gateway_client.get_gateway_client", return_value=mock_client):
            result = state_store._restore_from_remote()

        assert result is True
        mock_client.ls_remote_branch.assert_called_once_with(
            pipeline_id="state-restore",
            repo_path=str(state_store.repo_path),
            ref="refs/heads/egg/pipeline-state",
            mode="public",
        )
        mock_client.fetch_branch.assert_called_once_with(
            pipeline_id="state-restore",
            repo_path=str(state_store.repo_path),
            args=["+refs/heads/egg/pipeline-state:refs/heads/egg/pipeline-state"],
            mode="public",
        )

    def test_restore_from_remote_skips_when_no_remote(self, state_store):
        """_restore_from_remote returns False when remote branch doesn't exist."""
        mock_client = MagicMock()
        mock_client.ls_remote_branch.return_value = False

        with patch("gateway_client.get_gateway_client", return_value=mock_client):
            result = state_store._restore_from_remote()

        assert result is False
        mock_client.fetch_branch.assert_not_called()

    def test_restore_from_remote_handles_fetch_failure(self, state_store):
        """_restore_from_remote returns False when fetch fails."""
        mock_client = MagicMock()
        mock_client.ls_remote_branch.return_value = True
        mock_client.fetch_branch.return_value = False

        with patch("gateway_client.get_gateway_client", return_value=mock_client):
            result = state_store._restore_from_remote()

        assert result is False

    def test_restore_from_remote_catches_exceptions(self, state_store):
        """_restore_from_remote catches exceptions and returns False."""
        with patch("gateway_client.get_gateway_client", side_effect=Exception("no gateway")):
            result = state_store._restore_from_remote()

        assert result is False

    @pytest.mark.parametrize(
        "remote_url, expected_repo",
        [
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("git@github.com:owner/repo", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ],
    )
    def test_detect_gateway_mode_parses_remote_urls(self, state_store, remote_url, expected_repo):
        """_detect_gateway_mode correctly parses HTTPS and SSH remote URLs."""
        # Clear any cached value
        if hasattr(state_store, "_cached_gateway_mode"):
            del state_store._cached_gateway_mode

        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "private"
        git_result = MagicMock(returncode=0, stdout=remote_url)

        with (
            patch("gateway_client.get_gateway_client", return_value=mock_client),
            patch.object(state_store, "_run_git", return_value=git_result),
        ):
            result = state_store._detect_gateway_mode()

        assert result == "private"
        mock_client.get_repo_visibility.assert_called_once_with(expected_repo)

    def test_detect_gateway_mode_caches_result(self, state_store):
        """_detect_gateway_mode caches the result for the instance lifetime."""
        # Clear any cached value
        if hasattr(state_store, "_cached_gateway_mode"):
            del state_store._cached_gateway_mode

        mock_client = MagicMock()
        mock_client.get_repo_visibility.return_value = "public"
        git_result = MagicMock(returncode=0, stdout="https://github.com/o/r.git")

        with (
            patch("gateway_client.get_gateway_client", return_value=mock_client),
            patch.object(state_store, "_run_git", return_value=git_result),
        ):
            result1 = state_store._detect_gateway_mode()
            result2 = state_store._detect_gateway_mode()

        assert result1 == result2 == "public"
        # Only called once — second call uses cache
        mock_client.get_repo_visibility.assert_called_once()

    def test_sync_to_remote_async_respects_max_retries(self, state_store):
        """_sync_to_remote_async skips retry when max retry depth is reached."""
        import time

        call_count = 0
        original_in_flight = StateStore._push_in_flight
        original_pending = StateStore._push_pending
        StateStore._push_in_flight = False
        StateStore._push_pending = False

        def slow_sync():
            nonlocal call_count
            call_count += 1
            time.sleep(0.05)
            # Simulate another commit arriving during every push
            with StateStore._push_lock:
                StateStore._push_pending = True
            return True

        try:
            with patch.object(state_store, "sync_to_remote", side_effect=slow_sync):
                state_store._sync_to_remote_async()
                # Wait for the full retry chain to complete
                time.sleep(0.5)

            # Should be capped at _MAX_PUSH_RETRIES (3): initial + 2 retries
            assert call_count == StateStore._MAX_PUSH_RETRIES
        finally:
            StateStore._push_in_flight = original_in_flight
            StateStore._push_pending = original_pending

    def test_delete_pipeline_triggers_remote_sync(self, state_store, mock_git):
        """delete_pipeline syncs to remote after committing deletion."""
        state_store.create_pipeline(
            issue_number=500,
            repo="owner/repo",
            branch="egg/issue-500",
        )

        # Make 'diff --cached --quiet' return non-zero (staged changes exist)

        def git_side_effect(*args, **kwargs):
            if args and "diff" in args and "--cached" in args and "--quiet" in args:
                return MagicMock(stdout="", returncode=1)
            return MagicMock(stdout="abc1234\n", returncode=0)

        mock_git.side_effect = git_side_effect

        with patch.object(state_store, "_sync_to_remote_async") as mock_sync:
            state_store.delete_pipeline("issue-500")

        mock_sync.assert_called_once()


class TestCommitFailureResilience:
    """Tests for issue #1736: _commit_state failure must not corrupt pipeline state."""

    def test_commit_state_tolerates_nothing_to_commit(self, state_store, mock_git):
        """_commit_state returns HEAD SHA when git commit fails with 'nothing to commit'."""
        pipeline = Pipeline(
            id="issue-700",
            issue_number=700,
            repo="owner/repo",
            branch="egg/issue-700",
        )

        def mock_git_responses(*args, **kwargs):
            if args[0] == "diff" and "--cached" in args:
                return MagicMock(stdout="", returncode=1)  # Changes staged
            if args[0] == "commit":
                raise GitOperationError("Git command failed: nothing to commit")
            if args[0] == "rev-parse" and "HEAD" in args:
                return MagicMock(stdout="aaa1111\n", returncode=0)
            return MagicMock(stdout="", returncode=0)

        mock_git.side_effect = mock_git_responses

        sha = state_store._commit_state(pipeline)
        assert sha == "aaa1111"

    def test_commit_state_reraises_non_benign_errors(self, state_store, mock_git):
        """_commit_state still raises for non-benign git errors."""
        pipeline = Pipeline(
            id="issue-701",
            issue_number=701,
            repo="owner/repo",
            branch="egg/issue-701",
        )

        def mock_git_responses(*args, **kwargs):
            if args[0] == "diff" and "--cached" in args:
                return MagicMock(stdout="", returncode=1)
            if args[0] == "commit":
                raise GitOperationError("Git command failed: fatal: unable to write")
            return MagicMock(stdout="", returncode=0)

        mock_git.side_effect = mock_git_responses

        with pytest.raises(GitOperationError, match="unable to write"):
            state_store._commit_state(pipeline)

    def test_save_pipeline_atomic_write_survives_commit_failure(self, state_store, mock_git):
        """File on disk is valid JSON even when _commit_state raises."""
        pipeline = state_store.create_pipeline(
            issue_number=702,
            repo="owner/repo",
            branch="egg/issue-702",
        )

        # Make commit fail with a non-benign error (caught by save_pipeline's try/except)
        def failing_git(*args, **kwargs):
            if args[0] == "commit":
                raise GitOperationError("Git command failed: index corruption")
            if args[0] == "diff" and "--cached" in args:
                return MagicMock(stdout="", returncode=1)
            return MagicMock(stdout="abc1234\n", returncode=0)

        mock_git.side_effect = failing_git

        pipeline.status = PipelineStatus.RUNNING
        path = state_store.save_pipeline(pipeline, force_commit=True)

        # File must be valid JSON and loadable
        loaded = state_store.load_pipeline("issue-702")
        assert loaded.status == PipelineStatus.RUNNING
        assert path.exists()

    def test_save_pipeline_does_not_raise_on_commit_failure(self, state_store, mock_git):
        """save_pipeline returns path successfully even when _commit_state raises."""
        pipeline = Pipeline(
            id="issue-703",
            issue_number=None,
            repo="owner/repo",
            branch="egg/pipeline-703",
            prompt="test prompt",
        )

        with patch.object(state_store, "_commit_state", side_effect=GitOperationError("boom")):
            path = state_store.save_pipeline(pipeline, force_commit=True)

        assert path.exists()
        loaded = state_store.load_pipeline("issue-703")
        assert loaded.id == "issue-703"

    def test_load_pipeline_empty_file_returns_not_found(self, state_store, mock_git):
        """Empty state file raises PipelineNotFoundError, not StateValidationError."""
        # Create the pipeline directory and write an empty file
        pipelines_dir = state_store.worktree / ".egg-state" / "pipelines"
        pipelines_dir.mkdir(parents=True, exist_ok=True)
        (pipelines_dir / "issue-704.json").write_text("")

        with pytest.raises(PipelineNotFoundError, match="empty"):
            state_store.load_pipeline("issue-704")


class TestEnsureWorktreeIdempotency:
    """Regression tests for #2140 — `_ensure_worktree` must be idempotent
    against worktree directories that exist on disk but are missing or
    have a broken `.git` link."""

    def _make_store(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()
        worktree_dir = tmp_path / "wt"
        store = StateStore(repo_path, worktree_dir=worktree_dir)
        return store, worktree_dir

    def _git_router(self, *, rev_parse_returncodes, branch_exists=True):
        """Build a `_run_git` side_effect that routes by command.

        - `rev-parse --is-inside-work-tree` → returncodes from
          `rev_parse_returncodes` (one per call, in order).
        - `rev-parse --verify refs/heads/...` → branch existence probe.
        - Anything else → success.
        """
        rev_parse_iter = iter(rev_parse_returncodes)

        def run_git(*args, check=True, cwd=None):
            if args[:2] == ("rev-parse", "--is-inside-work-tree"):
                rc = next(rev_parse_iter)
                return MagicMock(stdout="", returncode=rc)
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0 if branch_exists else 1)
            return MagicMock(stdout="", returncode=0)

        return run_git

    def test_idempotent_when_worktree_healthy(self, tmp_path):
        """Repeat _ensure_worktree calls reuse a healthy worktree —
        `git worktree add` must NOT be invoked the second time."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /fake\n")

        with patch.object(
            StateStore, "_run_git", side_effect=self._git_router(rev_parse_returncodes=[0, 0])
        ) as mock_git:
            assert store._ensure_worktree() == wt
            assert store._ensure_worktree() == wt

        # Neither call should have invoked `git worktree add` because the
        # worktree validated cleanly both times.
        worktree_add_calls = [
            c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "add")
        ]
        assert worktree_add_calls == []

    def test_recreates_when_dot_git_missing(self, tmp_path):
        """Worktree dir exists but `.git` link is missing (#2140 trigger).

        Prior to the fix, the validity branch was gated on `(wt / ".git").exists()`,
        so this case skipped the cleanup entirely and fell through to
        `git worktree add`, which fails with "already exists"."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        # Intentionally do not create wt/.git — simulates an orphaned admin dir
        # whose admin metadata under <repo>/.git/worktrees/ is gone.
        (wt / "leftover.txt").write_text("from cycle 0")

        # rev-parse fails twice (no .git), forcing recreate.
        with patch.object(
            StateStore, "_run_git", side_effect=self._git_router(rev_parse_returncodes=[1, 1])
        ) as mock_git:
            with patch("state_store.time.sleep"):
                result = store._ensure_worktree()

        assert result == wt
        # `git worktree add <wt> <STATE_BRANCH>` must have been called after
        # cleanup — confirms we took the recreate path, not the failure path.
        worktree_add_calls = [
            c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "add")
        ]
        assert len(worktree_add_calls) == 1
        # And the leftover from cycle 0 must be gone (rmtree ran).
        assert not (wt / "leftover.txt").exists()

    def test_recreates_when_rev_parse_fails(self, tmp_path):
        """Worktree dir + .git both exist but rev-parse fails twice.

        Existing behavior, but previously cleanup used
        `shutil.rmtree(..., ignore_errors=True)` which silently masked
        failures. This test confirms the recreate path still runs."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /broken\n")

        with patch.object(
            StateStore, "_run_git", side_effect=self._git_router(rev_parse_returncodes=[1, 1])
        ) as mock_git:
            with patch("state_store.time.sleep"):
                result = store._ensure_worktree()

        assert result == wt
        worktree_add_calls = [
            c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "add")
        ]
        assert len(worktree_add_calls) == 1

    def test_rmtree_failure_raises_git_error(self, tmp_path):
        """If cleanup rmtree fails, we surface a GitOperationError instead
        of silently falling through to `git worktree add` (which would
        produce the cryptic "already exists" failure that motivates #2140)."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /broken\n")

        with patch.object(
            StateStore, "_run_git", side_effect=self._git_router(rev_parse_returncodes=[1, 1])
        ):
            with (
                patch("state_store.time.sleep"),
                patch("state_store.shutil.rmtree", side_effect=OSError("permission denied")),
            ):
                with pytest.raises(
                    GitOperationError, match="Failed to remove stale state worktree"
                ):
                    store._ensure_worktree()

    def test_rmtree_enoent_treated_as_success(self, tmp_path):
        """Regression for #2234.

        `_ensure_worktree` runs from many threads concurrently (the state
        store probe at ``state_store_probe.py``, every pipeline driver
        thread polling via ``wait_for_decision``, the ``/api/v1/health``
        probe, sibling pipelines on the same repo).  Between the
        ``wt.exists()`` validity check and the ``shutil.rmtree(wt)`` that
        the recreate path runs against the same path, a concurrent caller
        can rmtree the directory, leaving our rmtree to raise
        ``FileNotFoundError``.  The directory being gone is exactly the
        desired post-state — we must treat ENOENT as success and continue
        to the recreate path, not raise ``GitOperationError`` and zombie
        the pipeline.
        """
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /broken\n")

        # When our rmtree raises ENOENT, the recreate path needs wt gone
        # on disk so the post-rmtree ``if wt.exists(): raise`` guard
        # passes.  Simulate the racing deleter by removing wt for real.
        # Bind the un-patched rmtree before patching so the helper's own
        # delete does not recurse into the patched mock.
        _real_rmtree = shutil.rmtree

        def rmtree_after_concurrent_delete(path, *args, **kwargs):
            _real_rmtree(path, ignore_errors=True)
            raise FileNotFoundError(2, "No such file or directory", str(path))

        with patch.object(
            StateStore, "_run_git", side_effect=self._git_router(rev_parse_returncodes=[1, 1])
        ) as mock_git:
            with (
                patch("state_store.time.sleep"),
                patch("state_store.shutil.rmtree", side_effect=rmtree_after_concurrent_delete),
            ):
                # Must not raise — ENOENT means the racing caller already
                # delivered the desired post-state.
                result = store._ensure_worktree()

        assert result == wt
        # We must have continued through to recreate via `git worktree
        # add`.  Without the ENOENT-as-success branch, this code path
        # would have raised GitOperationError before reaching the add.
        worktree_add_calls = [
            c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "add")
        ]
        assert len(worktree_add_calls) == 1

    def test_transient_rev_parse_failure_recovers_on_retry(self, tmp_path):
        """First rev-parse fails, second succeeds — worktree must be reused
        without recreate.  Locks in the #1396 retry behavior so future
        refactors don't accidentally collapse it to a single attempt."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /fake\n")
        (wt / "preserved.txt").write_text("survives transient contention")

        with patch.object(
            StateStore, "_run_git", side_effect=self._git_router(rev_parse_returncodes=[1, 0])
        ) as mock_git:
            with patch("state_store.time.sleep"):
                result = store._ensure_worktree()

        assert result == wt
        # No recreate: rmtree never ran (file is preserved) and `git worktree
        # add` was not invoked.
        assert (wt / "preserved.txt").exists()
        worktree_add_calls = [
            c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "add")
        ]
        assert worktree_add_calls == []

    def test_force_removes_admin_dir_when_worktree_present(self, tmp_path):
        """`_remove_stale_admin_dir(force=True)` clears the admin dir even
        when the worktree directory still exists. The default behavior
        (force=False) preserves the admin dir in that case."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()  # worktree dir exists

        admin_dir = store.repo_path / ".git" / "worktrees" / "wt"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(f"{wt}/.git\n")

        # Default: admin dir is preserved because wt exists.
        store._remove_stale_admin_dir()
        assert admin_dir.exists()

        # Forced: admin dir is removed.
        store._remove_stale_admin_dir(force=True)
        assert not admin_dir.exists()


class TestBranchHeldByPrunableWorktree:
    """Regression tests for #2167 — ``git worktree add`` must self-heal
    when the state branch is pinned by an admin dir whose worktree
    directory has vanished (``prunable``).  This bites when the
    deployment-side worktree path changes (single-repo → multi-repo)
    and the legacy admin dir survives, or when the state volume is
    wiped while the admin dir under ``<repo>/.git/worktrees/`` does not.
    Either way every state load 500'd until the admin dir was hand-pruned.
    """

    def _make_store(self, tmp_path):
        from state_store import StateStore

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()
        worktree_dir = tmp_path / "wt"
        store = StateStore(repo_path, worktree_dir=worktree_dir)
        return store, worktree_dir

    def test_recovers_from_branch_held_by_prunable_path(self, tmp_path):
        """Stale admin dir for a vanished path holds the branch — the
        first ``worktree add`` fails, we drop the matching admin dir,
        and the retry succeeds.  This is the exact wedge from #2167:
        legacy ``pipeline-worktree`` admin dir survives, new
        ``pipeline-worktree-egg`` add fails, every state load 500s."""
        store, wt = self._make_store(tmp_path)

        # A vanished prunable worktree path.  The directory does not
        # exist; only the admin dir does.
        stale_path = tmp_path / "old-pipeline-worktree"
        admin_dir = store.repo_path / ".git" / "worktrees" / "old-pipeline-worktree"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(f"{stale_path}/.git\n")

        # Two-call mock: first `worktree add` fails with the contention
        # error, second succeeds after the admin dir is cleared.
        call_count = {"n": 0}

        def fake_run(*args, check=True, cwd=None):
            if args[:2] == ("worktree", "add"):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise GitOperationError(
                        f"Git command failed: fatal: 'egg/pipeline-state' "
                        f"is already used by worktree at '{stale_path}'\n"
                    )
                return MagicMock(stdout="", returncode=0)
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0)
            return MagicMock(stdout="", returncode=0)

        with patch.object(StateStore, "_run_git", side_effect=fake_run):
            result = store._ensure_worktree()

        assert result == wt
        assert call_count["n"] == 2, "expected exactly one retry after admin-dir cleanup"
        assert not admin_dir.exists(), "stale admin dir should have been removed"

    def test_does_not_prune_when_holding_path_still_exists(self, tmp_path):
        """If the path mentioned in the error STILL exists on disk, a
        live worktree genuinely holds the branch — refuse to touch it
        and re-raise the original error.  Safety guard against pruning
        a real worktree (e.g., a gateway-managed container worktree if
        paths ever overlap)."""
        store, wt = self._make_store(tmp_path)

        live_path = tmp_path / "live-worktree"
        live_path.mkdir()  # the holding worktree is real
        admin_dir = store.repo_path / ".git" / "worktrees" / "live-worktree"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(f"{live_path}/.git\n")

        def fake_run(*args, check=True, cwd=None):
            if args[:2] == ("worktree", "add"):
                raise GitOperationError(
                    f"Git command failed: fatal: 'egg/pipeline-state' "
                    f"is already used by worktree at '{live_path}'\n"
                )
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0)
            return MagicMock(stdout="", returncode=0)

        with patch.object(StateStore, "_run_git", side_effect=fake_run):
            with pytest.raises(GitOperationError, match="is already used by worktree"):
                store._ensure_worktree()

        # Admin dir for the live worktree must NOT have been removed.
        assert admin_dir.exists()

    def test_unrelated_git_failure_propagates(self, tmp_path):
        """Failure messages that are NOT the branch-contention pattern
        must propagate unmodified.  We only self-heal one specific
        error — anything else surfaces to the route as 500."""
        store, wt = self._make_store(tmp_path)

        def fake_run(*args, check=True, cwd=None):
            if args[:2] == ("worktree", "add"):
                raise GitOperationError("Git command failed: fatal: out of disk space")
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0)
            return MagicMock(stdout="", returncode=0)

        with patch.object(StateStore, "_run_git", side_effect=fake_run):
            with pytest.raises(GitOperationError, match="out of disk space"):
                store._ensure_worktree()

    def test_remove_admin_dir_for_path_independent_of_self_worktree(self, tmp_path):
        """``_remove_admin_dir_for_path`` must match by the supplied
        path, not by ``self._worktree_dir`` — that's the whole point
        for #2167, where the orphaned admin dir references the
        legacy path that the new StateStore no longer uses."""
        store, _wt = self._make_store(tmp_path)

        unrelated_path = tmp_path / "different-worktree"
        admin_dir = store.repo_path / ".git" / "worktrees" / "different-worktree"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(f"{unrelated_path}/.git\n")

        assert store._remove_admin_dir_for_path(unrelated_path) is True
        assert not admin_dir.exists()
        # Calling again — nothing left to remove.
        assert store._remove_admin_dir_for_path(unrelated_path) is False

    def test_concurrent_callers_serialize_through_git_op(self, tmp_path):
        """Two callers entering ``_ensure_worktree`` concurrently must
        both succeed.  Pre-fix (#2177), the recovery sequence
        ``worktree add`` (fail) → ``_remove_admin_dir_for_path`` →
        ``worktree add`` (retry) released ``_git_op`` between the
        inner ``_run_git`` calls, so the loser found the admin dir
        already cleaned (returned False) and re-raised the original
        ``GitOperationError`` as a misleading one-shot 500.  Wrapping
        the bring-up sequence in ``_git_op`` makes the loser block on
        the lock and observe a healthy worktree on entry."""
        store_seed, wt = self._make_store(tmp_path)

        # Wedge state: admin dir for a vanished path holds the branch.
        stale_path = tmp_path / "old-pipeline-worktree"
        admin_dir = store_seed.repo_path / ".git" / "worktrees" / "old-pipeline-worktree"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(f"{stale_path}/.git\n")

        add_call_count = {"n": 0}
        depth_when_removing_admin: list[int] = []
        counter_lock = threading.Lock()
        errors: list[BaseException] = []
        results: list = []

        def fake_run(*args, check=True, cwd=None):
            if args[:2] == ("worktree", "add"):
                with counter_lock:
                    add_call_count["n"] += 1
                    is_first = add_call_count["n"] == 1
                if is_first:
                    raise GitOperationError(
                        f"Git command failed: fatal: 'egg/pipeline-state' "
                        f"is already used by worktree at '{stale_path}'\n"
                    )
                # Retry: materialize wt so subsequent rev-parse + the
                # other thread's wt.exists() probe both observe a
                # healthy worktree.
                wt.mkdir(parents=True, exist_ok=True)
                (wt / ".git").touch()
                return MagicMock(stdout="", returncode=0)
            if args[:2] == ("rev-parse", "--is-inside-work-tree"):
                return MagicMock(
                    stdout="",
                    returncode=0 if (wt / ".git").exists() else 1,
                )
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0)
            return MagicMock(stdout="", returncode=0)

        original_remove = StateStore._remove_admin_dir_for_path

        def tracked_remove(self, target_path):
            # _flock_depth >= 2 proves *both* wraps are in place: the
            # outer one in _ensure_worktree (depth 1) and the inner one
            # in _add_worktree_with_branch_recovery (depth 2).  A weaker
            # ``> 0`` assertion would still pass if a future refactor
            # dropped the outer wrap and left only the inner one — and
            # that would re-open the #2177 race.
            depth_when_removing_admin.append(StateStore._flock_depth)
            return original_remove(self, target_path)

        def caller():
            try:
                s = StateStore(store_seed.repo_path, worktree_dir=wt)
                results.append(s._ensure_worktree())
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(StateStore, "_run_git", side_effect=fake_run),
            patch.object(
                StateStore,
                "_remove_admin_dir_for_path",
                tracked_remove,
            ),
        ):
            threads = [threading.Thread(target=caller) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert errors == [], f"concurrent callers must not raise: {errors}"
        assert results == [wt, wt]
        # Exactly one fail+retry pair across both threads — the loser
        # never re-entered the recovery path; it observed the wt as
        # healthy under the lock and short-circuited.
        assert add_call_count["n"] == 2, (
            f"expected one fail + one retry, got {add_call_count['n']} adds"
        )
        # The recovery removed the admin dir while holding the lock.
        # Depth must be >= 2: the outer wrap in _ensure_worktree
        # contributes 1, and the inner wrap in
        # _add_worktree_with_branch_recovery contributes another.  A
        # weaker ``> 0`` check would still pass with only the inner
        # wrap present, which would re-open the #2177 race.
        assert depth_when_removing_admin, "recovery never ran"
        assert all(d >= 2 for d in depth_when_removing_admin), (
            f"_remove_admin_dir_for_path ran without both _git_op wraps: depths={depth_when_removing_admin}"
        )
        assert not admin_dir.exists()


class TestEnsureWorktreeRepoPathGuard:
    """Regression: ``_ensure_worktree`` must reject a non-repo
    ``repo_path`` with an actionable error instead of letting ``git
    worktree add`` produce an opaque "not a git repository" failure.

    Repro case: a deployment sets ``EGG_REPO_PATH`` to a parent dir
    containing several repos and a caller constructs ``StateStore``
    directly (bypassing ``get_state_store``'s multi-repo discovery)."""

    def test_non_repo_path_raises_actionable_error(self, tmp_path):
        """``repo_path`` without a ``.git`` entry raises StateStoreError
        naming ``EGG_REPO_PATH`` so operators can fix the env var."""
        parent = tmp_path / "repos-parent"
        parent.mkdir()
        # No `.git` under parent — it's a parent of repos, not a repo.
        store = StateStore(parent, worktree_dir=tmp_path / "wt")

        with pytest.raises(StateStoreError) as exc_info:
            store._ensure_worktree()

        msg = str(exc_info.value)
        assert "not a git repository" in msg
        assert "EGG_REPO_PATH" in msg
        assert str(parent) in msg

    def test_repo_path_with_dot_git_passes_guard(self, tmp_path, mock_git):
        """``repo_path`` with a ``.git`` dir clears the guard.

        With ``mock_git`` returning success for every git invocation and
        the worktree dir present on disk, ``_ensure_worktree`` takes the
        ``rev-parse --is-inside-work-tree`` healthy fast path and returns
        without reaching the orphan-branch logic.  That is fine for this
        test — the only assertion is that the new fail-fast guard does
        not trigger when ``.git`` is present; orphan-branch behaviour is
        covered by other tests in this module.
        """
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()
        wt = tmp_path / "wt"
        wt.mkdir(parents=True, exist_ok=True)
        store = StateStore(repo_path, worktree_dir=wt)

        # Should not raise — guard passes, fast path returns the
        # healthy worktree.
        assert store._ensure_worktree() == wt


class TestStateWorktreeLocked:
    """Regression tests for #2324 — the state worktree must be marked
    ``git worktree lock``ed so a ``git worktree prune`` invoked from a
    different pod (with a different ``emptyDir`` view of the state mount)
    does not delete its admin dir from the shared bare repo."""

    def _make_store(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()
        worktree_dir = tmp_path / "wt"
        store = StateStore(repo_path, worktree_dir=worktree_dir)
        return store, worktree_dir

    def _make_router(self, *, rev_parse_returncodes, branch_exists):
        rev_parse_iter = iter(rev_parse_returncodes)

        def run_git(*args, check=True, cwd=None):
            if args[:2] == ("rev-parse", "--is-inside-work-tree"):
                return MagicMock(stdout="", returncode=next(rev_parse_iter))
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0 if branch_exists else 1)
            return MagicMock(stdout="", returncode=0)

        return run_git

    def test_lock_called_after_existing_branch_recreate(self, tmp_path):
        """When the existing-branch path recreates the worktree,
        ``git worktree lock`` must run on the new worktree."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /broken\n")

        with patch.object(
            StateStore,
            "_run_git",
            side_effect=self._make_router(rev_parse_returncodes=[1, 1], branch_exists=True),
        ) as mock_git:
            with patch("state_store.time.sleep"):
                store._ensure_worktree()

        lock_calls = [c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "lock")]
        assert len(lock_calls) == 1
        assert lock_calls[0].args[2] == str(wt)

    def test_lock_called_after_orphan_branch_create(self, tmp_path):
        """When the orphan-branch path creates the worktree on first
        run, ``git worktree lock`` must run on the new worktree."""
        store, wt = self._make_store(tmp_path)
        # First run: no existing worktree, no branch — orphan path.

        # `git worktree add` creates the directory in real life; mirror
        # that side effect so the post-add cleanup loop has something
        # to iterate.
        def run_git(*args, check=True, cwd=None):
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=1)  # branch missing
            if args[:3] == ("worktree", "add", "--detach"):
                wt.mkdir(parents=True, exist_ok=True)
            return MagicMock(stdout="", returncode=0)

        with (
            patch.object(StateStore, "_run_git", side_effect=run_git) as mock_git,
            patch.object(StateStore, "_restore_from_remote", return_value=False),
        ):
            store._ensure_worktree()

        lock_calls = [c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "lock")]
        assert len(lock_calls) == 1
        assert lock_calls[0].args[2] == str(wt)

    def test_lock_called_on_healthy_existing_worktree(self, tmp_path):
        """Existing worktrees that pre-date this fix must self-heal:
        on the healthy fast path we still call ``worktree lock`` so
        the next gateway prune cannot remove the admin dir."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /fake\n")

        with patch.object(
            StateStore,
            "_run_git",
            side_effect=self._make_router(rev_parse_returncodes=[0], branch_exists=True),
        ) as mock_git:
            store._ensure_worktree()

        lock_calls = [c for c in mock_git.call_args_list if c.args[:2] == ("worktree", "lock")]
        assert len(lock_calls) == 1
        assert lock_calls[0].args[2] == str(wt)

    def test_lock_already_locked_is_not_warned(self, tmp_path, caplog):
        """A re-entry on an already-locked worktree must not warn —
        ``git worktree lock`` exits non-zero with 'already locked' and
        we treat that as success."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /fake\n")

        def run_git(*args, check=True, cwd=None):
            if args[:2] == ("rev-parse", "--is-inside-work-tree"):
                return MagicMock(stdout="", returncode=0)
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0)
            if args[:2] == ("worktree", "lock"):
                return MagicMock(
                    stdout="",
                    stderr="fatal: '/path' is already locked",
                    returncode=128,
                )
            return MagicMock(stdout="", returncode=0)

        with patch.object(StateStore, "_run_git", side_effect=run_git):
            import logging

            with caplog.at_level(logging.WARNING, logger="orchestrator.state_store"):
                store._ensure_worktree()

        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("Failed to lock state worktree" in m for m in warning_msgs)

    def test_lock_failure_does_not_raise(self, tmp_path):
        """Other lock failures must be logged but not propagated —
        locking is best-effort; a missing lock is preferable to a
        wedged state store."""
        store, wt = self._make_store(tmp_path)
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /fake\n")

        def run_git(*args, check=True, cwd=None):
            if args[:2] == ("rev-parse", "--is-inside-work-tree"):
                return MagicMock(stdout="", returncode=0)
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(stdout="", returncode=0)
            if args[:2] == ("worktree", "lock"):
                return MagicMock(
                    stdout="",
                    stderr="fatal: some other failure",
                    returncode=128,
                )
            return MagicMock(stdout="", returncode=0)

        with patch.object(StateStore, "_run_git", side_effect=run_git):
            # Must not raise.
            store._ensure_worktree()
