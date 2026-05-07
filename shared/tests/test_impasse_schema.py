"""Tests for the typed Impasse primitive (#2529)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from egg_contracts import Impasse, ImpasseCategory
from pydantic import ValidationError


class TestImpasseConstruction:
    def test_minimal_payload_succeeds(self):
        imp = Impasse(category=ImpasseCategory.UNKNOWN, reason="not sure why")
        assert imp.category == ImpasseCategory.UNKNOWN
        assert imp.reason == "not sure why"
        assert imp.task_id is None
        assert imp.suggested_role is None
        assert imp.blocked_files == []
        assert imp.evidence == {}
        assert isinstance(imp.created_at, datetime)

    def test_full_payload_round_trips(self):
        imp = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="cannot write tests/conftest.py",
            task_id="task-1-1",
            suggested_role="tester",
            blocked_files=["tests/conftest.py"],
            evidence={"detected_by": "check_file_restriction"},
        )
        d = imp.to_dict()
        imp2 = Impasse.from_dict(d)
        assert imp2.category == imp.category
        assert imp2.reason == imp.reason
        assert imp2.task_id == imp.task_id
        assert imp2.suggested_role == imp.suggested_role
        assert imp2.blocked_files == imp.blocked_files
        assert imp2.evidence == imp.evidence
        # created_at must survive isoformat round trip
        assert imp2.created_at == imp.created_at

    def test_reason_required(self):
        with pytest.raises(ValidationError):
            Impasse(category=ImpasseCategory.WRONG_ROLE, reason="")

    def test_category_must_be_known(self):
        with pytest.raises(ValidationError):
            Impasse(category="bogus", reason="x")  # type: ignore[arg-type]

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            Impasse(  # type: ignore[call-arg]
                category=ImpasseCategory.PLAN_BUG,
                reason="x",
                bogus_field=1,
            )

    def test_from_dict_uses_now_when_timestamp_missing(self):
        before = datetime.now(UTC)
        imp = Impasse.from_dict({"category": "external_blocker", "reason": "needs upstream merge"})
        after = datetime.now(UTC)
        assert before <= imp.created_at <= after
        assert imp.category == ImpasseCategory.EXTERNAL_BLOCKER

    def test_from_dict_handles_missing_optional_lists(self):
        imp = Impasse.from_dict(
            {
                "category": "wrong_role",
                "reason": "blocked",
                "suggested_role": "tester",
            }
        )
        assert imp.blocked_files == []
        assert imp.evidence == {}


class TestImpasseCategory:
    def test_string_values_match_schema(self):
        # The MCP tool schema enumerates these literal strings; if anyone
        # renames a category we want the test suite to flag it before it
        # hits a sandbox agent that emits the old name.
        assert ImpasseCategory.WRONG_ROLE.value == "wrong_role"
        assert ImpasseCategory.PLAN_BUG.value == "plan_bug"
        assert ImpasseCategory.EXTERNAL_BLOCKER.value == "external_blocker"
        assert ImpasseCategory.UNKNOWN.value == "unknown"
