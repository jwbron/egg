"""Integration tests for SDLC pipeline role enforcement.

Tests that the gateway blocks unauthorized mutations where:
1. Implementer cannot mark tasks complete (reviewer only)
2. Implementer cannot transition phases (requires higher role)
3. Reviewer cannot modify implementation details
4. Human can override any field
5. Role escalation attempts are blocked
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from egg_contracts import (
    Contract,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    Role,
    Task,
    TaskStatus,
    apply_mutation,
    can_modify,
    get_field_owner,
    get_role_permissions,
    load_contract,
    save_contract,
    validate_mutation,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        contracts_dir = repo_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_path


@pytest.fixture
def sample_issue_info():
    """Create sample issue info for testing."""
    return IssueInfo(
        number=500,
        title="Role enforcement test",
        url="https://github.com/test-owner/test-repo/issues/500",
    )


@pytest.fixture
def contract_with_tasks(sample_issue_info):
    """Create a contract with tasks for role testing."""
    contract = Contract(
        schemaVersion="1.0",
        issue=sample_issue_info,
        current_phase=PipelinePhase.IMPLEMENT,
        phases=[
            Phase(
                id="phase-1",
                name="Implementation",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(
                        id="task-1-1",
                        description="Test task",
                        status=TaskStatus.IN_PROGRESS,
                    ),
                ],
            ),
        ],
    )
    return contract


class TestFieldOwnership:
    """Tests for field ownership mappings."""

    def test_commit_owned_by_implementer(self):
        """Commit field is owned by implementer."""
        owner = get_field_owner("phases.0.tasks.0.commit")
        assert owner == Role.IMPLEMENTER

    def test_notes_owned_by_implementer(self):
        """Notes field is owned by implementer."""
        owner = get_field_owner("phases.0.tasks.0.notes")
        assert owner == Role.IMPLEMENTER

    def test_task_status_owned_by_reviewer(self):
        """Task status is owned by reviewer."""
        owner = get_field_owner("phases.0.tasks.0.status")
        assert owner == Role.REVIEWER

    def test_phase_status_owned_by_reviewer(self):
        """Phase status is owned by reviewer."""
        owner = get_field_owner("phases.0.status")
        assert owner == Role.REVIEWER

    def test_current_phase_owned_by_reviewer(self):
        """Current phase is owned by reviewer."""
        owner = get_field_owner("current_phase")
        assert owner == Role.REVIEWER

    def test_decision_resolution_owned_by_human(self):
        """Decision resolution is owned by human."""
        owner = get_field_owner("decisions.0.resolved")
        assert owner == Role.HUMAN


class TestCanModify:
    """Tests for role modification permissions."""

    def test_implementer_can_modify_commit(self):
        """Implementer can modify commit field."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.commit") is True

    def test_implementer_cannot_modify_status(self):
        """Implementer cannot modify task status."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.status") is False

    def test_reviewer_can_modify_status(self):
        """Reviewer can modify task status."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.status") is True

    def test_reviewer_cannot_modify_commit(self):
        """Reviewer cannot modify commit field."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.commit") is False

    def test_human_can_modify_anything(self):
        """Human can modify any field."""
        assert can_modify(Role.HUMAN, "phases.0.tasks.0.commit") is True
        assert can_modify(Role.HUMAN, "phases.0.tasks.0.status") is True
        assert can_modify(Role.HUMAN, "current_phase") is True
        assert can_modify(Role.HUMAN, "decisions.0.resolved") is True


class TestRolePermissionsSummary:
    """Tests for role permission summaries."""

    def test_implementer_permissions(self):
        """Implementer has limited permissions."""
        perms = get_role_permissions(Role.IMPLEMENTER)

        assert "phases.*.tasks.*.commit" in perms["can_modify"]
        assert "phases.*.tasks.*.notes" in perms["can_modify"]
        assert "phases.*.tasks.*.status" in perms["cannot_modify"]

    def test_reviewer_permissions(self):
        """Reviewer has review-specific permissions."""
        perms = get_role_permissions(Role.REVIEWER)

        assert "phases.*.tasks.*.status" in perms["can_modify"]
        assert "phases.*.status" in perms["can_modify"]
        assert "phases.*.tasks.*.commit" in perms["cannot_modify"]

    def test_human_permissions(self):
        """Human has all permissions."""
        perms = get_role_permissions(Role.HUMAN)

        assert perms["can_modify"] == ["*"]
        assert len(perms["cannot_modify"]) == 0


class TestValidateMutation:
    """Tests for mutation validation."""

    def test_valid_implementer_mutation(self):
        """Valid implementer mutation passes validation."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is True

    def test_invalid_implementer_mutation(self):
        """Invalid implementer mutation fails validation."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is False
        assert result.required_role is not None

    def test_valid_reviewer_mutation(self):
        """Valid reviewer mutation passes validation."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is True


