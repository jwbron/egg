"""Tests for checkpoint models and utilities."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from egg_contracts.checkpoint_loader import (
    generate_checkpoint_id_from_commit,
    get_checkpoint_path,
)
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    FileOperation,
    FileOperationType,
    Message,
    MessageRole,
    SessionMetadata,
    SessionStatus,
    TokenUsage,
    ToolCall,
    Transcript,
    TriggerType,
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


class TestCheckpointLoaderUtils:
    """Tests for shared checkpoint loading utilities."""

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


# ==============================================================================
# V2 Model Tests
# ==============================================================================


class TestTriggerType:
    """Tests for TriggerType enum."""

    def test_enum_values(self):
        """Test TriggerType enum has expected values."""
        assert TriggerType.COMMIT == "commit"
        assert TriggerType.SESSION_END == "session_end"

    def test_serialization(self):
        """Test TriggerType values can be used as strings."""
        assert TriggerType("commit") == TriggerType.COMMIT
        assert TriggerType("session_end") == TriggerType.SESSION_END

    def test_invalid_value(self):
        """Test invalid TriggerType raises ValueError."""
        with pytest.raises(ValueError):
            TriggerType("invalid")


class TestSessionStatus:
    """Tests for SessionStatus enum."""

    def test_enum_values(self):
        """Test SessionStatus enum has expected values."""
        assert SessionStatus.COMPLETED == "completed"
        assert SessionStatus.EXPIRED == "expired"
        assert SessionStatus.FAILED == "failed"

    def test_serialization(self):
        """Test SessionStatus values can be used as strings."""
        assert SessionStatus("completed") == SessionStatus.COMPLETED
        assert SessionStatus("expired") == SessionStatus.EXPIRED
        assert SessionStatus("failed") == SessionStatus.FAILED

    def test_invalid_value(self):
        """Test invalid SessionStatus raises ValueError."""
        with pytest.raises(ValueError):
            SessionStatus("cancelled")


class TestAgentType:
    """Tests for AgentType enum."""

    def test_enum_values(self):
        """Test AgentType enum has expected values."""
        assert AgentType.CODER == "coder"
        assert AgentType.TESTER == "tester"
        assert AgentType.DOCUMENTER == "documenter"
        assert AgentType.INTEGRATOR == "integrator"
        assert AgentType.REVIEWER == "reviewer"
        assert AgentType.UNKNOWN == "unknown"

    def test_serialization(self):
        """Test AgentType values can be used as strings."""
        for agent_type in AgentType:
            assert AgentType(agent_type.value) == agent_type


class TestCheckpointV2:
    """Tests for CheckpointV2 model."""

    def _make_session_metadata(self, now=None):
        """Create a SessionMetadata for testing."""
        if now is None:
            now = datetime.now(UTC)
        return SessionMetadata(session_id="test-session", started_at=now)

    def test_minimal_commit_checkpoint(self):
        """Test creating a minimal commit-triggered checkpoint."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            session=session,
            created_at=now,
            session_started_at=now,
        )
        assert checkpoint.trigger_type == TriggerType.COMMIT
        assert checkpoint.commit_sha == "abc1234567890"
        assert checkpoint.session_status is None
        assert checkpoint.schemaVersion == "2.0"

    def test_session_end_checkpoint_without_commit(self):
        """Test creating a session-end checkpoint without commit_sha."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="container-456",
            session=session,
            created_at=now,
            session_started_at=now,
        )
        assert checkpoint.trigger_type == TriggerType.SESSION_END
        assert checkpoint.commit_sha is None
        assert checkpoint.session_status == SessionStatus.COMPLETED

    def test_full_checkpoint(self):
        """Test creating a full v2 checkpoint with all fields."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session",
            started_at=now,
            agent_role="coder",
            container_id="container-789",
        )
        transcript = Transcript(
            messages=[Message(role=MessageRole.USER, content="Hello", timestamp=now)],
            message_count=1,
        )
        token_usage = TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
        tool_calls = [ToolCall(name="Bash", parameters={"command": "ls"}, timestamp=now)]
        files_touched = [FileOperation(path="src/main.py", operation=FileOperationType.READ)]

        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            push_sha="def4567890abc",
            branch="egg/feature-123",
            session_id="container-789",
            issue_number=530,
            pr_number=42,
            agent_type=AgentType.CODER,
            pipeline_phase="implement",
            session=session,
            transcript=transcript,
            files_touched=files_touched,
            tool_calls=tool_calls,
            token_usage=token_usage,
            created_at=now,
            session_started_at=now,
            session_ended_at=now + timedelta(hours=1),
        )

        assert checkpoint.issue_number == 530
        assert checkpoint.pr_number == 42
        assert checkpoint.agent_type == AgentType.CODER
        assert checkpoint.pipeline_phase == "implement"
        assert checkpoint.session_ended_at is not None

    def test_optional_commit_sha_for_session_end(self):
        """Test that commit_sha is truly optional for session-end checkpoints."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.EXPIRED,
            session_id="container-expired",
            session=session,
            created_at=now,
            session_started_at=now,
        )
        assert checkpoint.commit_sha is None
        assert checkpoint.push_sha is None

    def test_session_id_required(self):
        """Test that session_id is required."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        with pytest.raises(ValueError):
            CheckpointV2(
                id="ckpt-abc123def456",
                trigger_type=TriggerType.COMMIT,
                commit_sha="abc1234",
                # session_id missing
                session=session,
                created_at=now,
                session_started_at=now,
            )

    def test_trigger_type_required(self):
        """Test that trigger_type is required."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        with pytest.raises(ValueError):
            CheckpointV2(
                id="ckpt-abc123def456",
                # trigger_type missing
                session_id="container-123",
                session=session,
                created_at=now,
                session_started_at=now,
            )

    def test_pipeline_phase_validation(self):
        """Test pipeline_phase validates against allowed values."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)

        # Valid phases
        for phase in ("refine", "plan", "implement", "pr"):
            cp = CheckpointV2(
                id="ckpt-abc123def456",
                trigger_type=TriggerType.COMMIT,
                commit_sha="abc1234",
                session_id="container-123",
                session=session,
                created_at=now,
                session_started_at=now,
                pipeline_phase=phase,
            )
            assert cp.pipeline_phase == phase

        # Invalid phase
        with pytest.raises(ValueError):
            CheckpointV2(
                id="ckpt-abc123def456",
                trigger_type=TriggerType.COMMIT,
                commit_sha="abc1234",
                session_id="container-123",
                session=session,
                created_at=now,
                session_started_at=now,
                pipeline_phase="deploy",
            )

    def test_checkpoint_id_validation(self):
        """Test checkpoint ID pattern validation."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)

        with pytest.raises(ValueError):
            CheckpointV2(
                id="invalid-id",
                trigger_type=TriggerType.COMMIT,
                commit_sha="abc1234",
                session_id="container-123",
                session=session,
                created_at=now,
                session_started_at=now,
            )

    def test_pipeline_id_optional(self):
        """Test that pipeline_id is optional and defaults to None."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            session=session,
            created_at=now,
            session_started_at=now,
        )
        assert checkpoint.pipeline_id is None

    def test_pipeline_id_set(self):
        """Test that pipeline_id can be set."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            session=session,
            created_at=now,
            session_started_at=now,
            pipeline_id="issue-42",
        )
        assert checkpoint.pipeline_id == "issue-42"

    def test_empty_commit_sha_becomes_none(self):
        """Test that empty string commit_sha is converted to None."""
        now = datetime.now(UTC)
        session = self._make_session_metadata(now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            commit_sha="",
            session_id="container-123",
            session=session,
            created_at=now,
            session_started_at=now,
        )
        assert checkpoint.commit_sha is None


class TestCheckpointSummaryV2:
    """Tests for CheckpointSummaryV2 model."""

    def test_from_checkpoint(self):
        """Test creating summary from a full v2 checkpoint."""
        now = datetime.now(UTC)
        session = SessionMetadata(
            session_id="test-session",
            started_at=now,
            agent_role="coder",
        )
        transcript = Transcript(message_count=5)
        token_usage = TokenUsage(total_tokens=1500)

        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            issue_number=42,
            pr_number=10,
            branch="egg/test",
            agent_type=AgentType.CODER,
            pipeline_phase="implement",
            session=session,
            transcript=transcript,
            tool_calls=[
                ToolCall(name="Bash", parameters={}, timestamp=now),
                ToolCall(name="Read", parameters={}, timestamp=now),
            ],
            token_usage=token_usage,
            created_at=now,
            session_started_at=now,
        )

        summary = CheckpointSummaryV2.from_checkpoint(checkpoint)

        assert summary.id == checkpoint.id
        assert summary.trigger_type == TriggerType.COMMIT
        assert summary.session_status is None
        assert summary.session_id == "container-123"
        assert summary.commit_sha == "abc1234567890"
        assert summary.issue_number == 42
        assert summary.pr_number == 10
        assert summary.branch == "egg/test"
        assert summary.agent_type == AgentType.CODER
        assert summary.pipeline_phase == "implement"
        assert summary.message_count == 5
        assert summary.tool_call_count == 2
        assert summary.total_tokens == 1500

    def test_from_checkpoint_with_pipeline_id(self):
        """Test that pipeline_id is carried into summary."""
        now = datetime.now(UTC)
        session = SessionMetadata(session_id="test-session", started_at=now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            session=session,
            created_at=now,
            session_started_at=now,
            pipeline_id="issue-42",
        )
        summary = CheckpointSummaryV2.from_checkpoint(checkpoint)
        assert summary.pipeline_id == "issue-42"

    def test_from_checkpoint_files_touched_count(self):
        """Test that files_touched_count is computed from checkpoint."""
        now = datetime.now(UTC)
        session = SessionMetadata(session_id="test-session", started_at=now)
        files = [
            FileOperation(path="src/a.py", operation=FileOperationType.READ),
            FileOperation(path="src/b.py", operation=FileOperationType.WRITE),
            FileOperation(path="src/c.py", operation=FileOperationType.EDIT),
        ]
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            session=session,
            files_touched=files,
            created_at=now,
            session_started_at=now,
        )
        summary = CheckpointSummaryV2.from_checkpoint(checkpoint)
        assert summary.files_touched_count == 3

    def test_from_checkpoint_files_touched_count_empty(self):
        """Test files_touched_count defaults to 0 when no files."""
        now = datetime.now(UTC)
        session = SessionMetadata(session_id="test-session", started_at=now)
        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc1234567890",
            session_id="container-123",
            session=session,
            created_at=now,
            session_started_at=now,
        )
        summary = CheckpointSummaryV2.from_checkpoint(checkpoint)
        assert summary.files_touched_count == 0

    def test_from_session_end_checkpoint(self):
        """Test creating summary from session-end checkpoint."""
        now = datetime.now(UTC)
        session = SessionMetadata(session_id="test-session", started_at=now)

        checkpoint = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.FAILED,
            session_id="container-456",
            session=session,
            created_at=now,
            session_started_at=now,
        )

        summary = CheckpointSummaryV2.from_checkpoint(checkpoint)

        assert summary.trigger_type == TriggerType.SESSION_END
        assert summary.session_status == SessionStatus.FAILED
        assert summary.commit_sha is None
        assert summary.message_count == 0
        assert summary.tool_call_count == 0
        assert summary.total_tokens == 0


class TestCheckpointIndexV2:
    """Tests for CheckpointIndexV2 model."""

    def _make_summary(self, **kwargs):
        """Create a CheckpointSummaryV2 for testing."""
        defaults = {
            "id": "ckpt-abc123def456",
            "trigger_type": TriggerType.COMMIT,
            "session_id": "session-1",
            "created_at": datetime.now(UTC),
        }
        defaults.update(kwargs)
        return CheckpointSummaryV2(**defaults)

    def test_empty_index(self):
        """Test empty v2 index."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(last_updated=now)
        assert len(index.checkpoints) == 0
        assert index.by_session == {}
        assert index.by_issue == {}
        assert index.by_commit == {}

    def test_get_by_session(self):
        """Test get_by_session lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_session={"session-1": ["ckpt-aa00000001", "ckpt-aa00000002"]},
        )
        assert index.get_by_session("session-1") == ["ckpt-aa00000001", "ckpt-aa00000002"]
        assert index.get_by_session("nonexistent") == []

    def test_get_by_issue(self):
        """Test get_by_issue lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_issue={"42": ["ckpt-aa00000001"]},
        )
        assert index.get_by_issue(42) == ["ckpt-aa00000001"]
        assert index.get_by_issue(999) == []

    def test_get_by_pr(self):
        """Test get_by_pr lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_pr={"10": ["ckpt-aa00000001", "ckpt-bb00000002"]},
        )
        assert index.get_by_pr(10) == ["ckpt-aa00000001", "ckpt-bb00000002"]
        assert index.get_by_pr(999) == []

    def test_get_by_commit(self):
        """Test get_by_commit lookup (1:1 mapping)."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_commit={"abc1234567890": "ckpt-aa00000001"},
        )
        assert index.get_by_commit("abc1234567890") == "ckpt-aa00000001"
        assert index.get_by_commit("nonexistent") is None

    def test_get_by_agent_type(self):
        """Test get_by_agent_type lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_agent_type={"coder": ["ckpt-aa00000001"], "tester": ["ckpt-bb00000002"]},
        )
        assert index.get_by_agent_type(AgentType.CODER) == ["ckpt-aa00000001"]
        assert index.get_by_agent_type(AgentType.TESTER) == ["ckpt-bb00000002"]
        assert index.get_by_agent_type(AgentType.UNKNOWN) == []

    def test_get_by_phase(self):
        """Test get_by_phase lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_phase={"implement": ["ckpt-aa00000001"]},
        )
        assert index.get_by_phase("implement") == ["ckpt-aa00000001"]
        assert index.get_by_phase("plan") == []

    def test_get_by_trigger(self):
        """Test get_by_trigger lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_trigger={
                "commit": ["ckpt-aa00000001"],
                "session_end": ["ckpt-bb00000002"],
            },
        )
        assert index.get_by_trigger(TriggerType.COMMIT) == ["ckpt-aa00000001"]
        assert index.get_by_trigger(TriggerType.SESSION_END) == ["ckpt-bb00000002"]

    def test_get_by_status(self):
        """Test get_by_status lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_status={
                "completed": ["ckpt-aa00000001"],
                "failed": ["ckpt-bb00000002"],
            },
        )
        assert index.get_by_status(SessionStatus.COMPLETED) == ["ckpt-aa00000001"]
        assert index.get_by_status(SessionStatus.FAILED) == ["ckpt-bb00000002"]
        assert index.get_by_status(SessionStatus.EXPIRED) == []

    def test_get_by_pipeline(self):
        """Test get_by_pipeline lookup."""
        now = datetime.now(UTC)
        index = CheckpointIndexV2(
            last_updated=now,
            by_pipeline={
                "issue-42": ["ckpt-aa00000001", "ckpt-bb00000002"],
                "issue-99": ["ckpt-cc00000003"],
            },
        )
        assert index.get_by_pipeline("issue-42") == ["ckpt-aa00000001", "ckpt-bb00000002"]
        assert index.get_by_pipeline("issue-99") == ["ckpt-cc00000003"]
        assert index.get_by_pipeline("nonexistent") == []
