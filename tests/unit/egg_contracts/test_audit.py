"""Tests for audit logging."""

import sys
from datetime import datetime
from pathlib import Path

# Add shared to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts.audit import (
    create_audit_entry,
    get_actor_history,
    get_blocked_operations,
    get_field_history,
    log_blocked_operation,
    log_mutation,
)
from egg_contracts.models import AuditAction, Contract, Issue
from egg_contracts.roles import Role


class TestCreateAuditEntry:
    """Tests for audit entry creation."""

    def test_create_update_entry(self):
        entry = create_audit_entry(
            actor="implementer",
            role=Role.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
        )
        assert entry.action == AuditAction.UPDATE
        assert entry.actor == "implementer"
        assert entry.role == "implementer"
        assert entry.new_value == "abc1234"
        assert isinstance(entry.timestamp, datetime)

    def test_create_blocked_entry(self):
        entry = create_audit_entry(
            actor="implementer",
            role="implementer",
            action="blocked",
            field_path="phases.0.tasks.0.status",
            new_value="complete",
            reason="Role not authorized",
        )
        assert entry.action == AuditAction.BLOCKED
        assert entry.reason == "Role not authorized"

    def test_create_entry_with_role_enum(self):
        entry = create_audit_entry(
            actor="test",
            role=Role.REVIEWER,
            action=AuditAction.UPDATE,
            field_path="test",
        )
        assert entry.role == "reviewer"


class TestLogMutation:
    """Tests for logging mutations to contract."""

    def test_log_mutation_creates_entry(self, sample_contract):
        initial_count = len(sample_contract.audit_log or [])

        log_mutation(
            sample_contract,
            actor="implementer",
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )

        assert sample_contract.audit_log is not None
        assert len(sample_contract.audit_log) == initial_count + 1
        assert sample_contract.audit_log[-1].action == AuditAction.CREATE

    def test_log_mutation_with_old_value(self, sample_contract):
        log_mutation(
            sample_contract,
            actor="implementer",
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.notes",
            new_value="Updated notes",
            old_value="Old notes",
        )

        entry = sample_contract.audit_log[-1]
        assert entry.action == AuditAction.UPDATE
        assert entry.old_value == "Old notes"
        assert entry.new_value == "Updated notes"

    def test_log_mutation_initializes_audit_log(self):
        contract = Contract(
            issue=Issue(number=1, title="Test", url="https://example.com"),
            audit_log=None,
        )

        log_mutation(
            contract,
            actor="test",
            role="human",
            field_path="test",
            new_value="value",
        )

        assert contract.audit_log is not None
        assert len(contract.audit_log) == 1


class TestLogBlockedOperation:
    """Tests for logging blocked operations."""

    def test_log_blocked_creates_entry(self, sample_contract):
        log_blocked_operation(
            sample_contract,
            actor="implementer",
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.status",
            attempted_value="complete",
            reason="Role 'implementer' cannot modify status",
        )

        assert sample_contract.audit_log is not None
        entry = sample_contract.audit_log[-1]
        assert entry.action == AuditAction.BLOCKED
        assert entry.reason == "Role 'implementer' cannot modify status"


class TestGetFieldHistory:
    """Tests for retrieving field modification history."""

    def test_get_field_history(self, sample_contract):
        # Log some mutations to the same field
        for i in range(3):
            log_mutation(
                sample_contract,
                actor="implementer",
                role=Role.IMPLEMENTER,
                field_path="phases.0.tasks.0.commit",
                new_value=f"commit-{i}",
            )

        history = get_field_history(sample_contract, "phases.0.tasks.0.commit")
        assert len(history) == 3
        assert history[0].new_value == "commit-0"
        assert history[2].new_value == "commit-2"

    def test_get_field_history_empty(self, sample_contract):
        history = get_field_history(sample_contract, "nonexistent.field")
        assert len(history) == 0

    def test_get_field_history_no_audit_log(self):
        contract = Contract(
            issue=Issue(number=1, title="Test", url="https://example.com"),
            audit_log=None,
        )
        history = get_field_history(contract, "any.field")
        assert len(history) == 0


class TestGetActorHistory:
    """Tests for retrieving actor action history."""

    def test_get_actor_history(self, sample_contract):
        # Log mutations from different actors
        log_mutation(sample_contract, actor="alice", role="human", field_path="a", new_value="1")
        log_mutation(sample_contract, actor="bob", role="human", field_path="b", new_value="2")
        log_mutation(sample_contract, actor="alice", role="human", field_path="c", new_value="3")

        history = get_actor_history(sample_contract, "alice")
        assert len(history) == 2

        history = get_actor_history(sample_contract, "bob")
        assert len(history) == 1

    def test_get_actor_history_empty(self, sample_contract):
        history = get_actor_history(sample_contract, "nonexistent")
        assert len(history) == 0


class TestGetBlockedOperations:
    """Tests for retrieving blocked operation history."""

    def test_get_blocked_operations(self, sample_contract):
        # Log some blocked operations
        log_blocked_operation(
            sample_contract,
            actor="implementer",
            role="implementer",
            field_path="status",
            attempted_value="complete",
            reason="Not authorized",
        )
        log_mutation(
            sample_contract,
            actor="implementer",
            role="implementer",
            field_path="commit",
            new_value="abc",
        )
        log_blocked_operation(
            sample_contract,
            actor="implementer",
            role="implementer",
            field_path="phase",
            attempted_value="implement",
            reason="Not authorized",
        )

        blocked = get_blocked_operations(sample_contract)
        assert len(blocked) == 2
        assert all(e.action == AuditAction.BLOCKED for e in blocked)

    def test_get_blocked_operations_empty(self, sample_contract):
        blocked = get_blocked_operations(sample_contract)
        assert len(blocked) == 0
