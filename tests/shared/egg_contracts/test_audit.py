"""Tests for egg_contracts.audit module."""

from datetime import UTC, datetime

from egg_contracts.audit import (
    create_audit_entry,
    create_transition_entry,
    create_update_entry,
    format_audit_log,
)
from egg_contracts.models import AuditAction, AuditEntry, AuditRole


class TestCreateAuditEntry:
    """Tests for create_audit_entry function."""

    def test_creates_entry_with_all_fields(self):
        """Test creating an audit entry with all fields."""
        entry = create_audit_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
            reason="Implementation complete",
        )
        assert entry.actor == "egg"
        assert entry.role == AuditRole.IMPLEMENTER
        assert entry.action == AuditAction.UPDATE
        assert entry.field_path == "phases.0.tasks.0.commit"
        assert entry.old_value is None
        assert entry.new_value == "abc1234"
        assert entry.reason == "Implementation complete"
        assert isinstance(entry.timestamp, datetime)

    def test_creates_entry_with_minimal_fields(self):
        """Test creating an audit entry with minimal fields."""
        entry = create_audit_entry(
            actor="system",
            role=AuditRole.SYSTEM,
            action=AuditAction.CREATE,
            field_path="contract",
        )
        assert entry.actor == "system"
        assert entry.old_value is None
        assert entry.new_value is None
        assert entry.reason is None

    def test_timestamp_is_utc(self):
        """Test that timestamp is in UTC."""
        entry = create_audit_entry(
            actor="test",
            role=AuditRole.HUMAN,
            action=AuditAction.UPDATE,
            field_path="test.field",
        )
        assert entry.timestamp.tzinfo == UTC


class TestCreateUpdateEntry:
    """Tests for create_update_entry function."""

    def test_creates_update_entry(self):
        """Test creating an update entry."""
        entry = create_update_entry(
            actor="reviewer-bot",
            role=AuditRole.REVIEWER,
            field_path="phases.0.tasks.0.status",
            old_value="pending",
            new_value="complete",
            reason="Task verified",
        )
        assert entry.action == AuditAction.UPDATE
        assert entry.actor == "reviewer-bot"
        assert entry.role == AuditRole.REVIEWER
        assert entry.old_value == "pending"
        assert entry.new_value == "complete"
        assert entry.reason == "Task verified"

    def test_creates_update_entry_without_reason(self):
        """Test creating an update entry without reason."""
        entry = create_update_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            field_path="phases.0.tasks.0.notes",
            old_value="",
            new_value="Added implementation notes",
        )
        assert entry.reason is None


class TestCreateTransitionEntry:
    """Tests for create_transition_entry function."""

    def test_creates_transition_entry(self):
        """Test creating a phase transition entry."""
        entry = create_transition_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            from_phase="refine",
            to_phase="implement",
            reason="Plan approved",
        )
        assert entry.action == AuditAction.TRANSITION
        assert entry.field_path == "current_phase"
        assert entry.old_value == "refine"
        assert entry.new_value == "implement"
        assert entry.reason == "Plan approved"

    def test_creates_transition_entry_without_reason(self):
        """Test creating a transition entry without reason."""
        entry = create_transition_entry(
            actor="system",
            role=AuditRole.SYSTEM,
            from_phase="implement",
            to_phase="review",
        )
        assert entry.reason is None


class TestFormatAuditLog:
    """Tests for format_audit_log function."""

    def test_format_empty_log(self):
        """Test formatting an empty audit log."""
        result = format_audit_log([])
        assert result == "Audit Log:"

    def test_format_single_entry(self):
        """Test formatting a single entry."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
        )
        result = format_audit_log([entry])
        assert "Audit Log:" in result
        assert "2025-01-15 10:30:00" in result
        assert "implementer:egg" in result
        assert "update" in result
        assert "phases.0.tasks.0.commit" in result
        assert "= abc1234" in result

    def test_format_entry_with_old_and_new_value(self):
        """Test formatting entry with both old and new values."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="reviewer",
            role=AuditRole.REVIEWER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.status",
            old_value="pending",
            new_value="complete",
        )
        result = format_audit_log([entry])
        assert "(pending -> complete)" in result

    def test_format_entry_with_reason(self):
        """Test formatting entry with reason."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="human",
            role=AuditRole.HUMAN,
            action=AuditAction.UPDATE,
            field_path="decisions.0.resolved",
            old_value=False,
            new_value=True,
            reason="Approved after review",
        )
        result = format_audit_log([entry])
        assert "- Approved after review" in result

    def test_format_multiple_entries(self):
        """Test formatting multiple entries."""
        entries = [
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
                actor="egg",
                role=AuditRole.IMPLEMENTER,
                action=AuditAction.UPDATE,
                field_path="phases.0.tasks.0.commit",
                new_value="abc1234",
            ),
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
                actor="reviewer",
                role=AuditRole.REVIEWER,
                action=AuditAction.UPDATE,
                field_path="phases.0.tasks.0.status",
                old_value="pending",
                new_value="complete",
            ),
        ]
        result = format_audit_log(entries)
        lines = result.split("\n")
        assert len(lines) == 3  # Header + 2 entries

    def test_format_with_limit(self):
        """Test formatting with a limit on entries."""
        entries = [
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 10, i, 0, tzinfo=UTC),
                actor="egg",
                role=AuditRole.IMPLEMENTER,
                action=AuditAction.UPDATE,
                field_path=f"phases.0.tasks.{i}.commit",
                new_value=f"commit{i}",
            )
            for i in range(5)
        ]
        result = format_audit_log(entries, limit=2)
        lines = result.split("\n")
        # Should only show last 2 entries + header
        assert len(lines) == 3
        # Should show the last entries (3 and 4)
        assert "tasks.3" in result
        assert "tasks.4" in result
        assert "tasks.0" not in result

    def test_format_transition_entry(self):
        """Test formatting a transition entry."""
        entry = create_transition_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            from_phase="refine",
            to_phase="implement",
        )
        result = format_audit_log([entry])
        assert "transition" in result
        assert "current_phase" in result
        assert "(refine -> implement)" in result
