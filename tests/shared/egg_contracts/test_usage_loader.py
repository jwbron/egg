"""Tests for usage loader functions."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from egg_contracts.checkpoints import (
    CheckpointV2,
    SessionMetadata,
    TokenUsage,
    TriggerType,
)
from egg_contracts.usage import (
    IssueUsage,
    PRUsage,
    SessionUsage,
    TokenCounts,
    UsageIndex,
)
from egg_contracts.usage_loader import (
    UsageLoadError,
    backfill_pr_usage,
    get_issue_usage_path,
    get_pr_usage_path,
    get_session_usage_path,
    get_usage_base_path,
    get_usage_index_path,
    load_issue_usage,
    load_pr_usage,
    load_session_usage,
    load_usage_index,
    query_usage_by_issue,
    query_usage_by_pr,
    query_usage_by_session,
    save_issue_usage,
    save_pr_usage,
    save_session_usage,
    save_usage_index,
    update_usage_from_checkpoint,
)


class TestPathFunctions:
    """Tests for path generation functions."""

    def test_get_usage_base_path(self):
        """Test usage base path generation."""
        base = Path("/tmp/test")
        path = get_usage_base_path(base)
        assert path == Path("/tmp/test/usage")

    def test_get_session_usage_path(self):
        """Test session usage path generation."""
        base = Path("/tmp/test")
        path = get_session_usage_path(base, "session-123")
        assert path == Path("/tmp/test/usage/by-session/session-123.json")

    def test_get_issue_usage_path(self):
        """Test issue usage path generation."""
        base = Path("/tmp/test")
        path = get_issue_usage_path(base, 519)
        assert path == Path("/tmp/test/usage/by-issue/519.json")

    def test_get_pr_usage_path(self):
        """Test PR usage path generation."""
        base = Path("/tmp/test")
        path = get_pr_usage_path(base, 522)
        assert path == Path("/tmp/test/usage/by-pr/522.json")

    def test_get_usage_index_path(self):
        """Test usage index path generation."""
        base = Path("/tmp/test")
        path = get_usage_index_path(base)
        assert path == Path("/tmp/test/usage/index.json")


class TestSessionUsageIO:
    """Tests for session usage I/O."""

    def test_save_and_load_session_usage(self):
        """Test saving and loading session usage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            usage = SessionUsage(
                session_id="test-session",
                container_id="container-123",
                agent_role="coder",
                tokens=TokenCounts(input_tokens=1000, output_tokens=500),
                estimated_cost_usd=0.01,
                checkpoint_count=1,
                last_updated=now,
            )

            save_session_usage(base, usage)
            loaded = load_session_usage(base, "test-session")

            assert loaded is not None
            assert loaded.session_id == "test-session"
            assert loaded.container_id == "container-123"
            assert loaded.tokens.input_tokens == 1000

    def test_load_nonexistent_session_usage(self):
        """Test loading nonexistent session usage returns None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            result = load_session_usage(base, "nonexistent")
            assert result is None

    def test_load_invalid_json_raises_error(self):
        """Test that invalid JSON raises UsageLoadError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            path = get_session_usage_path(base, "invalid")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not valid json {{{")

            with pytest.raises(UsageLoadError):
                load_session_usage(base, "invalid")


class TestIssueUsageIO:
    """Tests for issue usage I/O."""

    def test_save_and_load_issue_usage(self):
        """Test saving and loading issue usage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            usage = IssueUsage(
                issue_number=519,
                pr_number=522,
                session_ids=["s1", "s2"],
                branch="egg/issue-519",
                pipeline_phases=["refine", "plan"],
                tokens=TokenCounts(input_tokens=5000, output_tokens=2500),
                checkpoint_count=5,
                last_updated=now,
            )

            save_issue_usage(base, usage)
            loaded = load_issue_usage(base, 519)

            assert loaded is not None
            assert loaded.issue_number == 519
            assert loaded.pr_number == 522
            assert len(loaded.session_ids) == 2

    def test_load_nonexistent_issue_usage(self):
        """Test loading nonexistent issue usage returns None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            result = load_issue_usage(base, 999)
            assert result is None


class TestPRUsageIO:
    """Tests for PR usage I/O."""

    def test_save_and_load_pr_usage(self):
        """Test saving and loading PR usage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            usage = PRUsage(
                pr_number=522,
                issue_number=519,
                branch="egg/issue-519",
                base_branch="main",
                session_ids=["s1"],
                tokens=TokenCounts(input_tokens=10000, output_tokens=5000),
                checkpoint_count=10,
                last_updated=now,
            )

            save_pr_usage(base, usage)
            loaded = load_pr_usage(base, 522)

            assert loaded is not None
            assert loaded.pr_number == 522
            assert loaded.issue_number == 519
            assert loaded.base_branch == "main"


class TestUsageIndexIO:
    """Tests for usage index I/O."""

    def test_save_and_load_usage_index(self):
        """Test saving and loading usage index."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            index = UsageIndex(
                last_updated=now,
                total_sessions=5,
                total_issues=3,
                total_prs=2,
                total_tokens=TokenCounts(input_tokens=100000, output_tokens=50000),
                total_cost_usd=1.50,
                session_ids=["s1", "s2", "s3", "s4", "s5"],
                issue_numbers=[519, 520, 521],
                pr_numbers=[522, 523],
            )

            save_usage_index(base, index)
            loaded = load_usage_index(base)

            assert loaded.total_sessions == 5
            assert loaded.total_cost_usd == 1.50
            assert 519 in loaded.issue_numbers

    def test_load_nonexistent_usage_index_returns_empty(self):
        """Test loading nonexistent index returns empty index."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            index = load_usage_index(base)
            assert index.total_sessions == 0
            assert index.total_cost_usd == 0.0


class TestUpdateUsageFromCheckpoint:
    """Tests for update_usage_from_checkpoint function."""

    def _create_test_checkpoint(
        self,
        session_id: str = "test-session",
        issue_number: int | None = 519,
        pr_number: int | None = None,
        input_tokens: int = 1000,
        output_tokens: int = 500,
    ) -> CheckpointV2:
        """Create a test checkpoint."""
        now = datetime.now(UTC)
        return CheckpointV2(
            id="ckpt-abc123456789",
            trigger_type=TriggerType.COMMIT,
            commit_sha="abc123456789",
            session_id=session_id,
            session=SessionMetadata(
                session_id=session_id,
                container_id="container-123",
                agent_role="coder",
                started_at=now,
                model="claude-opus-4-5-20251101",
            ),
            token_usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            issue_number=issue_number,
            pr_number=pr_number,
            branch="egg/issue-519",
            pipeline_phase="implement",
            created_at=now,
            session_started_at=now,
        )

    def test_update_creates_new_session_usage(self):
        """Test that update creates new session usage if not exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            checkpoint = self._create_test_checkpoint()

            update_usage_from_checkpoint(base, checkpoint)

            usage = load_session_usage(base, "test-session")
            assert usage is not None
            assert usage.tokens.input_tokens == 1000
            assert usage.tokens.output_tokens == 500
            assert usage.checkpoint_count == 1

    def test_update_increments_existing_session_usage(self):
        """Test that update increments existing session usage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)

            # First checkpoint
            checkpoint1 = self._create_test_checkpoint(input_tokens=1000, output_tokens=500)
            checkpoint1.id = "ckpt-checkpoint1111"
            update_usage_from_checkpoint(base, checkpoint1)

            # Second checkpoint with same session
            checkpoint2 = self._create_test_checkpoint(input_tokens=2000, output_tokens=1000)
            checkpoint2.id = "ckpt-checkpoint2222"
            update_usage_from_checkpoint(base, checkpoint2)

            usage = load_session_usage(base, "test-session")
            assert usage is not None
            assert usage.tokens.input_tokens == 3000
            assert usage.tokens.output_tokens == 1500
            assert usage.checkpoint_count == 2

    def test_update_creates_issue_usage(self):
        """Test that update creates issue usage if issue_number is set."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            checkpoint = self._create_test_checkpoint(issue_number=519)

            update_usage_from_checkpoint(base, checkpoint)

            usage = load_issue_usage(base, 519)
            assert usage is not None
            assert usage.tokens.input_tokens == 1000
            assert "test-session" in usage.session_ids

    def test_update_creates_pr_usage(self):
        """Test that update creates PR usage if pr_number is set."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            checkpoint = self._create_test_checkpoint(issue_number=519, pr_number=522)

            update_usage_from_checkpoint(base, checkpoint)

            usage = load_pr_usage(base, 522)
            assert usage is not None
            assert usage.tokens.input_tokens == 1000
            assert usage.issue_number == 519

    def test_update_updates_index(self):
        """Test that update updates the usage index."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            checkpoint = self._create_test_checkpoint(issue_number=519, pr_number=522)

            update_usage_from_checkpoint(base, checkpoint)

            index = load_usage_index(base)
            assert "test-session" in index.session_ids
            assert 519 in index.issue_numbers
            assert 522 in index.pr_numbers
            assert index.total_sessions == 1
            assert index.total_issues == 1
            assert index.total_prs == 1

    def test_duplicate_checkpoint_not_counted_twice(self):
        """Test that the same checkpoint is not counted twice."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            checkpoint = self._create_test_checkpoint()

            update_usage_from_checkpoint(base, checkpoint)
            update_usage_from_checkpoint(base, checkpoint)  # Same checkpoint again

            usage = load_session_usage(base, "test-session")
            assert usage is not None
            # Should only be counted once
            assert usage.checkpoint_count == 1


class TestBackfillPRUsage:
    """Tests for backfill_pr_usage function."""

    def test_backfill_creates_pr_usage(self):
        """Test that backfill creates PR usage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            # Create session usage without PR
            session_usage = SessionUsage(
                session_id="test-session",
                issue_number=519,
                tokens=TokenCounts(input_tokens=1000, output_tokens=500),
                checkpoint_count=1,
                last_updated=now,
            )
            save_session_usage(base, session_usage)

            # Create issue usage
            issue_usage = IssueUsage(
                issue_number=519,
                session_ids=["test-session"],
                tokens=TokenCounts(input_tokens=1000, output_tokens=500),
                checkpoint_count=1,
                last_updated=now,
            )
            save_issue_usage(base, issue_usage)

            # Backfill PR
            updated = backfill_pr_usage(base, 522, issue_number=519)

            assert updated == 1

            # Check PR usage was created
            pr_usage = load_pr_usage(base, 522)
            assert pr_usage is not None
            assert pr_usage.issue_number == 519

            # Check session was updated
            session = load_session_usage(base, "test-session")
            assert session is not None
            assert session.pr_number == 522

            # Check issue was updated
            issue = load_issue_usage(base, 519)
            assert issue is not None
            assert issue.pr_number == 522

    def test_backfill_updates_index(self):
        """Test that backfill updates the usage index."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)

            backfill_pr_usage(base, 522)

            index = load_usage_index(base)
            assert 522 in index.pr_numbers


class TestQueryFunctions:
    """Tests for query functions."""

    def test_query_usage_by_issue(self):
        """Test querying usage by issue."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            usage = IssueUsage(
                issue_number=519,
                tokens=TokenCounts(input_tokens=1000),
                last_updated=now,
            )
            save_issue_usage(base, usage)

            result = query_usage_by_issue(base, 519)
            assert result is not None
            assert result.issue_number == 519

    def test_query_usage_by_session(self):
        """Test querying usage by session."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            usage = SessionUsage(
                session_id="test-session",
                tokens=TokenCounts(input_tokens=1000),
                last_updated=now,
            )
            save_session_usage(base, usage)

            result = query_usage_by_session(base, "test-session")
            assert result is not None
            assert result.session_id == "test-session"

    def test_query_usage_by_pr(self):
        """Test querying usage by PR."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = datetime.now(UTC)

            usage = PRUsage(
                pr_number=522,
                tokens=TokenCounts(input_tokens=1000),
                last_updated=now,
            )
            save_pr_usage(base, usage)

            result = query_usage_by_pr(base, 522)
            assert result is not None
            assert result.pr_number == 522
