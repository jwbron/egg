"""Tests for v2 checkpoint loader functions."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from egg_contracts.checkpoint_loader import (
    CheckpointLoadError,
    add_checkpoint_to_index_v2,
    generate_checkpoint_id_v2,
    load_checkpoint_index_v2,
    load_checkpoint_v2,
    save_checkpoint_index_v2,
    save_checkpoint_v2,
)
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointV2,
    SessionMetadata,
    SessionStatus,
    TokenUsage,
    ToolCall,
    Transcript,
    TriggerType,
)


def _make_v2_checkpoint(
    checkpoint_id="ckpt-abc123def456",
    trigger_type=TriggerType.COMMIT,
    session_id="container-123",
    commit_sha="abc1234567890",
    session_status=None,
    issue_number=None,
    pr_number=None,
    agent_type=AgentType.UNKNOWN,
    pipeline_phase=None,
    branch=None,
    now=None,
):
    """Helper to create a v2 checkpoint for testing."""
    if now is None:
        now = datetime.now(UTC)
    session = SessionMetadata(session_id="test-session", started_at=now)
    return CheckpointV2(
        id=checkpoint_id,
        trigger_type=trigger_type,
        session_status=session_status,
        commit_sha=commit_sha,
        session_id=session_id,
        issue_number=issue_number,
        pr_number=pr_number,
        agent_type=agent_type,
        pipeline_phase=pipeline_phase,
        branch=branch,
        session=session,
        created_at=now,
        session_started_at=now,
    )


class TestGenerateCheckpointIdV2:
    """Tests for generate_checkpoint_id_v2 function."""

    def test_determinism(self):
        """Test same inputs produce same output."""
        fixed_time = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        id1 = generate_checkpoint_id_v2("session-1", fixed_time)
        id2 = generate_checkpoint_id_v2("session-1", fixed_time)
        assert id1 == id2

    def test_uniqueness_different_session(self):
        """Test different session IDs produce different IDs."""
        fixed_time = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        id1 = generate_checkpoint_id_v2("session-1", fixed_time)
        id2 = generate_checkpoint_id_v2("session-2", fixed_time)
        assert id1 != id2

    def test_uniqueness_different_timestamp(self):
        """Test different timestamps produce different IDs."""
        time1 = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        time2 = datetime(2025, 6, 15, 12, 0, 1, tzinfo=UTC)
        id1 = generate_checkpoint_id_v2("session-1", time1)
        id2 = generate_checkpoint_id_v2("session-1", time2)
        assert id1 != id2

    def test_format(self):
        """Test generated ID matches expected format."""
        fixed_time = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        checkpoint_id = generate_checkpoint_id_v2("session-1", fixed_time)
        assert checkpoint_id.startswith("ckpt-")
        hex_part = checkpoint_id[5:]
        assert len(hex_part) == 16
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_default_timestamp(self):
        """Test that omitting timestamp uses current time."""
        id1 = generate_checkpoint_id_v2("session-1")
        id2 = generate_checkpoint_id_v2("session-1")
        # With microsecond precision, these should almost always be different
        # but both should be valid format
        assert id1.startswith("ckpt-")
        assert id2.startswith("ckpt-")


class TestSaveAndLoadCheckpointV2:
    """Tests for save_checkpoint_v2 and load_checkpoint_v2 roundtrip."""

    def test_roundtrip_commit_checkpoint(self):
        """Test saving and loading a commit-triggered v2 checkpoint."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "checkpoint.json"
            checkpoint = _make_v2_checkpoint(
                issue_number=42,
                pr_number=10,
                agent_type=AgentType.CODER,
                pipeline_phase="implement",
                branch="egg/test",
            )

            save_checkpoint_v2(checkpoint, path)
            assert path.exists()

            loaded = load_checkpoint_v2(path)
            assert loaded.id == checkpoint.id
            assert loaded.trigger_type == TriggerType.COMMIT
            assert loaded.commit_sha == "abc1234567890"
            assert loaded.session_id == "container-123"
            assert loaded.issue_number == 42
            assert loaded.pr_number == 10
            assert loaded.agent_type == AgentType.CODER
            assert loaded.pipeline_phase == "implement"
            assert loaded.branch == "egg/test"
            assert loaded.schemaVersion == "2.0"

    def test_roundtrip_session_end_checkpoint(self):
        """Test saving and loading a session-end v2 checkpoint."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "checkpoint.json"
            checkpoint = _make_v2_checkpoint(
                trigger_type=TriggerType.SESSION_END,
                session_status=SessionStatus.EXPIRED,
                commit_sha=None,
            )

            save_checkpoint_v2(checkpoint, path)
            loaded = load_checkpoint_v2(path)

            assert loaded.trigger_type == TriggerType.SESSION_END
            assert loaded.session_status == SessionStatus.EXPIRED
            assert loaded.commit_sha is None

    def test_roundtrip_with_transcript_and_usage(self):
        """Test roundtrip with transcript and token usage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "checkpoint.json"
            now = datetime.now(UTC)
            session = SessionMetadata(session_id="test", started_at=now)
            transcript = Transcript(message_count=10, truncated=True, truncation_reason="size limit")
            token_usage = TokenUsage(input_tokens=5000, output_tokens=2000, total_tokens=7000)

            checkpoint = CheckpointV2(
                id="ckpt-abc123def456",
                trigger_type=TriggerType.COMMIT,
                commit_sha="abc1234",
                session_id="container-1",
                session=session,
                transcript=transcript,
                token_usage=token_usage,
                tool_calls=[ToolCall(name="Bash", parameters={}, timestamp=now)],
                created_at=now,
                session_started_at=now,
            )

            save_checkpoint_v2(checkpoint, path)
            loaded = load_checkpoint_v2(path)

            assert loaded.transcript.message_count == 10
            assert loaded.transcript.truncated is True
            assert loaded.token_usage.total_tokens == 7000
            assert len(loaded.tool_calls) == 1

    def test_load_nonexistent_raises(self):
        """Test loading non-existent file raises CheckpointLoadError."""
        with pytest.raises(CheckpointLoadError):
            load_checkpoint_v2(Path("/nonexistent/path.json"))

    def test_load_invalid_json_raises(self):
        """Test loading invalid JSON raises CheckpointLoadError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.json"
            path.write_text("not valid json {{{")
            with pytest.raises(CheckpointLoadError):
                load_checkpoint_v2(path)

    def test_save_creates_parent_dirs(self):
        """Test save creates parent directories if they don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "deep" / "checkpoint.json"
            checkpoint = _make_v2_checkpoint()
            save_checkpoint_v2(checkpoint, path)
            assert path.exists()


