"""Tests for agent anchor Pydantic models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from egg_anchor.models import (
    AgentAnchor,
    AnchorMeta,
    AnchorStatus,
    BRCPhase,
    BRCState,
    Decision,
    ErrorEncountered,
    KeyContext,
    ProgressItem,
    ProgressState,
    TaskInfo,
)


def _make_meta(**overrides):
    """Create an AnchorMeta with defaults."""
    defaults = {
        "schema_version": "1.0",
        "created_at": datetime(2026, 3, 17, 10, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 3, 17, 10, 0, 0, tzinfo=timezone.utc),
        "sequence": 0,
    }
    defaults.update(overrides)
    return AnchorMeta(**defaults)


def _make_anchor(**overrides):
    """Create a minimal valid AgentAnchor with defaults."""
    now = datetime(2026, 3, 17, 10, 0, 0, tzinfo=timezone.utc)
    defaults = {
        "_meta": {
            "schema_version": "1.0",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "sequence": 1,
        },
        "agent_id": "coder-abc123",
        "role": "coder",
        "team": ["tester-def456"],
        "task": {
            "id": "task-1",
            "description": "Implement feature X",
            "phase": "implement",
        },
        "status": "working",
        "pipeline_id": "pipeline-xyz",
        "progress": [],
        "decisions": [],
        "brc_state": {
            "phase": "orient",
            "acks": [],
            "nacks": [],
        },
        "key_context": [],
        "errors_encountered": [],
        "files_modified": [],
    }
    defaults.update(overrides)
    return AgentAnchor.model_validate(defaults)


class TestEnums:
    """Test enum values match the JSON schema."""

    def test_anchor_status_values(self):
        assert set(AnchorStatus) == {
            AnchorStatus.INITIALIZING,
            AnchorStatus.WORKING,
            AnchorStatus.PROPOSED,
            AnchorStatus.CONFIRMED,
            AnchorStatus.BLOCKED,
            AnchorStatus.FAILED,
        }

    def test_brc_phase_values(self):
        assert set(BRCPhase) == {
            BRCPhase.ORIENT,
            BRCPhase.WORKING,
            BRCPhase.PROPOSED,
            BRCPhase.REVIEWING,
            BRCPhase.CONFIRMED,
        }

    def test_progress_state_values(self):
        assert set(ProgressState) == {
            ProgressState.PENDING,
            ProgressState.WORKING,
            ProgressState.COMPLETE,
            ProgressState.BLOCKED,
        }

    def test_enum_string_values(self):
        assert AnchorStatus.WORKING == "working"
        assert BRCPhase.ORIENT == "orient"
        assert ProgressState.COMPLETE == "complete"


class TestAnchorMeta:
    """Test AnchorMeta model."""

    def test_valid_meta(self):
        meta = _make_meta()
        assert meta.schema_version == "1.0"
        assert meta.sequence == 0

    def test_negative_sequence_rejected(self):
        with pytest.raises(ValidationError):
            _make_meta(sequence=-1)


class TestTaskInfo:
    """Test TaskInfo model."""

    def test_valid_task(self):
        task = TaskInfo(id="task-1", description="Do something", phase="implement")
        assert task.id == "task-1"
        assert task.phase == "implement"


class TestKeyContext:
    """Test KeyContext max_length constraints."""

    def test_valid_key_context(self):
        kc = KeyContext(label="branch", value="egg/feature-x")
        assert kc.label == "branch"

    def test_label_too_long(self):
        with pytest.raises(ValidationError):
            KeyContext(label="x" * 51, value="short")

    def test_value_too_long(self):
        with pytest.raises(ValidationError):
            KeyContext(label="short", value="x" * 501)

    def test_label_at_max(self):
        kc = KeyContext(label="x" * 50, value="ok")
        assert len(kc.label) == 50

    def test_value_at_max(self):
        kc = KeyContext(label="ok", value="x" * 500)
        assert len(kc.value) == 500


class TestErrorEncountered:
    """Test ErrorEncountered max_length constraints."""

    def test_valid_error(self):
        now = datetime.now(tz=timezone.utc)
        err = ErrorEncountered(error="Something broke", timestamp=now)
        assert err.resolution is None

    def test_error_too_long(self):
        now = datetime.now(tz=timezone.utc)
        with pytest.raises(ValidationError):
            ErrorEncountered(error="x" * 201, timestamp=now)

    def test_resolution_too_long(self):
        now = datetime.now(tz=timezone.utc)
        with pytest.raises(ValidationError):
            ErrorEncountered(error="ok", resolution="x" * 201, timestamp=now)


class TestAgentAnchor:
    """Test the main AgentAnchor model."""

    def test_minimal_anchor(self):
        anchor = _make_anchor()
        assert anchor.agent_id == "coder-abc123"
        assert anchor.role == "coder"
        assert anchor.status == AnchorStatus.WORKING

    def test_round_trip_serialization(self):
        """Test to_dict -> from_dict round-trip preserves data."""
        anchor = _make_anchor(
            progress=[
                {
                    "step": "Running tests",
                    "state": "working",
                    "detail": "pytest suite 2/5",
                    "timestamp": "2026-03-17T10:05:00+00:00",
                }
            ],
            key_context=[
                {"label": "branch", "value": "egg/feature-x"},
            ],
            files_modified=["src/main.py", "tests/test_main.py"],
        )
        data = anchor.to_dict()
        restored = AgentAnchor.from_dict(data)

        assert restored.agent_id == anchor.agent_id
        assert restored.role == anchor.role
        assert restored.status == anchor.status
        assert restored.pipeline_id == anchor.pipeline_id
        assert len(restored.progress) == 1
        assert restored.progress[0].step == "Running tests"
        assert restored.progress[0].state == ProgressState.WORKING
        assert len(restored.key_context) == 1
        assert restored.key_context[0].label == "branch"
        assert restored.files_modified == ["src/main.py", "tests/test_main.py"]

    def test_to_dict_datetime_format(self):
        """Verify datetimes are serialized as ISO strings."""
        anchor = _make_anchor()
        data = anchor.to_dict()
        meta = data["_meta"]
        assert isinstance(meta["created_at"], str)
        assert "T" in meta["created_at"]

    def test_max_progress_items(self):
        """Exceeding max progress items raises validation error."""
        now = datetime.now(tz=timezone.utc).isoformat()
        items = [
            {"step": f"step-{i}", "state": "pending", "timestamp": now}
            for i in range(11)
        ]
        with pytest.raises(ValidationError, match="progress"):
            _make_anchor(progress=items)

    def test_max_decisions(self):
        """Exceeding max decisions raises validation error."""
        now = datetime.now(tz=timezone.utc).isoformat()
        items = [
            {"id": f"d-{i}", "question": f"Q{i}?", "timestamp": now}
            for i in range(9)
        ]
        with pytest.raises(ValidationError, match="decisions"):
            _make_anchor(decisions=items)

    def test_max_key_context(self):
        """Exceeding max key context items raises validation error."""
        items = [{"label": f"k{i}", "value": f"v{i}"} for i in range(6)]
        with pytest.raises(ValidationError, match="key_context"):
            _make_anchor(key_context=items)

    def test_max_errors(self):
        """Exceeding max errors raises validation error."""
        now = datetime.now(tz=timezone.utc).isoformat()
        items = [{"error": f"e{i}", "timestamp": now} for i in range(6)]
        with pytest.raises(ValidationError, match="errors_encountered"):
            _make_anchor(errors_encountered=items)

    def test_max_files(self):
        """Exceeding max files raises validation error."""
        files = [f"file{i}.py" for i in range(16)]
        with pytest.raises(ValidationError, match="files_modified"):
            _make_anchor(files_modified=files)

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            _make_anchor(status="invalid_status")

    def test_all_statuses_accepted(self):
        for status in AnchorStatus:
            anchor = _make_anchor(status=status.value)
            assert anchor.status == status

    def test_brc_state_defaults(self):
        anchor = _make_anchor()
        assert anchor.brc_state.phase == BRCPhase.ORIENT
        assert anchor.brc_state.acks == []
        assert anchor.brc_state.nacks == []
        assert anchor.brc_state.proposed_at is None
        assert anchor.brc_state.last_message_id is None

    def test_full_anchor_with_all_fields(self):
        """Test a fully populated anchor."""
        now = datetime.now(tz=timezone.utc).isoformat()
        anchor = _make_anchor(
            status="proposed",
            team=["tester-1", "documenter-2"],
            progress=[
                {"step": "implement", "state": "complete", "timestamp": now},
                {"step": "test", "state": "working", "detail": "3/5 suites", "timestamp": now},
            ],
            decisions=[
                {
                    "id": "d-1",
                    "question": "Use Redis or Postgres?",
                    "answer": "Redis",
                    "decided_by": "human",
                    "timestamp": now,
                }
            ],
            brc_state={
                "phase": "proposed",
                "proposed_at": now,
                "acks": ["tester-1"],
                "nacks": [],
                "last_message_id": "msg-abc",
            },
            key_context=[
                {"label": "branch", "value": "egg/anchor-impl"},
                {"label": "commit", "value": "abc1234"},
            ],
            errors_encountered=[
                {"error": "Test flake in CI", "resolution": "Retried", "timestamp": now}
            ],
            files_modified=["shared/egg_anchor/models.py"],
        )
        assert anchor.status == AnchorStatus.PROPOSED
        assert len(anchor.team) == 2
        assert len(anchor.progress) == 2
        assert anchor.decisions[0].answer == "Redis"
        assert anchor.brc_state.phase == BRCPhase.PROPOSED
        assert anchor.brc_state.last_message_id == "msg-abc"
        assert len(anchor.key_context) == 2
        assert anchor.errors_encountered[0].resolution == "Retried"


# === GAP TESTS: Additional coverage for edge cases ===


class TestToDictNoneOmission:
    """Test that to_dict properly omits None optional fields."""

    def test_optional_brc_fields_omitted_when_none(self):
        """BRC optional fields set to None should not appear in dict."""
        anchor = _make_anchor()
        data = anchor.to_dict()
        assert "proposed_at" not in data["brc_state"]
        assert "last_message_id" not in data["brc_state"]

    def test_optional_fields_present_when_set(self):
        """Optional fields set to a value should appear in dict."""
        now = datetime.now(tz=timezone.utc).isoformat()
        anchor = _make_anchor(
            brc_state={
                "phase": "proposed",
                "proposed_at": now,
                "acks": ["tester-1"],
                "nacks": [],
                "last_message_id": "msg-123",
            }
        )
        data = anchor.to_dict()
        assert "proposed_at" in data["brc_state"]
        assert "last_message_id" in data["brc_state"]

    def test_error_resolution_omitted_when_none(self):
        """ErrorEncountered.resolution should be omitted when None."""
        now = datetime.now(tz=timezone.utc).isoformat()
        anchor = _make_anchor(
            errors_encountered=[{"error": "test error", "timestamp": now}]
        )
        data = anchor.to_dict()
        assert "resolution" not in data["errors_encountered"][0]

    def test_progress_detail_omitted_when_none(self):
        """ProgressItem.detail should be omitted when None."""
        now = datetime.now(tz=timezone.utc).isoformat()
        anchor = _make_anchor(
            progress=[{"step": "test", "state": "working", "timestamp": now}]
        )
        data = anchor.to_dict()
        assert "detail" not in data["progress"][0]

    def test_decision_answer_omitted_when_none(self):
        """Decision.answer should be omitted when None."""
        now = datetime.now(tz=timezone.utc).isoformat()
        anchor = _make_anchor(
            decisions=[{"id": "d-1", "question": "Which?", "timestamp": now}]
        )
        data = anchor.to_dict()
        assert "answer" not in data["decisions"][0]
        assert "decided_by" not in data["decisions"][0]


class TestFromDictEdgeCases:
    """Test edge cases in from_dict deserialization."""

    def test_from_dict_preserves_team_order(self):
        """Team list order should be preserved."""
        anchor = _make_anchor(team=["agent-c", "agent-a", "agent-b"])
        data = anchor.to_dict()
        restored = AgentAnchor.from_dict(data)
        assert restored.team == ["agent-c", "agent-a", "agent-b"]

    def test_from_dict_preserves_files_order(self):
        """Files modified list order should be preserved."""
        files = ["z.py", "a.py", "m.py"]
        anchor = _make_anchor(files_modified=files)
        data = anchor.to_dict()
        restored = AgentAnchor.from_dict(data)
        assert restored.files_modified == files


class TestPopulateByName:
    """Test that models accept both alias and field names."""

    def test_to_dict_uses_alias(self):
        """to_dict should use _meta alias, not 'meta'."""
        anchor = _make_anchor()
        data = anchor.to_dict()
        assert "_meta" in data
        assert "meta" not in data


class TestBoundaryValues:
    """Test boundary conditions for array limits."""

    def test_exactly_max_progress(self):
        """Exactly 10 progress items should be accepted."""
        now = datetime.now(tz=timezone.utc).isoformat()
        items = [
            {"step": f"step-{i}", "state": "pending", "timestamp": now}
            for i in range(10)
        ]
        anchor = _make_anchor(progress=items)
        assert len(anchor.progress) == 10

    def test_exactly_max_decisions(self):
        """Exactly 8 decisions should be accepted."""
        now = datetime.now(tz=timezone.utc).isoformat()
        items = [
            {"id": f"d-{i}", "question": f"Q{i}?", "timestamp": now}
            for i in range(8)
        ]
        anchor = _make_anchor(decisions=items)
        assert len(anchor.decisions) == 8

    def test_exactly_max_key_context(self):
        """Exactly 5 key context items should be accepted."""
        items = [{"label": f"k{i}", "value": f"v{i}"} for i in range(5)]
        anchor = _make_anchor(key_context=items)
        assert len(anchor.key_context) == 5

    def test_exactly_max_errors(self):
        """Exactly 5 errors should be accepted."""
        now = datetime.now(tz=timezone.utc).isoformat()
        items = [{"error": f"e{i}", "timestamp": now} for i in range(5)]
        anchor = _make_anchor(errors_encountered=items)
        assert len(anchor.errors_encountered) == 5

    def test_exactly_max_files(self):
        """Exactly 15 files should be accepted."""
        files = [f"file{i}.py" for i in range(15)]
        anchor = _make_anchor(files_modified=files)
        assert len(anchor.files_modified) == 15

    def test_empty_team(self):
        """Empty team list should be valid."""
        anchor = _make_anchor(team=[])
        assert anchor.team == []

    def test_sequence_zero(self):
        """Sequence 0 is valid (minimum)."""
        now = datetime.now(tz=timezone.utc).isoformat()
        anchor = _make_anchor(
            **{"_meta": {"schema_version": "1.0", "created_at": now, "updated_at": now, "sequence": 0}}
        )
        assert anchor.meta.sequence == 0
