"""
Tests for state store.

Note: Git operations are mocked since git init is not available in the sandbox.
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
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
        with pytest.raises(StateStoreError):
            state_store.create_pipeline(
                issue_number=496,
                repo="owner/repo",
                branch="egg/issue-496",
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

    def test_invalid_wrong_prefix(self):
        """Test pipeline ID with wrong prefix is rejected."""
        with pytest.raises(InvalidPipelineIdError):
            _validate_pipeline_id("pr-123")

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
        """True concurrent test: force interleaving via threading.Barrier.

        Reproduces the exact race from issue-647:
        - Thread A: loads pipeline (stale), pauses, then saves (clobbering)
        - Thread B: resolves decision (load → modify → save) while A is paused

        Without the lock, Thread A's save overwrites Thread B's resolution.
        With the lock, Thread A blocks until Thread B completes.
        """
        import threading
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

        # Barrier ensures both threads reach the critical section together.
        # Thread A loads stale state, hits the barrier, then saves.
        # Thread B resolves the decision (under lock) and hits the barrier.
        barrier = threading.Barrier(2, timeout=5)
        errors: list[Exception] = []

        original_load = state_store.load_pipeline

        def patched_load_for_update(pid):
            """Intercept load inside update_pipeline to inject a pause."""
            result = original_load(pid)
            # Signal that we've loaded (stale) state, wait for Thread B
            barrier.wait()
            return result

        def thread_a_update():
            """Simulate PATCH /pipelines — loads stale state, pauses, saves."""
            try:
                with mock_patch.object(
                    state_store, "load_pipeline", side_effect=patched_load_for_update
                ):
                    state_store.update_pipeline(pipeline_id, {"status": "awaiting_human"})
            except Exception as exc:
                errors.append(exc)

        def thread_b_resolve():
            """Simulate POST /decisions/resolve — resolves while A holds stale state."""
            try:
                with mock_patch("decision_queue.get_state_store", return_value=state_store):
                    dq_b = DecisionQueue(pipeline_id, state_store.repo_path)
                    # Wait until Thread A has loaded stale state
                    barrier.wait()
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

        # The decision must still be RESOLVED — Thread A must not clobber it
        final = state_store.load_pipeline(pipeline_id)
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
        from state_store import _pipeline_state_locks, release_pipeline_state_lock

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

        lock_file = tmp_path / ".git-ops.lock"
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
