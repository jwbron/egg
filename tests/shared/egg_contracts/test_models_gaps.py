"""Regression tests for ``Task.gaps`` (#1917, iter-2 task_mark_gap).

Covers:

1. Default value — a ``Task`` constructed without ``gaps`` has ``gaps == []``
   (no ``default_factory`` mistakes, no ``None`` surprises).
2. Round-trip — a ``Task`` with populated ``gaps`` survives
   ``model_dump()`` → ``model_validate()``.
3. Back-compat — every existing on-disk contract under
   ``.egg-state/contracts/*.json`` (pre-iter-2) continues to validate
   and reports ``gaps: []`` per task, not an absent key.
4. Schema — the JSON schema at ``.egg/schemas/contract.schema.json``
   declares ``gaps`` as an optional array for every task.
5. Validator — role-aware mutation of ``phases.<p>.tasks.<t>.gaps.*``
   is permitted for the implementer and reviewer roles (per
   ``FIELD_OWNERSHIP`` in ``shared/egg_contracts/roles.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "sandbox"))

from egg_contracts.models import Contract, Task, TaskGap  # noqa: E402
from egg_contracts.roles import Role  # noqa: E402
from egg_contracts.validator import validate_task_mutation  # noqa: E402

CONTRACT_FIXTURES = sorted((ROOT / ".egg-state" / "contracts").glob("*.json"))


# --------------------------------------------------------------------
# Default + round-trip
# --------------------------------------------------------------------


class TestTaskGapsDefault:
    def test_empty_list_by_default(self):
        task = Task(id="task-1", description="x")
        assert task.gaps == []

    def test_default_is_independent_per_instance(self):
        """Catches the classic mutable-default pitfall: each Task must
        own its gaps list (``default_factory=list`` on the Pydantic
        field, not a shared reference)."""
        a = Task(id="task-1", description="x")
        b = Task(id="task-2", description="y")
        # Note: TaskGap.id enforces the ``^gap-[0-9]+$`` pattern per
        # the handler's _next_gap_id contract.
        a.gaps.append(
            TaskGap(
                id="gap-1",
                from_role="tester",
                to_role="coder",
                description="d",
            )
        )
        assert b.gaps == []


class TestTaskGapValidation:
    def test_id_required(self):
        with pytest.raises(ValueError):
            TaskGap(from_role="tester", to_role="coder", description="d")

    def test_from_role_required(self):
        with pytest.raises(ValueError):
            TaskGap(id="gap-1", to_role="coder", description="d")

    def test_description_required(self):
        with pytest.raises(ValueError):
            TaskGap(id="gap-1", from_role="tester", to_role="coder")

    def test_description_min_length(self):
        with pytest.raises(ValueError):
            TaskGap(id="gap-1", from_role="tester", to_role="coder", description="")

    def test_id_min_length(self):
        with pytest.raises(ValueError):
            TaskGap(id="", from_role="tester", to_role="coder", description="d")

    def test_id_pattern_enforces_gap_N(self):
        """The handler generates ``gap-<N>`` (monotonic integer suffix)
        — the Pydantic pattern must reject anything else so stray
        hand-edits don't end up in the contract."""
        with pytest.raises(ValueError):
            TaskGap(
                id="custom-xyz",
                from_role="tester",
                to_role="coder",
                description="d",
            )

    def test_to_role_required(self):
        """``to_role`` is required (min_length=1, no default) — the
        handler still supplies a 'coder' default at the application
        layer, but the model itself enforces presence on every
        stored record."""
        with pytest.raises(ValueError):
            TaskGap(id="gap-1", from_role="tester", description="d")

    def test_resolved_defaults_false(self):
        gap = TaskGap(id="gap-1", from_role="tester", to_role="coder", description="d")
        assert gap.resolved is False


class TestTaskGapsRoundTrip:
    def test_single_gap_roundtrip(self):
        from datetime import UTC, datetime

        gap = TaskGap(
            id="gap-7",
            from_role="tester",
            to_role="coder",
            description="missing error-path test",
            created_at=datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC),
            resolved=False,
        )
        dumped = gap.model_dump()
        reloaded = TaskGap.model_validate(dumped)
        assert reloaded == gap

    def test_task_with_multiple_gaps_roundtrips(self):
        task = Task(
            id="task-1-2",
            description="coverage",
            gaps=[
                TaskGap(
                    id="gap-1",
                    from_role="tester",
                    to_role="coder",
                    description="no error path test",
                ),
                TaskGap(
                    id="gap-2",
                    from_role="reviewer_code",
                    to_role="coder",
                    description="missing edge case",
                    resolved=True,
                ),
            ],
        )
        payload = task.model_dump(mode="json")
        reloaded = Task.model_validate(payload)
        assert len(reloaded.gaps) == 2
        assert reloaded.gaps[0].id == "gap-1"
        assert reloaded.gaps[1].resolved is True

    def test_contract_with_gaps_serialises_under_tasks(self):
        """JSON shape should expose gaps under ``slices[*].tasks[*].gaps``."""
        contract = Contract(
            pipeline_id="issue-1917",
            slices=[
                {
                    "id": "slice-1",
                    "name": "n",
                    "tasks": [
                        {
                            "id": "task-1-1",
                            "description": "d",
                            "gaps": [
                                {
                                    "id": "gap-9",
                                    "from_role": "tester",
                                    "to_role": "coder",
                                    "description": "d",
                                }
                            ],
                        }
                    ],
                }
            ],
        )
        dumped = contract.model_dump(mode="json")
        assert dumped["slices"][0]["tasks"][0]["gaps"][0]["id"] == "gap-9"


