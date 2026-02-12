"""Tests for checkpoint models and utilities."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import json

import pytest

from egg_contracts.checkpoints import (
    Checkpoint,
    CheckpointIndex,
    CheckpointSummary,
    FileOperation,
    FileOperationType,
    Message,
    MessageRole,
    SessionMetadata,
    TokenUsage,
    ToolCall,
    Transcript,
)
from egg_contracts.checkpoint_loader import (
    CheckpointLoadError,
    CheckpointSaveError,
    generate_checkpoint_id,
    generate_checkpoint_id_from_commit,
    get_checkpoint_filename,
    get_checkpoint_path,
    load_checkpoint,
    save_checkpoint,
    add_checkpoint_to_index,
    load_checkpoint_index,
    list_checkpoints,
)


class TestSessionMetadata:
    """Tests for SessionMetadata model."""

    def test_minimal_session_metadata(self):
        """Test creating session metadata with minimal fields."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session-123",
            started_at=now,
        )
        assert session.session_id == "test-session-123"
        assert session.started_at == now
        assert session.container_id is None
        assert session.agent_role is None
        assert session.duration_seconds is None

    def test_full_session_metadata(self):
        """Test creating session metadata with all fields."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session-456",
            container_id="egg-abc123",
            agent_role="coder",
            started_at=now,
            ended_at=now + timedelta(hours=1),
            duration_seconds=3600.0,
            model="claude-opus-4-5-20251101",
            claude_code_version="2.1.29",
        )
        assert session.duration_seconds == 3600.0
        assert session.model == "claude-opus-4-5-20251101"


class TestMessage:
    """Tests for Message model."""

    def test_user_message(self):
        """Test creating a user message."""
        now = datetime.now(UTC)
        msg = Message(
            role=MessageRole.USER,
            content="Hello, please help me.",
            timestamp=now,
        )
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello, please help me."

    def test_assistant_message_with_summary(self):
        """Test assistant message with content summary."""
        now = datetime.now(UTC)
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="Very long response...",
            content_summary="[Content truncated: 10000 characters]",
            timestamp=now,
        )
        assert msg.content_summary is not None


class TestTranscript:
    """Tests for Transcript model."""

    def test_empty_transcript(self):
        """Test empty transcript."""
        transcript = Transcript()
        assert transcript.messages == []
        assert transcript.message_count == 0
        assert transcript.truncated is False

    def test_transcript_with_messages(self):
        """Test transcript with messages."""
        now = datetime.now(UTC)
        messages = [
            Message(role=MessageRole.USER, content="Hello", timestamp=now),
            Message(role=MessageRole.ASSISTANT, content="Hi there!", timestamp=now),
        ]
        transcript = Transcript(
            messages=messages,
            message_count=2,
        )
        assert len(transcript.messages) == 2
        assert transcript.message_count == 2


class TestToolCall:
    """Tests for ToolCall model."""

    def test_tool_call(self):
        """Test creating a tool call."""
        now = datetime.now(UTC)
        tc = ToolCall(
            name="Bash",
            tool_use_id="toolu_123",
            parameters={"command": "ls -la"},
            result_summary="file1.txt\nfile2.txt",
            success=True,
            duration_ms=150.0,
            timestamp=now,
        )
        assert tc.name == "Bash"
        assert tc.parameters["command"] == "ls -la"
        assert tc.success is True


class TestFileOperation:
    """Tests for FileOperation model."""

    def test_file_operations(self):
        """Test different file operation types."""
        now = datetime.now(UTC)

        read_op = FileOperation(
            path="src/main.py",
            operation=FileOperationType.READ,
            timestamp=now,
        )
        assert read_op.operation == FileOperationType.READ

        write_op = FileOperation(
            path="src/new.py",
            operation=FileOperationType.WRITE,
            timestamp=now,
        )
        assert write_op.operation == FileOperationType.WRITE


class TestTokenUsage:
    """Tests for TokenUsage model."""

    def test_token_usage(self):
        """Test token usage calculation."""
        usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_creation_tokens=100,
            total_tokens=1500,
            estimated_cost_usd=0.10,
        )
        assert usage.calculate_total() == 1500

    def test_default_token_usage(self):
        """Test default token usage values."""
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.total_tokens == 0


class TestCheckpoint:
    """Tests for Checkpoint model."""

    def test_minimal_checkpoint(self):
        """Test creating a minimal checkpoint."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session",
            started_at=now,
        )
        checkpoint = Checkpoint(
            id="ckpt-abc123def456",
            commit_sha="abc1234",
            session=session,
            created_at=now,
        )
        assert checkpoint.id == "ckpt-abc123def456"
        assert checkpoint.commit_sha == "abc1234"
        assert checkpoint.transcript is None
        assert checkpoint.files_touched == []

    def test_full_checkpoint(self):
        """Test creating a full checkpoint."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session",
            started_at=now,
            agent_role="coder",
        )
        transcript = Transcript(
            messages=[
                Message(role=MessageRole.USER, content="Hello", timestamp=now),
            ],
            message_count=1,
        )
        tool_calls = [
            ToolCall(name="Bash", parameters={"command": "ls"}, timestamp=now),
        ]
        files_touched = [
            FileOperation(path="src/main.py", operation=FileOperationType.READ),
        ]
        token_usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
        )

        checkpoint = Checkpoint(
            id="ckpt-abc123def456",
            commit_sha="abc1234567890",
            session=session,
            transcript=transcript,
            files_touched=files_touched,
            tool_calls=tool_calls,
            token_usage=token_usage,
            issue_number=123,
            pipeline_phase="implement",
            branch="egg/feature-123",
            created_at=now,
        )

        assert checkpoint.issue_number == 123
        assert checkpoint.pipeline_phase == "implement"
        assert len(checkpoint.tool_calls) == 1
        assert len(checkpoint.files_touched) == 1

    def test_checkpoint_helper_methods(self):
        """Test checkpoint helper methods."""
        now = datetime.now(UTC)
        session = SessionMetadata(session_id="test", started_at=now)
        checkpoint = Checkpoint(
            id="ckpt-abc123def456",
            commit_sha="abc1234",
            session=session,
            created_at=now,
            tool_calls=[
                ToolCall(name="Bash", parameters={}, timestamp=now),
                ToolCall(name="Bash", parameters={}, timestamp=now),
                ToolCall(name="Read", parameters={}, timestamp=now),
            ],
            files_touched=[
                FileOperation(path="a.py", operation=FileOperationType.READ),
                FileOperation(path="b.py", operation=FileOperationType.WRITE),
                FileOperation(path="c.py", operation=FileOperationType.EDIT),
            ],
        )

        bash_calls = checkpoint.get_tool_calls_by_name("Bash")
        assert len(bash_calls) == 2

        read_ops = checkpoint.get_files_by_operation(FileOperationType.READ)
        assert len(read_ops) == 1

        written = checkpoint.get_files_written()
        assert len(written) == 2
        assert "b.py" in written
        assert "c.py" in written

        read = checkpoint.get_files_read()
        assert len(read) == 1
        assert "a.py" in read

    def test_checkpoint_id_validation(self):
        """Test checkpoint ID pattern validation."""
        now = datetime.now(UTC)
        session = SessionMetadata(session_id="test", started_at=now)

        # Valid IDs
        valid_ids = [
            "ckpt-abc12345",
            "ckpt-1234567890ab",
            "ckpt-abcdef12",
        ]
        for cp_id in valid_ids:
            checkpoint = Checkpoint(
                id=cp_id,
                commit_sha="abc1234",
                session=session,
                created_at=now,
            )
            assert checkpoint.id == cp_id

        # Invalid IDs
        with pytest.raises(ValueError):
            Checkpoint(
                id="invalid-id",
                commit_sha="abc1234",
                session=session,
                created_at=now,
            )


class TestCheckpointSummary:
    """Tests for CheckpointSummary model."""

    def test_from_checkpoint(self):
        """Test creating summary from checkpoint."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session",
            started_at=now,
            agent_role="coder",
        )
        transcript = Transcript(message_count=5)
        token_usage = TokenUsage(total_tokens=1500)

        checkpoint = Checkpoint(
            id="ckpt-abc123def456",
            commit_sha="abc1234567890",
            session=session,
            transcript=transcript,
            tool_calls=[
                ToolCall(name="Bash", parameters={}, timestamp=now),
                ToolCall(name="Read", parameters={}, timestamp=now),
            ],
            token_usage=token_usage,
            issue_number=42,
            branch="egg/test",
            pipeline_phase="implement",
            created_at=now,
        )

        summary = CheckpointSummary.from_checkpoint(checkpoint)

        assert summary.id == checkpoint.id
        assert summary.commit_sha == checkpoint.commit_sha
        assert summary.session_id == "test-session"
        assert summary.agent_role == "coder"
        assert summary.issue_number == 42
        assert summary.branch == "egg/test"
        assert summary.message_count == 5
        assert summary.tool_call_count == 2
        assert summary.total_tokens == 1500


