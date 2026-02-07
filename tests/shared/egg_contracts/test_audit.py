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

    def test_all_fields_populated(self):
        """Test creating an entry with all fields set."""
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

    def test_timestamp_is_utc(self):
        """Test that the timestamp uses UTC timezone."""
        before = datetime.now(UTC)
        entry = create_audit_entry(
            actor="egg",
            role=AuditRole.SYSTEM,
            action=AuditAction.CREATE,
            field_path="issue",
        )
        after = datetime.now(UTC)

        assert before <= entry.timestamp <= after

    def test_optional_fields_default_to_none(self):
        """Test that old_value, new_value, and reason default to None."""
        entry = create_audit_entry(
            actor="system",
            role=AuditRole.SYSTEM,
            action=AuditAction.CREATE,
            field_path="contract",
        )

        assert entry.old_value is None
        assert entry.new_value is None
        assert entry.reason is None

    def test_returns_audit_entry_model(self):
        """Test that the return type is AuditEntry."""
        entry = create_audit_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.notes",
        )
        assert isinstance(entry, AuditEntry)

    def test_all_roles(self):
        """Test creating entries with each role."""
        for role in AuditRole:
            entry = create_audit_entry(
                actor="test-actor",
                role=role,
                action=AuditAction.UPDATE,
                field_path="test.path",
            )
            assert entry.role == role

    def test_all_actions(self):
        """Test creating entries with each action type."""
        for action in AuditAction:
            entry = create_audit_entry(
                actor="test-actor",
                role=AuditRole.SYSTEM,
                action=action,
                field_path="test.path",
            )
            assert entry.action == action


class TestCreateUpdateEntry:
    """Tests for create_update_entry function."""

    def test_creates_update_action(self):
        """Test that the entry has UPDATE action."""
        entry = create_update_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            field_path="phases.0.tasks.0.notes",
            old_value="old notes",
            new_value="new notes",
        )

        assert entry.action == AuditAction.UPDATE

    def test_captures_old_and_new_values(self):
        """Test that old and new values are recorded."""
        entry = create_update_entry(
            actor="reviewer-bot",
            role=AuditRole.REVIEWER,
            field_path="phases.0.tasks.0.status",
            old_value="pending",
            new_value="complete",
        )

        assert entry.old_value == "pending"
        assert entry.new_value == "complete"

    def test_with_reason(self):
        """Test update entry with a reason."""
        entry = create_update_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="def5678",
            reason="Fixed failing test",
        )

        assert entry.reason == "Fixed failing test"

    def test_without_reason(self):
        """Test update entry without a reason defaults to None."""
        entry = create_update_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            field_path="phases.0.tasks.0.notes",
            old_value="",
            new_value="Added notes",
        )

        assert entry.reason is None

    def test_field_path_preserved(self):
        """Test that the field_path is set correctly."""
        entry = create_update_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            field_path="acceptance_criteria.0.verified",
            old_value=False,
            new_value=True,
        )

        assert entry.field_path == "acceptance_criteria.0.verified"


class TestCreateTransitionEntry:
    """Tests for create_transition_entry function."""

    def test_creates_transition_action(self):
        """Test that the entry has TRANSITION action."""
        entry = create_transition_entry(
            actor="system",
            role=AuditRole.SYSTEM,
            from_phase="refine",
            to_phase="implement",
        )

        assert entry.action == AuditAction.TRANSITION

    def test_field_path_is_current_phase(self):
        """Test that field_path is always 'current_phase'."""
        entry = create_transition_entry(
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            from_phase="implement",
            to_phase="pr",
        )

        assert entry.field_path == "current_phase"

    def test_phases_stored_as_old_and_new_values(self):
        """Test that from_phase and to_phase map to old_value and new_value."""
        entry = create_transition_entry(
            actor="system",
            role=AuditRole.SYSTEM,
            from_phase="refine",
            to_phase="plan",
        )

        assert entry.old_value == "refine"
        assert entry.new_value == "plan"

    def test_with_reason(self):
        """Test transition entry with a reason."""
        entry = create_transition_entry(
            actor="reviewer-bot",
            role=AuditRole.REVIEWER,
            from_phase="implement",
            to_phase="pr",
            reason="All tasks complete",
        )

        assert entry.reason == "All tasks complete"

    def test_without_reason(self):
        """Test transition entry without a reason."""
        entry = create_transition_entry(
            actor="system",
            role=AuditRole.SYSTEM,
            from_phase="refine",
            to_phase="implement",
        )

        assert entry.reason is None