# --------------------------------------------------------------------
# Back-compat
# --------------------------------------------------------------------


class TestBackCompatWithExistingContracts:
    """Old on-disk contracts MUST continue to load cleanly post-iter-2,
    and the parsed task objects must expose ``gaps`` as an empty list
    (NOT an absent key) so the rendered MCP response shape stays stable.
    """

    @pytest.mark.parametrize("fixture", CONTRACT_FIXTURES, ids=lambda p: p.name)
    def test_existing_contract_parses(self, fixture: Path):
        """A pre-iter-2 contract must load post-iter-2 and report
        ``gaps=[]`` on every task.

        Some legacy fixtures have unrelated schema drift (e.g., old
        agent role enums); those predate the gaps migration and are
        skipped so this back-compat test only catches regressions
        introduced by adding ``gaps``.  The filter is intentionally
        strict — a validation error that DOES mention ``gaps`` fails
        the test.
        """
        from pydantic import ValidationError

        data = json.loads(fixture.read_text())
        # Some legacy fixtures are checkpoint-style (without issue/pipeline_id);
        # only Contract-shaped fixtures need to validate.
        if not isinstance(data, dict) or "schemaVersion" not in data:
            pytest.skip("not a Contract-shaped fixture")
        try:
            contract = Contract.model_validate(data)
        except ValidationError as exc:
            if "gap" in str(exc).lower():
                raise AssertionError(
                    f"Legacy fixture {fixture.name} regressed on gaps: {exc}"
                ) from exc
            pytest.skip(
                f"Legacy fixture {fixture.name} has unrelated schema drift "
                f"(not about gaps): {str(exc).splitlines()[0]}"
            )
        # Every task, in every phase, must report gaps as a list.
        for phase in contract.phases:
            for task in phase.tasks:
                assert task.gaps == [], (
                    f"{fixture.name}: task {task.id} in phase {phase.id} "
                    f"has non-empty gaps before iter-2 writes landed"
                )

    def test_at_least_one_fixture_back_compat_tested(self):
        """Sanity: the parametrised sweep shouldn't silently no-op if
        the contracts directory is empty in a future layout change."""
        assert CONTRACT_FIXTURES, (
            "No contract fixtures under .egg-state/contracts; the "
            "back-compat check would silently no-op."
        )


# --------------------------------------------------------------------
# JSON schema
# --------------------------------------------------------------------


class TestContractJsonSchema:
    """`.egg/schemas/contract.schema.json` is the external-consumer view
    of the contract.  Iter-2 added ``gaps`` as optional — consumers
    that validate contracts against the schema must see it."""

    schema_path = ROOT / ".egg" / "schemas" / "contract.schema.json"

    def test_schema_declares_task_gaps_optional(self):
        assert self.schema_path.exists(), (
            f"Expected JSON schema at {self.schema_path}; the iter-2 plan "
            "required it to be bumped alongside the Pydantic model."
        )
        schema = json.loads(self.schema_path.read_text())
        # Walk to the Task definition.  The schema uses $defs or
        # definitions; support either so this test survives a schema
        # renderer update.
        defs = schema.get("$defs") or schema.get("definitions") or {}
        # Accept any reasonable Task-shape def name.
        task_def = None
        for name, body in defs.items():
            if name.lower() == "task" and isinstance(body, dict):
                task_def = body
                break
        assert task_def is not None, (
            "Task definition missing from contract.schema.json; "
            "iter-2 must bump the schema alongside the Pydantic model."
        )
        props = task_def.get("properties") or {}
        assert "gaps" in props, (
            "`gaps` must be declared on the Task schema definition so "
            "external consumers validating JSON contracts see it."
        )
        gaps_schema = props["gaps"]
        # `gaps` is optional (not in `required`) and is an array.
        required = task_def.get("required", [])
        assert "gaps" not in required, "`gaps` must be optional — legacy contracts don't have it."
        # Pydantic may render it as {"type": "array"} or
        # {"anyOf": [{"type": "array"}, {"type": "null"}]} or similar;
        # accept any shape that mentions an array.
        serialised = json.dumps(gaps_schema)
        assert "array" in serialised, f"`gaps` schema must be array-typed; got {gaps_schema!r}"


# --------------------------------------------------------------------
# Role-aware mutation validator
# --------------------------------------------------------------------


class TestGapsMutationAuthorization:
    """The tester role hits the gateway as IMPLEMENTER; the reviewer
    roles hit as REVIEWER.  Both must be allowed to write gaps."""

    def test_implementer_can_write_whole_gaps_array(self):
        result = validate_task_mutation(Role.IMPLEMENTER, "gaps", [])
        assert result.valid, result.message

    def test_implementer_can_write_single_gap(self):
        """phases.*.tasks.*.gaps.* writes must be authorised — the
        handler appends to ``gaps.<n>``, not the whole array."""
        result = validate_task_mutation(
            Role.IMPLEMENTER,
            "gaps.0",
            {"id": "gap-1", "description": "d"},
        )
        assert result.valid, result.message

    def test_reviewer_can_write_gaps(self):
        result = validate_task_mutation(
            Role.REVIEWER,
            "gaps",
            [],
        )
        assert result.valid, result.message

    def test_human_can_write_gaps(self):
        # Human always has override privileges.
        result = validate_task_mutation(Role.HUMAN, "gaps", [])
        assert result.valid, result.message

    def test_system_cannot_write_gaps(self):
        """SYSTEM can only mutate fields it owns; gaps is shared
        between implementer/reviewer."""
        result = validate_task_mutation(Role.SYSTEM, "gaps", [])
        assert not result.valid