class TestCheckpointIndex:
    """Tests for CheckpointIndex model."""

    def test_empty_index(self):
        """Test empty index."""
        now = datetime.now(UTC)
        index = CheckpointIndex(last_updated=now)
        assert len(index.checkpoints) == 0

    def test_index_lookups(self):
        """Test index lookup methods."""
        now = datetime.now(UTC)
        summaries = [
            CheckpointSummary(
                id="ckpt-aa00000001",
                commit_sha="abc1234567890",
                session_id="session1",
                issue_number=100,
                branch="egg/feature-100",
                created_at=now,
            ),
            CheckpointSummary(
                id="ckpt-bb00000002",
                commit_sha="def4567890abc",
                session_id="session2",
                issue_number=100,
                branch="egg/feature-100",
                created_at=now,
            ),
            CheckpointSummary(
                id="ckpt-cc00000003",
                commit_sha="0123456789abcd",
                session_id="session3",
                issue_number=200,
                branch="egg/feature-200",
                created_at=now,
            ),
        ]
        index = CheckpointIndex(last_updated=now, checkpoints=summaries)

        # Test get_by_commit
        result = index.get_by_commit("abc1234567890")
        assert result is not None
        assert result.id == "ckpt-aa00000001"

        # Test partial commit match
        result = index.get_by_commit("abc1234")
        assert result is not None
        assert result.id == "ckpt-aa00000001"

        # Test get_by_issue
        issue_100 = index.get_by_issue(100)
        assert len(issue_100) == 2

        issue_200 = index.get_by_issue(200)
        assert len(issue_200) == 1

        # Test get_by_branch
        branch_100 = index.get_by_branch("egg/feature-100")
        assert len(branch_100) == 2


