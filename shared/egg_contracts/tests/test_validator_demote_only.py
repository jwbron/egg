"""Reviewer task-status writes are demote-only (#3114).

The contract-completeness gate blocks an enforcer's ACK while task rows
are incomplete — so a reviewer must not be able to satisfy the gate by
flipping rows to ``complete`` itself. Reviewers may demote (flag) a row;
only the implementer claims completion.
"""

from __future__ import annotations

import pytest

from egg_contracts.roles import Role
from egg_contracts.validator import validate_mutation


class TestReviewerDemoteOnly:
    def test_reviewer_cannot_set_complete(self) -> None:
        result = validate_mutation(Role.REVIEWER, "phases.1.tasks.3.status", "complete")
        assert not result.valid
        assert "demote-only" in result.message
        assert result.required_role == Role.IMPLEMENTER.value

    @pytest.mark.parametrize("value", ["pending", "incomplete", "blocked"])
    def test_reviewer_may_demote(self, value: str) -> None:
        result = validate_mutation(Role.REVIEWER, "phases.1.tasks.3.status", value)
        assert result.valid

    def test_implementer_may_set_complete(self) -> None:
        result = validate_mutation(Role.IMPLEMENTER, "phases.1.tasks.3.status", "complete")
        assert result.valid

    def test_reviewer_slice_status_unaffected(self) -> None:
        """Slice/phase status transitions (gateway phase API) keep working."""
        result = validate_mutation(Role.REVIEWER, "phases.1.status", "complete")
        assert result.valid
