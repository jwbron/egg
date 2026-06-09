"""Regression tests for ``Contract.task_description`` (#3033).

Covers:

1. Default value — a ``Contract`` constructed without ``task_description``
   has ``task_description is None`` (issue-driven default).
2. Round-trip — a ``Contract`` with populated ``task_description`` survives
   ``model_dump()`` → ``model_validate()`` untruncated.
3. Schema parity — the JSON schema at ``.egg/schemas/contract.schema.json``
   declares ``task_description`` as an optional string + null. External
   consumers validating contracts against the schema (it sets
   ``additionalProperties: false`` at the top level) would otherwise reject
   any contract carrying the field.
4. Schema default-version pin — the schema-level ``schemaVersion.default``
   tracks the Pydantic model's current default. The model rolled forward to
   ``1.3`` for #3033; the schema must follow so a hand-validated contract
   without an explicit ``schemaVersion`` doesn't drift backward.

Mirrors the iter-2 pattern in ``test_models_gaps.py::TestContractJsonSchema``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "sandbox"))

from egg_contracts.models import Contract, IssueInfo  # noqa: E402

SCHEMA_PATH = ROOT / ".egg" / "schemas" / "contract.schema.json"


def _issue() -> IssueInfo:
    return IssueInfo(
        number=3033,
        title="x",
        url="https://github.com/o/r/issues/3033",
    )


class TestTaskDescriptionDefault:
    def test_none_by_default(self) -> None:
        contract = Contract(issue=_issue())
        assert contract.task_description is None

    def test_round_trip_preserves_full_text(self) -> None:
        """A multi-section task survives dump/reload untruncated."""
        body = (
            "Big refactor.\n\n## Goals\n"
            + ("Drop the legacy session cookie path. " * 30)
            + "\n\n## Non-goals\nJWT validation (already done)."
        )
        original = Contract(issue=_issue(), task_description=body)
        reloaded = Contract.model_validate(original.model_dump())
        assert reloaded.task_description == body
        assert "## Non-goals" in (reloaded.task_description or "")


class TestContractJsonSchemaTaskDescription:
    """``.egg/schemas/contract.schema.json`` is the external-consumer view of
    the contract; the top-level object sets ``additionalProperties: false``
    so an undeclared field would be rejected. #3033 added
    ``task_description`` to the Pydantic model — the JSON schema must keep up.
    """

    def test_schema_declares_task_description_optional_string(self) -> None:
        assert SCHEMA_PATH.exists()
        schema = json.loads(SCHEMA_PATH.read_text())

        props = schema.get("properties") or {}
        assert "task_description" in props, (
            "`task_description` must be declared at the top level of "
            "contract.schema.json. The schema sets "
            "`additionalProperties: false`, so an external consumer "
            "validating a contract with this field would reject it "
            "until the schema is bumped alongside the Pydantic model."
        )

        # Optional — not in `required`.
        required = schema.get("required", [])
        assert "task_description" not in required

        # Must accept both string and null (issue-driven contracts have None).
        td_schema = props["task_description"]
        serialised = json.dumps(td_schema)
        assert "string" in serialised
        assert "null" in serialised, (
            "`task_description` must accept null — issue-driven pipelines leave it unset."
        )

    def test_schema_default_schemaversion_tracks_model(self) -> None:
        """A hand-built contract without an explicit ``schemaVersion`` must
        come up at the same default as a fresh ``Contract()``. If the
        schema-level default drifts behind the model, downstream tooling
        sees a stale version that bypasses the after-mode stamp migrators.
        """
        schema = json.loads(SCHEMA_PATH.read_text())
        schema_default = (schema.get("properties") or {}).get("schemaVersion", {}).get("default")
        model_default = Contract.model_fields["schemaVersion"].default
        assert schema_default == model_default, (
            f"contract.schema.json `schemaVersion.default` is "
            f"{schema_default!r} but the Pydantic model defaults to "
            f"{model_default!r}. Bump the schema default in lockstep "
            f"with the model default (#3033 went 1.2 → 1.3)."
        )
