"""
Tests for the yaml-tasks JSON Schema.

Covers:
- Tasks with valid role values (coder, tester, documenter) pass validation
- Tasks without role field pass validation (backward compatibility)
- Tasks with invalid role values are rejected by schema enum constraint
- additionalProperties enforcement still works with role field
- The canonical 'slice-<N>' string shape for a phase's dependencies field
"""

import json
from pathlib import Path

import pytest

try:
    import jsonschema
except ImportError:
    jsonschema = None

SCHEMA_PATH = Path(__file__).parent.parent / ".egg" / "schemas" / "yaml-tasks.schema.json"


def _load_schema():
    """Load the yaml-tasks JSON Schema file."""
    if not SCHEMA_PATH.exists():
        pytest.skip("yaml-tasks JSON Schema file not yet created")
    with SCHEMA_PATH.open() as f:
        return json.load(f)


def _minimal_task(task_id="TASK-1-1", description="Do something", acceptance="Done", **kwargs):
    """Create a minimal valid task dict, with optional overrides."""
    task = {
        "id": task_id,
        "description": description,
        "acceptance": acceptance,
    }
    task.update(kwargs)
    return task


def _minimal_doc(**task_kwargs):
    """Create a minimal valid yaml-tasks document with one phase and one task."""
    return {
        "phases": [
            {
                "id": 1,
                "name": "Implementation",
                "tasks": [_minimal_task(**task_kwargs)],
            }
        ]
    }


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestYamlTasksSchemaRoleField:
    """Tests for the role field in the yaml-tasks schema."""

    def test_task_without_role_is_valid(self):
        """Tasks without a role field should validate (backward compatibility)."""
        schema = _load_schema()
        doc = _minimal_doc()
        jsonschema.validate(doc, schema)

    def test_task_with_role_coder_is_valid(self):
        """Tasks with role='coder' should validate."""
        schema = _load_schema()
        doc = _minimal_doc(role="coder")
        jsonschema.validate(doc, schema)

    def test_task_with_role_tester_is_valid(self):
        """Tasks with role='tester' should validate."""
        schema = _load_schema()
        doc = _minimal_doc(role="tester")
        jsonschema.validate(doc, schema)

    def test_task_with_role_documenter_is_valid(self):
        """Tasks with role='documenter' should validate."""
        schema = _load_schema()
        doc = _minimal_doc(role="documenter")
        jsonschema.validate(doc, schema)

    def test_task_with_invalid_role_rejected(self):
        """Tasks with an invalid role value should be rejected by enum constraint."""
        schema = _load_schema()
        doc = _minimal_doc(role="invalid_role")
        with pytest.raises(jsonschema.ValidationError, match="'invalid_role' is not one of"):
            jsonschema.validate(doc, schema)

    def test_task_with_empty_role_rejected(self):
        """Tasks with an empty string role should be rejected."""
        schema = _load_schema()
        doc = _minimal_doc(role="")
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_task_with_numeric_role_rejected(self):
        """Tasks with a numeric role should be rejected (wrong type)."""
        schema = _load_schema()
        doc = _minimal_doc(role=123)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_mixed_roles_in_phase(self):
        """Phase with tasks having different valid roles should validate."""
        schema = _load_schema()
        doc = {
            "phases": [
                {
                    "id": 1,
                    "name": "Implementation",
                    "tasks": [
                        _minimal_task("TASK-1-1", "Code it", "Works", role="coder"),
                        _minimal_task("TASK-1-2", "Test it", "Passes", role="tester"),
                        _minimal_task("TASK-1-3", "Document it", "Readable", role="documenter"),
                        _minimal_task("TASK-1-4", "Other work", "Done"),  # No role
                    ],
                }
            ]
        }
        jsonschema.validate(doc, schema)

    def test_task_with_files_and_role(self):
        """Task with both files and role should validate."""
        schema = _load_schema()
        doc = _minimal_doc(role="tester", files=["tests/test_foo.py"])
        jsonschema.validate(doc, schema)

    def test_additional_properties_still_rejected_with_role(self):
        """Additional unknown properties should still be rejected."""
        schema = _load_schema()
        doc = _minimal_doc(role="coder", unknown_field="bad")
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


def _doc_with_phase_dependencies(dependencies):
    """Create a minimal yaml-tasks document whose second phase carries a dependencies value."""
    return {
        "phases": [
            {
                "id": 1,
                "name": "First phase",
                "tasks": [_minimal_task("TASK-1-1")],
            },
            {
                "id": 2,
                "name": "Second phase",
                "dependencies": dependencies,
                "tasks": [_minimal_task("TASK-2-1")],
            },
        ]
    }


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestYamlTasksSchemaDependenciesField:
    """Tests for the canonical dependencies field shape in the yaml-tasks schema."""

    def test_dependencies_slice_string_is_valid(self):
        """A phase with dependencies='slice-1' (the canonical shape) should validate."""
        schema = _load_schema()
        doc = _doc_with_phase_dependencies("slice-1")
        jsonschema.validate(doc, schema)

    def test_dependencies_int_array_is_rejected(self):
        """A phase with dependencies=[1] (non-canonical array form) should be rejected."""
        schema = _load_schema()
        doc = _doc_with_phase_dependencies([1])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)