class TestLoadCheckpointIndexV2:
    """Tests for load_checkpoint_index_v2 function."""

    def test_nonexistent_returns_empty(self):
        """Test that loading from non-existent file returns empty index."""
        index = load_checkpoint_index_v2(Path("/nonexistent/index.json"))
        assert len(index.checkpoints) == 0
        assert index.by_session == {}
        assert index.by_issue == {}

    def test_invalid_json_raises(self):
        """Test that invalid JSON raises CheckpointLoadError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "index.json"
            path.write_text("not valid json")
            with pytest.raises(CheckpointLoadError):
                load_checkpoint_index_v2(path)

    def test_roundtrip(self):
        """Test save and load of an index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "index.json"
            now = datetime.now(UTC)
            index = CheckpointIndexV2(
                last_updated=now,
                by_session={"session-1": ["ckpt-aa00000001"]},
            )
            save_checkpoint_index_v2(index, path)

            loaded = load_checkpoint_index_v2(path)
            assert loaded.by_session == {"session-1": ["ckpt-aa00000001"]}


class TestAddCheckpointToIndexV2:
    """Tests for add_checkpoint_to_index_v2 function."""

    def test_add_to_empty_index(self):
        """Test adding a checkpoint to a new index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            checkpoint = _make_v2_checkpoint(
                issue_number=42,
                pr_number=10,
                agent_type=AgentType.CODER,
                pipeline_phase="implement",
            )

            index = add_checkpoint_to_index_v2(checkpoint, index_path)

            assert len(index.checkpoints) == 1
            assert index.checkpoints[0].id == checkpoint.id

    def test_secondary_indices_populated(self):
        """Test that all secondary indices are populated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            checkpoint = _make_v2_checkpoint(
                checkpoint_id="ckpt-aa00000001",
                session_id="container-123",
                commit_sha="abc1234567890",
                issue_number=42,
                pr_number=10,
                agent_type=AgentType.CODER,
                pipeline_phase="implement",
                trigger_type=TriggerType.COMMIT,
            )

            index = add_checkpoint_to_index_v2(checkpoint, index_path)

            # by_session
            assert index.get_by_session("container-123") == ["ckpt-aa00000001"]
            # by_issue
            assert index.get_by_issue(42) == ["ckpt-aa00000001"]
            # by_pr
            assert index.get_by_pr(10) == ["ckpt-aa00000001"]
            # by_commit
            assert index.get_by_commit("abc1234567890") == "ckpt-aa00000001"
            # by_agent_type
            assert index.get_by_agent_type(AgentType.CODER) == ["ckpt-aa00000001"]
            # by_phase
            assert index.get_by_phase("implement") == ["ckpt-aa00000001"]
            # by_trigger
            assert index.get_by_trigger(TriggerType.COMMIT) == ["ckpt-aa00000001"]

    def test_session_end_status_index(self):
        """Test that session_status is indexed for session-end checkpoints."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            checkpoint = _make_v2_checkpoint(
                checkpoint_id="ckpt-bb00000002",
                trigger_type=TriggerType.SESSION_END,
                session_status=SessionStatus.FAILED,
                commit_sha=None,
            )

            index = add_checkpoint_to_index_v2(checkpoint, index_path)

            assert index.get_by_status(SessionStatus.FAILED) == ["ckpt-bb00000002"]
            assert index.get_by_trigger(TriggerType.SESSION_END) == ["ckpt-bb00000002"]

    def test_deduplication(self):
        """Test that duplicate checkpoint IDs are not added."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            checkpoint = _make_v2_checkpoint()

            index1 = add_checkpoint_to_index_v2(checkpoint, index_path)
            index2 = add_checkpoint_to_index_v2(checkpoint, index_path)

            assert len(index1.checkpoints) == 1
            assert len(index2.checkpoints) == 1

    def test_multiple_checkpoints(self):
        """Test adding multiple checkpoints to the index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            now = datetime.now(UTC)

            cp1 = _make_v2_checkpoint(
                checkpoint_id="ckpt-aa00000001",
                session_id="session-1",
                commit_sha="abc1234567890",
                issue_number=42,
                now=now,
            )
            cp2 = _make_v2_checkpoint(
                checkpoint_id="ckpt-bb00000002",
                session_id="session-1",
                commit_sha="def4567890abc",
                issue_number=42,
                now=now + timedelta(seconds=1),
            )
            cp3 = _make_v2_checkpoint(
                checkpoint_id="ckpt-cc00000003",
                session_id="session-2",
                commit_sha="0123456789abcd",
                issue_number=99,
                now=now + timedelta(seconds=2),
            )

            add_checkpoint_to_index_v2(cp1, index_path)
            add_checkpoint_to_index_v2(cp2, index_path)
            index = add_checkpoint_to_index_v2(cp3, index_path)

            assert len(index.checkpoints) == 3

            # session-1 has 2 checkpoints
            assert len(index.get_by_session("session-1")) == 2
            # session-2 has 1 checkpoint
            assert len(index.get_by_session("session-2")) == 1

            # issue 42 has 2 checkpoints
            assert len(index.get_by_issue(42)) == 2
            # issue 99 has 1 checkpoint
            assert len(index.get_by_issue(99)) == 1

    def test_optional_fields_not_indexed(self):
        """Test that None optional fields don't create index entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            checkpoint = _make_v2_checkpoint(
                issue_number=None,
                pr_number=None,
                pipeline_phase=None,
            )

            index = add_checkpoint_to_index_v2(checkpoint, index_path)

            assert index.by_issue == {}
            assert index.by_pr == {}
            assert index.by_phase == {}
            # But by_session and by_trigger should still be populated
            assert len(index.by_session) == 1
            assert len(index.by_trigger) == 1

    def test_multi_dimensional_queries(self):
        """Test querying the index from multiple dimensions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.json"
            now = datetime.now(UTC)

            # Coder on issue 42, implement phase
            cp1 = _make_v2_checkpoint(
                checkpoint_id="ckpt-aa00000001",
                session_id="session-1",
                commit_sha="aaa1234567890",
                issue_number=42,
                agent_type=AgentType.CODER,
                pipeline_phase="implement",
                now=now,
            )
            # Tester on issue 42, implement phase
            cp2 = _make_v2_checkpoint(
                checkpoint_id="ckpt-bb00000002",
                session_id="session-2",
                commit_sha="bbb1234567890",
                issue_number=42,
                agent_type=AgentType.TESTER,
                pipeline_phase="implement",
                now=now + timedelta(seconds=1),
            )
            # Coder on issue 99, plan phase
            cp3 = _make_v2_checkpoint(
                checkpoint_id="ckpt-cc00000003",
                session_id="session-3",
                commit_sha="ccc1234567890",
                issue_number=99,
                agent_type=AgentType.CODER,
                pipeline_phase="plan",
                now=now + timedelta(seconds=2),
            )
            # Session-end (failed)
            cp4 = _make_v2_checkpoint(
                checkpoint_id="ckpt-dd00000004",
                session_id="session-4",
                trigger_type=TriggerType.SESSION_END,
                session_status=SessionStatus.FAILED,
                commit_sha=None,
                now=now + timedelta(seconds=3),
            )

            add_checkpoint_to_index_v2(cp1, index_path)
            add_checkpoint_to_index_v2(cp2, index_path)
            add_checkpoint_to_index_v2(cp3, index_path)
            index = add_checkpoint_to_index_v2(cp4, index_path)

            # Issue 42: 2 checkpoints
            assert len(index.get_by_issue(42)) == 2
            # Coder agents: 2 checkpoints
            assert len(index.get_by_agent_type(AgentType.CODER)) == 2
            # Implement phase: 2 checkpoints
            assert len(index.get_by_phase("implement")) == 2
            # Commit triggers: 3 checkpoints
            assert len(index.get_by_trigger(TriggerType.COMMIT)) == 3
            # Session-end triggers: 1 checkpoint
            assert len(index.get_by_trigger(TriggerType.SESSION_END)) == 1
            # Failed status: 1 checkpoint
            assert len(index.get_by_status(SessionStatus.FAILED)) == 1
            # Each session has exactly 1 checkpoint
            for i in range(1, 5):
                assert len(index.get_by_session(f"session-{i}")) == 1