class TestApplyMutationRoleEnforcement:
    """Tests for role enforcement when applying mutations."""

    def test_implementer_can_set_commit(self, temp_repo, contract_with_tasks):
        """Implementer can successfully set commit."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )

        assert result.success is True
        assert result.contract.phases[0].tasks[0].commit == "abc1234"

    def test_implementer_cannot_set_status(self, temp_repo, contract_with_tasks):
        """Implementer cannot set task status."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)

        # First validate the mutation to get structured error info
        validation = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        assert validation.valid is False
        assert validation.required_role == Role.REVIEWER.value

        # Also verify apply_mutation rejects it
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        assert result.success is False

    def test_reviewer_can_set_status(self, temp_repo, contract_with_tasks):
        """Reviewer can set task status."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )

        assert result.success is True
        assert result.contract.phases[0].tasks[0].status == TaskStatus.COMPLETE

    def test_reviewer_cannot_set_commit(self, temp_repo, contract_with_tasks):
        """Reviewer cannot set commit."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )

        assert result.success is False

    def test_human_can_set_any_field(self, temp_repo, contract_with_tasks):
        """Human can set any field."""
        save_contract(contract_with_tasks, temp_repo)

        # Human sets commit
        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="admin",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.success is True
        save_contract(result.contract, temp_repo)

        # Human sets status
        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="admin",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        assert result.success is True


class TestPhaseTransitionRoleEnforcement:
    """Tests for role enforcement on phase transitions."""

    def test_implementer_cannot_transition_phase(self, temp_repo, contract_with_tasks):
        """Implementer cannot transition pipeline phase."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="current_phase",
            new_value=PipelinePhase.PR.value,
        )

        assert result.success is False

    def test_reviewer_can_transition_to_pr(self, temp_repo, contract_with_tasks):
        """Reviewer can transition to PR phase."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.PR.value,
        )

        assert result.success is True
        assert result.contract.current_phase == PipelinePhase.PR

    def test_human_can_transition_any_phase(self, temp_repo, contract_with_tasks):
        """Human can transition to any phase."""
        contract_with_tasks.current_phase = PipelinePhase.REFINE
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="admin",
            field_path="current_phase",
            new_value=PipelinePhase.IMPLEMENT.value,
        )

        assert result.success is True


class TestDecisionRoleEnforcement:
    """Tests for role enforcement on HITL decisions."""

    def test_implementer_cannot_resolve_decision(self, temp_repo, contract_with_tasks):
        """Implementer cannot resolve HITL decisions."""
        from egg_contracts import Decision, DecisionType

        contract_with_tasks.decisions.append(
            Decision(
                id="decision-1",
                question="How to proceed?",
                type=DecisionType.HITL,
                resolved=False,
            )
        )
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="decisions.0.resolved",
            new_value=True,
        )

        assert result.success is False

    def test_human_can_resolve_decision(self, temp_repo, contract_with_tasks):
        """Human can resolve HITL decisions."""
        from egg_contracts import Decision, DecisionType

        contract_with_tasks.decisions.append(
            Decision(
                id="decision-1",
                question="How to proceed?",
                type=DecisionType.HITL,
                resolved=False,
            )
        )
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.HUMAN,
            actor="admin",
            field_path="decisions.0.resolved",
            new_value=True,
        )

        assert result.success is True


class TestAuditLogRoleTracking:
    """Tests that audit log tracks roles correctly."""

    def test_audit_log_records_role(self, temp_repo, contract_with_tasks):
        """Audit log records the role that made the mutation."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        save_contract(result.contract, temp_repo)

        loaded = load_contract(500, temp_repo)
        assert len(loaded.audit_log) > 0
        last_entry = loaded.audit_log[-1]
        assert last_entry.role.value == "implementer"

    def test_audit_log_records_different_roles(self, temp_repo, contract_with_tasks):
        """Audit log correctly records different roles."""
        save_contract(contract_with_tasks, temp_repo)

        # Implementer action
        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        save_contract(result.contract, temp_repo)

        # Reviewer action
        contract = load_contract(500, temp_repo)
        result = apply_mutation(
            contract=contract,
            role=Role.REVIEWER,
            actor="reviewer",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE.value,
        )
        save_contract(result.contract, temp_repo)

        loaded = load_contract(500, temp_repo)
        roles_in_log = [e.role.value for e in loaded.audit_log]
        assert "implementer" in roles_in_log
        assert "reviewer" in roles_in_log