class TestCheckpointLoader:
    """Tests for checkpoint loading/saving utilities."""

    def test_generate_checkpoint_id(self):
        """Test checkpoint ID generation."""
        id1 = generate_checkpoint_id()
        id2 = generate_checkpoint_id()

        # Should start with ckpt-
        assert id1.startswith("ckpt-")
        assert id2.startswith("ckpt-")

        # Should be unique
        assert id1 != id2

        # Should be valid hex after prefix
        hex_part = id1[5:]
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_generate_checkpoint_id_from_commit(self):
        """Test deterministic checkpoint ID generation with timestamp."""
        # With same timestamp, same inputs produce same output
        fixed_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        id1 = generate_checkpoint_id_from_commit("abc123", "session-1", fixed_time)
        id2 = generate_checkpoint_id_from_commit("abc123", "session-1", fixed_time)
        id3 = generate_checkpoint_id_from_commit("abc123", "session-2", fixed_time)

        # Same inputs (including timestamp) should produce same output
        assert id1 == id2

        # Different session should produce different output
        assert id1 != id3

        # Different timestamp should produce different output
        later_time = datetime(2025, 1, 15, 12, 0, 1, tzinfo=UTC)
        id4 = generate_checkpoint_id_from_commit("abc123", "session-1", later_time)
        assert id1 != id4

        # Generated ID should be 16 hex chars (8 bytes)
        assert id1.startswith("ckpt-")
        hex_part = id1[5:]
        assert len(hex_part) == 16
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_get_checkpoint_path(self):
        """Test checkpoint path generation."""
        base = Path("/tmp/checkpoints")
        path = get_checkpoint_path(base, "ckpt-ab123456")

        # Should use first 2 chars after prefix as subdirectory
        assert path.parent.name == "ab"
        assert path.name == "ckpt-ab123456.json"

    def test_save_and_load_checkpoint(self):
        """Test saving and loading a checkpoint."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            now = datetime.now(UTC)
            session = SessionMetadata(session_id="test", started_at=now)
            checkpoint = Checkpoint(
                id="ckpt-abc123def456",
                commit_sha="abc1234567890",
                session=session,
                created_at=now,
                issue_number=42,
            )

            checkpoint_path = get_checkpoint_path(tmppath, checkpoint.id)

            # Save
            save_checkpoint(checkpoint, checkpoint_path)
            assert checkpoint_path.exists()

            # Load
            loaded = load_checkpoint(checkpoint_path)
            assert loaded.id == checkpoint.id
            assert loaded.commit_sha == checkpoint.commit_sha
            assert loaded.issue_number == 42

    def test_load_nonexistent_checkpoint(self):
        """Test loading a non-existent checkpoint."""
        with pytest.raises(CheckpointLoadError):
            load_checkpoint(Path("/nonexistent/path.json"))

    def test_add_checkpoint_to_index(self):
        """Test adding checkpoint to index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            index_path = tmppath / "index.json"

            now = datetime.now(UTC)
            session = SessionMetadata(session_id="test", started_at=now)
            checkpoint = Checkpoint(
                id="ckpt-abc123def456",
                commit_sha="abc1234567890",
                session=session,
                created_at=now,
            )

            # Add to index
            index = add_checkpoint_to_index(checkpoint, index_path)

            assert len(index.checkpoints) == 1
            assert index.checkpoints[0].id == checkpoint.id

            # Add another
            checkpoint2 = Checkpoint(
                id="ckpt-def456789012",
                commit_sha="def4567890abc",
                session=session,
                created_at=now,
            )
            index = add_checkpoint_to_index(checkpoint2, index_path)

            assert len(index.checkpoints) == 2

    def test_list_checkpoints_empty(self):
        """Test listing checkpoints from empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            checkpoints = list_checkpoints(tmppath)
            assert checkpoints == []

    def test_list_checkpoints_with_filter(self):
        """Test listing checkpoints with filters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            now = datetime.now(UTC)
            session = SessionMetadata(session_id="test", started_at=now)

            # Create some checkpoints
            for i in range(3):
                cp = Checkpoint(
                    id=f"ckpt-aa{i:010d}",
                    commit_sha=f"abcdef{i:034d}",
                    session=session,
                    created_at=now - timedelta(hours=i),
                    issue_number=100 if i < 2 else 200,
                    branch="egg/test-100" if i < 2 else "egg/test-200",
                )
                path = get_checkpoint_path(tmppath, cp.id)
                save_checkpoint(cp, path)

            # List all
            all_cps = list_checkpoints(tmppath)
            assert len(all_cps) == 3

            # Filter by issue
            issue_100 = list_checkpoints(tmppath, issue_number=100)
            assert len(issue_100) == 2

            # Filter by branch
            branch_200 = list_checkpoints(tmppath, branch="egg/test-200")
            assert len(branch_200) == 1

            # With limit
            limited = list_checkpoints(tmppath, limit=2)
            assert len(limited) == 2
