"""
Tests for the yaml-tasks JSON Schema.

Covers:
- Tasks with valid role values (coder, tester, documenter) pass validation
- Tasks without role field pass validation (backward compatibility)
- Tasks with invalid role values are rejected by schema enum constraint
- additionalProperties enforcement still works with role field
- The canonical 'slices:' top-level key and the legacy 'phases:' alias
- The 'pr' block fields (title, description, test_plan, manual_steps,
  context_title, context_description)
- The canonical 'slice-<N>' string shape for a slice's dependencies field
- The 'serialized_chain_order' array field
- The shipped plan template (docs/templates/plan.md) validates end-to-end
"""

import json
from pathlib import Path

import pytest

try:
    import jsonschema
except ImportError:
    jsonschema = None

try:
    # Importing the production fence extractor also pulls in pyyaml (a
    # plan_parser dependency); a missing pyyaml therefore lands here too.
    from egg_contracts.plan_parser import parse_yaml_code_fence
except ImportError:
    parse_yaml_code_fence = None

REPO_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = REPO_ROOT / ".egg" / "schemas" / "yaml-tasks.schema.json"
PLAN_TEMPLATE_PATH = REPO_ROOT / "docs" / "templates" / "plan.md"


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


def _minimal_doc(top_key="slices", **task_kwargs):
    """Create a minimal valid yaml-tasks document with one slice and one task.

    ``top_key`` selects the top-level key: ``slices`` (canonical, post-#2137)
    or ``phases`` (legacy alias). Defaults to the canonical key so fixtures
    exercise the shape the plan template teaches.
    """
    return {
        top_key: [
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

    def test_mixed_roles_in_slice(self):
        """Slice with tasks having different valid roles should validate."""
        schema = _load_schema()
        doc = {
            "slices": [
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


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestYamlTasksSchemaTopLevelKey:
    """Tests for the canonical 'slices:' key and the legacy 'phases:' alias.

    Post-#2137 the canonical top-level key is 'slices'; 'phases' is retained
    as a backward-compatible alias. The schema must accept either.
    """

    def test_slices_top_level_key_is_valid(self):
        """The canonical 'slices:' top-level key should validate."""
        schema = _load_schema()
        jsonschema.validate(_minimal_doc(top_key="slices"), schema)

    def test_phases_top_level_key_is_valid(self):
        """The legacy 'phases:' alias should still validate."""
        schema = _load_schema()
        jsonschema.validate(_minimal_doc(top_key="phases"), schema)

    def test_missing_both_keys_rejected(self):
        """A document with neither 'slices' nor 'phases' should be rejected."""
        schema = _load_schema()
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"pr": {"title": "x"}}, schema)

    def test_unknown_top_level_key_rejected(self):
        """An unknown top-level key should still be rejected (additionalProperties)."""
        schema = _load_schema()
        doc = _minimal_doc()
        doc["unexpected"] = "bad"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestYamlTasksSchemaPrBlock:
    """Tests for the optional 'pr' metadata block.

    The canonical planner-emittable shape carries title (required),
    description, test_plan, manual_steps, and the #2548 context_title /
    context_description fields. Orchestrator-populated fields
    (context_branch, context_pr_number) are intentionally NOT in the schema
    so a planner emitting them is rejected.
    """

    def test_pr_block_with_test_plan_and_manual_steps_is_valid(self):
        """The pr block as taught by the plan template should validate."""
        schema = _load_schema()
        doc = _minimal_doc()
        doc["pr"] = {
            "title": "Concise PR title",
            "description": "Why and what.",
            "test_plan": "- Automated: pytest\n- Manual: click around",
            "manual_steps": "Pre-merge: run migration",
        }
        jsonschema.validate(doc, schema)

    def test_pr_block_with_context_fields_is_valid(self):
        """The #2548 context_title / context_description fields should validate."""
        schema = _load_schema()
        doc = _minimal_doc()
        doc["pr"] = {
            "title": "Concise PR title",
            "context_title": "Strategic plan for #123",
            "context_description": "Carries the refine analysis.",
        }
        jsonschema.validate(doc, schema)

    def test_pr_block_missing_title_rejected(self):
        """A pr block without the required title should be rejected."""
        schema = _load_schema()
        doc = _minimal_doc()
        doc["pr"] = {"description": "no title here"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_pr_block_orchestrator_field_rejected(self):
        """Orchestrator-populated pr fields are not planner-emittable and should be rejected."""
        schema = _load_schema()
        doc = _minimal_doc()
        doc["pr"] = {"title": "Concise PR title", "context_branch": "egg/context-123"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


def _doc_with_slice_field(**slice2_fields):
    """Create a minimal two-slice yaml-tasks document with extra fields on slice 2.

    Slice 2 always carries id/name/tasks; ``slice2_fields`` are merged on top,
    so a test can attach an arbitrary slice-level key (dependencies,
    serialized_chain_order, ...) and assert how the schema treats it.
    """
    second_slice = {
        "id": 2,
        "name": "Second slice",
        "tasks": [_minimal_task("TASK-2-1")],
    }
    second_slice.update(slice2_fields)
    return {
        "slices": [
            {
                "id": 1,
                "name": "First slice",
                "tasks": [_minimal_task("TASK-1-1")],
            },
            second_slice,
        ]
    }


def _doc_with_slice_dependencies(dependencies):
    """Create a minimal yaml-tasks document whose second slice carries a dependencies value."""
    return _doc_with_slice_field(dependencies=dependencies)


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestYamlTasksSchemaDependenciesField:
    """Tests for the dependencies field shape in the yaml-tasks schema.

    The schema enforces only ``type: string`` on ``dependencies`` — it does
    NOT carry a ``pattern`` for the canonical ``slice-<N>`` shape (OQ-1 in
    PR #2779). So an arbitrary string such as ``"banana"`` validates here;
    the ``slice-<N>`` convention is taught by the field description and
    enforced by the validate-yaml-tasks bin / plan_parser, not the schema.
    These tests therefore guard the ``type`` constraint, not the pattern.
    """

    def test_dependencies_slice_string_is_valid(self):
        """A slice with dependencies='slice-1' (the canonical shape) should validate."""
        schema = _load_schema()
        doc = _doc_with_slice_dependencies("slice-1")
        jsonschema.validate(doc, schema)

    def test_dependencies_int_array_is_rejected(self):
        """A slice with dependencies=[1] (non-canonical array form) should be rejected."""
        schema = _load_schema()
        doc = _doc_with_slice_dependencies([1])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
class TestYamlTasksSchemaSerializedChainOrder:
    """Tests for the 'serialized_chain_order' slice field.

    The slice DAG must be a forest, so a planner that hits a would-be
    multi-parent slice serialises the upstream cluster into a chain and
    records the chosen order on the downstream slice's
    'serialized_chain_order' field (#2137). The schema prescribes the
    canonical shape — an array of 'slice-<N>' id strings. The parser also
    tolerates a comma-separated string, but the schema deliberately does
    not, so a planner is taught only the canonical array form.
    """

    def test_serialized_chain_order_string_array_is_valid(self):
        """The canonical array-of-strings shape should validate."""
        schema = _load_schema()
        jsonschema.validate(
            _doc_with_slice_field(serialized_chain_order=["slice-1", "slice-2"]), schema
        )

    def test_serialized_chain_order_empty_array_is_valid(self):
        """An empty array (the default) should validate."""
        schema = _load_schema()
        jsonschema.validate(_doc_with_slice_field(serialized_chain_order=[]), schema)

    def test_serialized_chain_order_int_array_is_rejected(self):
        """Non-string array items should be rejected (items: type string)."""
        schema = _load_schema()
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_doc_with_slice_field(serialized_chain_order=[1]), schema)

    def test_serialized_chain_order_comma_string_is_rejected(self):
        """The comma-string form the parser tolerates is rejected by the schema.

        The schema is prescriptive: it teaches only the canonical array
        shape even though the lenient parser also accepts a comma-separated
        string.
        """
        schema = _load_schema()
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                _doc_with_slice_field(serialized_chain_order="slice-1,slice-2"), schema
            )


@pytest.mark.skipif(
    jsonschema is None or parse_yaml_code_fence is None,
    reason="jsonschema/pyyaml not installed",
)
class TestPlanTemplateValidatesAgainstSchema:
    """Regression guard: the shipped plan template must satisfy the schema.

    PR #2779 surfaced that the schema rejected docs/templates/plan.md. This
    test extracts the literal '# yaml-tasks' fenced block from the template
    — using the production fence extractor, so it cannot drift from the real
    parsing path — and validates it against the schema. The guarantee is
    scoped to the literal block: fields documented only in the template
    *prose* (e.g. 'serialized_chain_order', the 'depends_on' alias) are not
    present in the block and are covered by the dedicated schema tests above.
    """

    def test_plan_template_yaml_tasks_block_validates(self):
        if not PLAN_TEMPLATE_PATH.exists():
            pytest.skip("plan template not found")
        schema = _load_schema()
        doc, _, _ = parse_yaml_code_fence(PLAN_TEMPLATE_PATH.read_text())
        assert doc is not None, "no '# yaml-tasks' fenced block found in docs/templates/plan.md"
        jsonschema.validate(doc, schema)
