"""
Tests for the agent-anchor JSON Schema.

Covers:
- Valid anchor documents pass validation
- Missing required fields are rejected
- Field type violations are rejected
- maxItems constraints are enforced
- maxLength constraints are enforced
- additionalProperties enforcement
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

try:
    import jsonschema
except ImportError:
    jsonschema = None

SCHEMA_PATH = Path(__file__).parent.parent / ".egg" / "schemas" / "agent-anchor.schema.json"


def _load_schema():
    """Load the JSON Schema file."""
    if not SCHEMA_PATH.exists():
        pytest.skip("JSON Schema file not yet created")
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def _make_valid_document(**overrides):
    """Create a valid anchor document."""
    now = datetime(2026, 3, 17, 10, 0, 0, tzinfo=UTC).isoformat()
    doc = {
        "_meta": {
            "schema_version": "1.0",
            "created_at": now,
            "updated_at": now,
            "sequence": 1,
        },
        "agent_id": "coder-abc12345",
        "role": "coder",
        "team": ["tester-def456"],
        "task": {"id": "task-1", "description": "Test task", "phase": "implement"},
        "status": "working",
        "pipeline_id": "issue-1032",
        "progress": [],
        "decisions": [],
        "brc_state": {"phase": "orient", "acks": [], "nacks": []},
        "key_context": [],
        "errors_encountered": [],
        "files_modified": [],
    }
    doc.update(overrides)
    return doc


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestSchemaValidation:
    """Tests for JSON Schema validation of anchor documents."""

    def test_schema_is_valid(self):
        """The schema file is valid JSON Schema."""
        schema = _load_schema()
        assert "type" in schema
        assert schema["type"] == "object"

    def test_valid_minimal_document(self):
        """Minimal valid document passes."""
        schema = _load_schema()
        doc = _make_valid_document()
        jsonschema.validate(doc, schema)

    def test_valid_full_document(self):
        """Fully populated document passes."""
        now = datetime.now(tz=UTC).isoformat()
        schema = _load_schema()
        doc = _make_valid_document(
            progress=[
                {"step": "implement", "state": "complete", "timestamp": now},
                {"step": "test", "state": "working", "detail": "3/5", "timestamp": now},
            ],
            decisions=[
                {
                    "id": "d-1",
                    "question": "Use Redis?",
                    "answer": "Yes",
                    "decided_by": "human",
                    "timestamp": now,
                }
            ],
            brc_state={
                "phase": "proposed",
                "proposed_at": now,
                "acks": ["tester-1"],
                "nacks": [],
                "last_message_id": "msg-1",
            },
            key_context=[{"label": "branch", "value": "egg/test"}],
            errors_encountered=[{"error": "flake", "resolution": "retry", "timestamp": now}],
            files_modified=["models.py"],
        )
        jsonschema.validate(doc, schema)

    def test_missing_meta_fails(self):
        schema = _load_schema()
        doc = _make_valid_document()
        del doc["_meta"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_missing_agent_id_fails(self):
        schema = _load_schema()
        doc = _make_valid_document()
        del doc["agent_id"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_missing_pipeline_id_fails(self):
        schema = _load_schema()
        doc = _make_valid_document()
        del doc["pipeline_id"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_missing_task_fails(self):
        schema = _load_schema()
        doc = _make_valid_document()
        del doc["task"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_progress_max_items(self):
        now = datetime.now(tz=UTC).isoformat()
        schema = _load_schema()
        doc = _make_valid_document(
            progress=[{"step": f"s{i}", "state": "pending", "timestamp": now} for i in range(11)]
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_decisions_max_items(self):
        now = datetime.now(tz=UTC).isoformat()
        schema = _load_schema()
        doc = _make_valid_document(
            decisions=[{"id": f"d-{i}", "question": f"Q{i}?", "timestamp": now} for i in range(9)]
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_key_context_max_items(self):
        schema = _load_schema()
        doc = _make_valid_document(
            key_context=[{"label": f"k{i}", "value": f"v{i}"} for i in range(6)]
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_errors_max_items(self):
        now = datetime.now(tz=UTC).isoformat()
        schema = _load_schema()
        doc = _make_valid_document(
            errors_encountered=[{"error": f"e{i}", "timestamp": now} for i in range(6)]
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_files_max_items(self):
        schema = _load_schema()
        doc = _make_valid_document(files_modified=[f"file_{i}.py" for i in range(16)])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_invalid_status_enum(self):
        schema = _load_schema()
        doc = _make_valid_document(status="invalid")
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_invalid_progress_state(self):
        now = datetime.now(tz=UTC).isoformat()
        schema = _load_schema()
        doc = _make_valid_document(
            progress=[{"step": "test", "state": "invalid", "timestamp": now}]
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_additional_properties_top_level(self):
        schema = _load_schema()
        doc = _make_valid_document()
        doc["unknown"] = "field"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_additional_properties_in_meta(self):
        schema = _load_schema()
        doc = _make_valid_document()
        doc["_meta"]["extra"] = "field"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_key_context_label_max_length(self):
        schema = _load_schema()
        doc = _make_valid_document(key_context=[{"label": "x" * 51, "value": "ok"}])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_key_context_value_max_length(self):
        schema = _load_schema()
        doc = _make_valid_document(key_context=[{"label": "ok", "value": "x" * 501}])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_error_max_length(self):
        now = datetime.now(tz=UTC).isoformat()
        schema = _load_schema()
        doc = _make_valid_document(errors_encountered=[{"error": "x" * 201, "timestamp": now}])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_meta_sequence_must_be_integer(self):
        schema = _load_schema()
        doc = _make_valid_document()
        doc["_meta"]["sequence"] = "not-a-number"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_meta_sequence_minimum_zero(self):
        schema = _load_schema()
        doc = _make_valid_document()
        doc["_meta"]["sequence"] = -1
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_empty_arrays_valid(self):
        schema = _load_schema()
        doc = _make_valid_document()
        jsonschema.validate(doc, schema)

    def test_all_valid_statuses(self):
        schema = _load_schema()
        for status in ["initializing", "working", "proposed", "confirmed", "blocked", "failed"]:
            doc = _make_valid_document(status=status)
            jsonschema.validate(doc, schema)

    def test_all_valid_brc_phases(self):
        schema = _load_schema()
        for phase in ["orient", "working", "proposed", "reviewing", "confirmed"]:
            doc = _make_valid_document(brc_state={"phase": phase, "acks": [], "nacks": []})
            jsonschema.validate(doc, schema)