class TestFormatAuditLog:
    """Tests for format_audit_log function."""

    def test_empty_entries(self):
        """Test formatting an empty list of entries."""
        result = format_audit_log([])
        assert result == "Audit Log:"

    def test_single_update_entry_with_old_and_new(self):
        """Test formatting an update entry with both old and new values."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.notes",
            old_value="old",
            new_value="new",
        )

        result = format_audit_log([entry])
        lines = result.split("\n")

        assert lines[0] == "Audit Log:"
        assert "[2025-01-15 10:30:00]" in lines[1]
        assert "implementer:egg" in lines[1]
        assert "update" in lines[1]
        assert "phases.0.tasks.0.notes" in lines[1]
        assert "(old -> new)" in lines[1]

    def test_entry_with_new_value_only(self):
        """Test formatting an entry where only new_value is set (old_value is None)."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="system",
            role=AuditRole.SYSTEM,
            action=AuditAction.CREATE,
            field_path="issue",
            old_value=None,
            new_value="created",
        )

        result = format_audit_log([entry])
        lines = result.split("\n")

        assert "= created" in lines[1]
        # Should NOT have the arrow format
        assert "->" not in lines[1]

    def test_entry_with_reason(self):
        """Test formatting an entry that includes a reason."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
            reason="Implementation done",
        )

        result = format_audit_log([entry])
        assert "- Implementation done" in result

    def test_entry_without_values(self):
        """Test formatting an entry with no old or new value."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.DELETE,
            field_path="phases.0.tasks.0",
            old_value=None,
            new_value=None,
        )

        result = format_audit_log([entry])
        lines = result.split("\n")

        assert "delete" in lines[1]
        # Should not have value indicators
        assert "->" not in lines[1]
        assert "= " not in lines[1]

    def test_multiple_entries(self):
        """Test formatting multiple entries."""
        entries = [
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
                actor="system",
                role=AuditRole.SYSTEM,
                action=AuditAction.CREATE,
                field_path="contract",
                new_value="initialized",
            ),
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
                actor="egg",
                role=AuditRole.IMPLEMENTER,
                action=AuditAction.UPDATE,
                field_path="phases.0.tasks.0.commit",
                old_value=None,
                new_value="abc1234",
            ),
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
                actor="reviewer",
                role=AuditRole.REVIEWER,
                action=AuditAction.TRANSITION,
                field_path="current_phase",
                old_value="implement",
                new_value="pr",
            ),
        ]

        result = format_audit_log(entries)
        lines = result.split("\n")

        assert len(lines) == 4  # header + 3 entries
        assert lines[0] == "Audit Log:"
        assert "system:system" in lines[1]
        assert "implementer:egg" in lines[2]
        assert "reviewer:reviewer" in lines[3]

    def test_limit_shows_last_n_entries(self):
        """Test that limit parameter shows only the last N entries."""
        entries = [
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
                actor="first",
                role=AuditRole.SYSTEM,
                action=AuditAction.CREATE,
                field_path="contract",
            ),
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
                actor="second",
                role=AuditRole.IMPLEMENTER,
                action=AuditAction.UPDATE,
                field_path="phases.0.tasks.0.notes",
            ),
            AuditEntry(
                timestamp=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
                actor="third",
                role=AuditRole.REVIEWER,
                action=AuditAction.UPDATE,
                field_path="phases.0.status",
            ),
        ]

        result = format_audit_log(entries, limit=2)
        lines = result.split("\n")

        # Header + 2 entries (the last two)
        assert len(lines) == 3
        assert "second" in lines[1]
        assert "third" in lines[2]
        assert "first" not in result

    def test_limit_none_shows_all(self):
        """Test that limit=None shows all entries."""
        entries = [
            AuditEntry(
                timestamp=datetime(2025, 1, 15, i, 0, 0, tzinfo=UTC),
                actor=f"actor-{i}",
                role=AuditRole.SYSTEM,
                action=AuditAction.UPDATE,
                field_path="test",
            )
            for i in range(5)
        ]

        result = format_audit_log(entries, limit=None)
        lines = result.split("\n")
        assert len(lines) == 6  # header + 5 entries

    def test_transition_entry_format(self):
        """Test formatting a transition entry with phase change."""
        entry = AuditEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
            actor="system",
            role=AuditRole.SYSTEM,
            action=AuditAction.TRANSITION,
            field_path="current_phase",
            old_value="refine",
            new_value="implement",
            reason="Refinement complete",
        )

        result = format_audit_log([entry])
        lines = result.split("\n")

        assert "transition" in lines[1]
        assert "current_phase" in lines[1]
        assert "(refine -> implement)" in lines[1]
        assert "- Refinement complete" in lines[1]
