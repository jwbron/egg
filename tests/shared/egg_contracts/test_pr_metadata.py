"""Tests for PRMetadata.context_* fields + schemaVersion 1.0→1.1 migration.

Added in #2548 (slice-1, task-1-2). Covers the four new optional
``PRMetadata.context_*`` fields the planner emits for the doc-only
context PR (issue #2548) and the load-time migration shim that
back-fills the new fields as ``None`` when an on-disk contract
written with ``schemaVersion="1.0"`` is loaded into the post-rename
``schemaVersion="1.1"`` model.

The acceptance criteria from the plan:

* Round-trip a ``PRMetadata`` with all four context fields populated.
* Round-trip a ``PRMetadata`` with all four context fields omitted
  (defaults must be ``None``).
* Round-trip a contract serialised with ``schemaVersion="1.0"`` and
  no context fields, and confirm migration populates the defaults.
* Confirm ``context_pr_number`` validation: ``0`` and negative values
  are rejected (``ge=1``); positive ``int`` values round-trip.

The tests live at ``tests/shared/egg_contracts/`` because that is the
pytest collection root in the project's ``[tool.pytest.ini_options]
testpaths`` (the in-package path ``shared/egg_contracts/tests/`` is NOT
in ``testpaths`` / ``scripts/select_tests/_constants.TEST_ROOT_DIRS``
and would not be discovered by ``make test`` or ``make test-all``).
The plan task-1-2 ``files_affected`` referenced the in-package path
but the canonical location of every other ``PRMetadata`` test
(``tests/shared/egg_contracts/test_models.py``) is here.
"""

from __future__ import annotations

from typing import Any

import pytest
from egg_contracts.models import (
    Contract,
    IssueInfo,
    PRMetadata,
)
from pydantic import ValidationError


def _minimal_contract_payload(*, schema_version: str = "1.0") -> dict[str, Any]:
    """Return a minimal contract payload at the requested schema version.

    The contract has an ``IssueInfo`` and a single ``PRMetadata`` with
    the legacy required field (``title``) populated and no ``context_*``
    keys set. Used to drive the migration round-trip in
    :func:`test_contract_schemaversion_1_0_loads_with_context_defaults_none`.

    The return type is ``dict[str, Any]`` rather than the more precise
    ``dict[str, dict[str, str | list[Any]]]`` because callers extend
    ``payload["pr"]`` with arbitrary new keys (``context_title``,
    ``context_pr_number``, ``deferred_actions`` entries) — pinning a
    narrower inner type only forces casts at every mutation site.
    """
    return {
        "schemaVersion": schema_version,
        "issue": {
            "number": 2548,
            "title": "context PR + per-slice BRC history",
            "url": "https://example.com/i/2548",
        },
        "current_phase": "refine",
        "slices": [],
        "decisions": [],
        "audit_log": [],
        "pr": {
            "title": "Add context PR + per-slice BRC history",
            "description": "",
            "test_plan": "",
            "manual_steps": "",
            "deferred_actions": [],
        },
    }


class TestPRMetadataContextFields:
    """The four new optional ``context_*`` fields on ``PRMetadata`` (#2548)."""

    def test_context_fields_default_to_none(self):
        """Constructing without the new keys must leave them ``None``.

        Backwards-compat: a planner emitting only the legacy fields
        (``title`` / ``description`` / ``test_plan`` / ``manual_steps``)
        must produce a ``PRMetadata`` whose ``context_*`` fields are all
        ``None`` — that is what allows ``contract.pr.context_branch or
        pipeline_branch`` to fall back cleanly in slice-4.
        """
        pr = PRMetadata(title="Add context PR + per-slice BRC history")
        assert pr.context_title is None
        assert pr.context_description is None
        assert pr.context_branch is None
        assert pr.context_pr_number is None

    def test_context_fields_populated_round_trip(self):
        """All four ``context_*`` fields populated must round-trip via JSON.

        Asserts construction → ``model_dump()`` → ``model_validate()``
        is value-preserving for every field the orchestrator persists
        (``context_branch`` and ``context_pr_number``) and every field
        the planner emits (``context_title`` and ``context_description``).
        """
        pr = PRMetadata(
            title="Add context PR + per-slice BRC history",
            description="Per-slice BRC history + context PR work.",
            context_title="Strategic plan for #2548",
            context_description="Refine + plan artifacts for issue 2548.",
            context_branch="egg/issue-2548/context",
            context_pr_number=4242,
        )
        dumped = pr.model_dump()
        assert dumped["context_title"] == "Strategic plan for #2548"
        assert dumped["context_description"] == "Refine + plan artifacts for issue 2548."
        assert dumped["context_branch"] == "egg/issue-2548/context"
        assert dumped["context_pr_number"] == 4242

        round_trip = PRMetadata.model_validate(dumped)
        assert round_trip.context_title == "Strategic plan for #2548"
        assert round_trip.context_description == "Refine + plan artifacts for issue 2548."
        assert round_trip.context_branch == "egg/issue-2548/context"
        assert round_trip.context_pr_number == 4242

    def test_context_fields_omitted_round_trip(self):
        """Omitting the new keys at construction must round-trip as ``None``.

        Mirror of ``test_context_fields_default_to_none`` but at the
        JSON-round-trip boundary — confirms ``model_dump()`` does not
        synthesise spurious values and ``model_validate()`` accepts the
        dump as-is.
        """
        pr = PRMetadata(title="Plain PR — no context fields")
        dumped = pr.model_dump()
        assert dumped["context_title"] is None
        assert dumped["context_description"] is None
        assert dumped["context_branch"] is None
        assert dumped["context_pr_number"] is None

        round_trip = PRMetadata.model_validate(dumped)
        assert round_trip.context_title is None
        assert round_trip.context_description is None
        assert round_trip.context_branch is None
        assert round_trip.context_pr_number is None


class TestPRMetadataContextPRNumberValidator:
    """``context_pr_number`` must only accept positive integers (``ge=1``).

    Mirrors the validation already on ``IssueInfo.number`` — a GitHub PR
    number is always a positive integer; ``0`` and negatives indicate a
    bug somewhere upstream and should be surfaced loudly.
    """

    def test_positive_pr_number_accepted(self):
        pr = PRMetadata(title="t", context_pr_number=1)
        assert pr.context_pr_number == 1
        pr = PRMetadata(title="t", context_pr_number=999_999)
        assert pr.context_pr_number == 999_999

    def test_zero_pr_number_rejected(self):
        with pytest.raises(ValidationError):
            PRMetadata(title="t", context_pr_number=0)

    def test_negative_pr_number_rejected(self):
        with pytest.raises(ValidationError):
            PRMetadata(title="t", context_pr_number=-1)

    def test_none_pr_number_accepted(self):
        """``None`` is the sentinel for 'not yet opened' and must remain valid."""
        pr = PRMetadata(title="t", context_pr_number=None)
        assert pr.context_pr_number is None

    def test_pr_number_validator_re_runs_on_assignment(self):
        """``validate_assignment=True`` makes ``setattr`` re-run validation.

        Regression for the shared ``EggContractBaseModel`` config (#2490).
        Setting ``context_pr_number`` to ``0`` after construction must
        raise — without this guard a buggy orchestrator path could
        smuggle a 0 onto a previously-valid PRMetadata.
        """
        pr = PRMetadata(title="t", context_pr_number=10)
        with pytest.raises(ValidationError):
            pr.context_pr_number = 0
        # Original value unchanged after the failed assignment.
        assert pr.context_pr_number == 10


class TestPRMetadataSchemaVersionMigration:
    """A ``schemaVersion=1.0`` contract must load cleanly into the 1.1 model.

    The migration shim is on ``Contract`` (model-level), not on
    ``PRMetadata`` directly, but the observable behavior we lock down
    here is at the ``Contract.pr.context_*`` level: a pre-#2548 contract
    on disk has no ``context_*`` keys; loading it into the post-#2548
    model must:

    * succeed (no ``ValidationError``),
    * leave ``context_title`` / ``context_description`` / ``context_branch``
      / ``context_pr_number`` defaulted to ``None``,
    * produce a contract whose ``schemaVersion`` is the post-migration
      string (``"1.1"`` per the plan).
    """

    def test_legacy_1_0_payload_loads_without_context_keys(self):
        """A 1.0 payload missing the four keys parses and defaults to ``None``."""
        payload = _minimal_contract_payload(schema_version="1.0")
        contract = Contract.model_validate(payload)

        assert contract.pr is not None
        assert contract.pr.context_title is None
        assert contract.pr.context_description is None
        assert contract.pr.context_branch is None
        assert contract.pr.context_pr_number is None

    def test_legacy_1_0_payload_round_trip_preserves_defaults(self):
        """Load → dump → reload must not synthesise spurious context values."""
        payload = _minimal_contract_payload(schema_version="1.0")
        first = Contract.model_validate(payload)
        dumped = first.model_dump()
        second = Contract.model_validate(dumped)

        assert second.pr is not None
        assert second.pr.context_title is None
        assert second.pr.context_description is None
        assert second.pr.context_branch is None
        assert second.pr.context_pr_number is None

    def test_default_schemaversion_is_1_1(self):
        """Brand-new ``Contract`` defaults the schemaVersion to ``1.1``.

        The plan bumps the default from ``"1.0"`` to ``"1.1"``. This
        test pins that default so a future revert is caught loudly.
        """
        contract = Contract(
            issue=IssueInfo(
                number=1,
                title="t",
                url="https://github.com/o/r/issues/1",
            )
        )
        assert contract.schemaVersion == "1.1"

    def test_explicit_1_1_payload_loads_with_context_fields(self):
        """A 1.1 payload with all context fields populated round-trips."""
        payload = _minimal_contract_payload(schema_version="1.1")
        payload["pr"]["context_title"] = "Strategic plan for #2548"
        payload["pr"]["context_description"] = "Refine + plan artifacts."
        payload["pr"]["context_branch"] = "egg/issue-2548/context"
        payload["pr"]["context_pr_number"] = 4242
        contract = Contract.model_validate(payload)

        assert contract.pr is not None
        assert contract.pr.context_title == "Strategic plan for #2548"
        assert contract.pr.context_description == "Refine + plan artifacts."
        assert contract.pr.context_branch == "egg/issue-2548/context"
        assert contract.pr.context_pr_number == 4242

    def test_legacy_1_0_payload_does_not_lose_legacy_pr_fields(self):
        """Migration must not drop any legacy ``PRMetadata`` field on the way in.

        Adversarial regression: a too-eager migration that rebuilt
        ``PRMetadata`` from scratch could lose ``deferred_actions`` or
        ``manual_steps``. Pin the legacy fields explicitly.
        """
        payload = _minimal_contract_payload(schema_version="1.0")
        payload["pr"]["description"] = "legacy description"
        payload["pr"]["test_plan"] = "legacy test plan"
        payload["pr"]["manual_steps"] = "legacy manual steps"
        payload["pr"]["deferred_actions"] = [
            {
                "reviewer": "reviewer_code",
                "condition": "must rename foo → bar before merge",
                "resolved_in_diff": "",
            }
        ]
        contract = Contract.model_validate(payload)

        assert contract.pr is not None
        assert contract.pr.description == "legacy description"
        assert contract.pr.test_plan == "legacy test plan"
        assert contract.pr.manual_steps == "legacy manual steps"
        assert len(contract.pr.deferred_actions) == 1
        assert contract.pr.deferred_actions[0].condition == "must rename foo → bar before merge"

    def test_legacy_1_0_promotes_schemaversion_to_1_1(self):
        """Loading a 1.0 payload must promote the version to 1.1 on the loaded model.

        The plan calls for "promotion" semantics — pre-#2548 contracts
        on disk are bumped to 1.1 when loaded into the new model so
        downstream tooling sees a consistent value. This pins the
        bump direction.
        """
        payload = _minimal_contract_payload(schema_version="1.0")
        contract = Contract.model_validate(payload)
        assert contract.schemaVersion == "1.1"

    def test_legacy_1_0_round_trip_persists_at_1_1(self):
        """After the 1.0→1.1 promotion, dump→reload must keep the version at 1.1.

        Adversarial: a faulty migration that lived on the *input* path
        (e.g. wrap-mode mutation of incoming dict) could re-trigger on
        the second load and silently re-bump or downgrade. The
        canonical post-migration version must be stable across an
        arbitrary number of round-trips.
        """
        payload = _minimal_contract_payload(schema_version="1.0")
        first = Contract.model_validate(payload)
        assert first.schemaVersion == "1.1"

        dumped = first.model_dump()
        assert dumped["schemaVersion"] == "1.1"

        second = Contract.model_validate(dumped)
        assert second.schemaVersion == "1.1"

        # Third round-trip — really pin idempotency.
        third = Contract.model_validate(second.model_dump())
        assert third.schemaVersion == "1.1"

    def test_unrecognized_schemaversion_not_silently_downgraded(self):
        """A schemaVersion outside the migration set must NOT be rewritten.

        Adversarial: the migration shim must be selective. A future
        ``2.0`` (or even an in-between ``1.2``) loading on an old
        binary should keep its declared version, not get silently
        downgraded to ``1.1``. The plan explicitly calls this out:
        "We deliberately do NOT touch versions outside ``{1.0}``".
        """
        payload = _minimal_contract_payload(schema_version="1.2")
        contract = Contract.model_validate(payload)
        assert contract.schemaVersion == "1.2"

        payload_v2 = _minimal_contract_payload(schema_version="2.0")
        contract_v2 = Contract.model_validate(payload_v2)
        assert contract_v2.schemaVersion == "2.0"


class TestPRMetadataContextEmptyStringSemantics:
    """Empty / whitespace strings are accepted at the model layer.

    The orchestrator hook computes ``contract.pr.context_title or
    contract.pr.title`` to pick the framing for the context PR — both
    ``None`` and ``""`` fall back via Python truthiness, so the model
    deliberately does NOT enforce a min_length on the context-string
    fields. These tests pin that behavior so a future ``min_length=1``
    addition is caught by the test suite (it would break the ingestion
    path, where the planner can legitimately emit an empty
    ``context_description: ""`` block scalar).
    """

    def test_empty_context_title_accepted(self):
        pr = PRMetadata(title="t", context_title="")
        assert pr.context_title == ""

    def test_empty_context_description_accepted(self):
        pr = PRMetadata(title="t", context_description="")
        assert pr.context_description == ""

    def test_empty_context_branch_accepted(self):
        # ``context_branch`` carries a git ref name; an empty string is
        # not a valid ref, but the model layer is permissive — the
        # orchestrator gateway primitive validates the ref shape when
        # it actually creates the branch (slice-3).
        pr = PRMetadata(title="t", context_branch="")
        assert pr.context_branch == ""

    def test_or_fallback_works_with_none_and_empty(self):
        """Mirror of the orchestrator hook's runtime fallback expression.

        ``context_title or title`` must yield ``title`` for both ``None``
        and ``""``. If a future commit tightens the model to reject
        ``""`` this test fails loudly because the orchestrator's
        fallback semantics depend on this dual treatment.
        """
        pr_none = PRMetadata(title="fallback-title")
        assert (pr_none.context_title or pr_none.title) == "fallback-title"

        pr_empty = PRMetadata(title="fallback-title", context_title="")
        assert (pr_empty.context_title or pr_empty.title) == "fallback-title"

        pr_set = PRMetadata(title="fallback-title", context_title="explicit-context")
        assert (pr_set.context_title or pr_set.title) == "explicit-context"


class TestPlanParserContextFieldExtraction:
    """End-to-end tests for the planner-emitted ``pr.context_*`` keys.

    Task-1-3's acceptance criteria require that planner-emitted
    YAML containing ``context_title:`` and ``context_description:`` is
    parsed without error and the values land on ``contract.pr.context_*``;
    omitting the keys leaves them as ``None``. Live in this file
    because they exercise the same surface (``PRMetadata.context_*``)
    that task-1-2 owns; without these tests a regression in
    ``extract_pr_context_metadata_from_yaml`` could silently drop
    planner-emitted keys without breaking the model-level tests above.
    """

    @staticmethod
    def _make_yaml(
        *,
        with_context_title: bool = False,
        with_context_description: bool = False,
        title_value: str = "Strategic plan for #2548",
        description_value: str = "Refine + plan artifacts.",
    ) -> dict[str, Any]:
        """Build a yaml-tasks dict, optionally with the new context keys."""
        pr_block: dict[str, str] = {
            "title": "Implement #2548",
            "description": "Slice-1 stub.",
        }
        if with_context_title:
            pr_block["context_title"] = title_value
        if with_context_description:
            pr_block["context_description"] = description_value
        return {"pr": pr_block, "phases": []}

    def test_extract_returns_none_pair_when_pr_block_missing(self):
        from egg_contracts.plan_parser import extract_pr_context_metadata_from_yaml

        title, desc, warnings = extract_pr_context_metadata_from_yaml({"phases": []})
        assert title is None
        assert desc is None
        assert warnings == []

    def test_extract_returns_none_pair_when_yaml_data_is_none(self):
        from egg_contracts.plan_parser import extract_pr_context_metadata_from_yaml

        title, desc, warnings = extract_pr_context_metadata_from_yaml(None)
        assert title is None
        assert desc is None
        assert warnings == []

    def test_extract_returns_none_pair_when_keys_absent(self):
        from egg_contracts.plan_parser import extract_pr_context_metadata_from_yaml

        yaml_data = self._make_yaml()  # neither key present
        title, desc, warnings = extract_pr_context_metadata_from_yaml(yaml_data)
        assert title is None
        assert desc is None
        assert warnings == []

    def test_extract_returns_populated_when_keys_present(self):
        from egg_contracts.plan_parser import extract_pr_context_metadata_from_yaml

        yaml_data = self._make_yaml(
            with_context_title=True,
            with_context_description=True,
        )
        title, desc, warnings = extract_pr_context_metadata_from_yaml(yaml_data)
        assert title == "Strategic plan for #2548"
        assert desc == "Refine + plan artifacts."
        assert warnings == []

    def test_extract_normalises_whitespace_to_none(self):
        """A planner emitting whitespace-only block scalars must collapse to None.

        The orchestrator hook's ``contract.pr.context_title or
        contract.pr.title`` fallback works with both ``None`` and
        ``""``; collapsing whitespace to ``None`` here keeps the
        contract diff clean (no spurious whitespace strings) and
        matches the existing ``_normalize_optional_string`` behavior
        for legacy fields.
        """
        from egg_contracts.plan_parser import extract_pr_context_metadata_from_yaml

        yaml_data = self._make_yaml(
            with_context_title=True,
            with_context_description=True,
            title_value="   ",
            description_value="   ",
        )
        title, desc, warnings = extract_pr_context_metadata_from_yaml(yaml_data)
        assert title is None
        assert desc is None

    def test_extract_warns_on_non_string_context_title(self):
        """A non-string ``context_title`` must produce a ParseWarning.

        Mirror of the existing behavior on ``pr.title`` — surfacing the
        type mismatch makes planner-prompt regressions easy to spot.
        """
        from egg_contracts.plan_parser import extract_pr_context_metadata_from_yaml

        yaml_data = {
            "pr": {
                "title": "Implement #2548",
                "context_title": 12345,  # int — not a string
            },
            "phases": [],
        }
        title, _desc, warnings = extract_pr_context_metadata_from_yaml(yaml_data)
        assert title is None  # malformed → fall back
        assert len(warnings) == 1
        assert "context_title" in warnings[0].message
        assert "int" in warnings[0].message

    def test_parse_plan_threads_context_into_parse_result(self):
        """End-to-end: ``parse_plan`` must populate ``ParseResult.pr_context_*``."""
        from egg_contracts.plan_parser import parse_plan

        plan_md = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            '  title: "Implement #2548"\n'
            '  description: "Slice-1 stub."\n'
            '  context_title: "Strategic plan for #2548"\n'
            "  context_description: |\n"
            "    Refine + plan artifacts for issue 2548.\n"
            "phases:\n"
            "  - id: 1\n"
            "    name: slice-1\n"
            "    tasks: []\n"
            "```\n"
        )
        result = parse_plan(plan_md)
        assert result.success is True
        assert result.pr_context_title == "Strategic plan for #2548"
        assert result.pr_context_description == "Refine + plan artifacts for issue 2548."

    def test_parse_plan_defaults_context_to_none_when_keys_omitted(self):
        from egg_contracts.plan_parser import parse_plan

        plan_md = (
            "# Plan\n\n"
            "```yaml\n"
            "# yaml-tasks\n"
            "pr:\n"
            '  title: "Implement #2548"\n'
            '  description: "Slice-1 stub."\n'
            "phases:\n"
            "  - id: 1\n"
            "    name: slice-1\n"
            "    tasks: []\n"
            "```\n"
        )
        result = parse_plan(plan_md)
        assert result.success is True
        assert result.pr_context_title is None
        assert result.pr_context_description is None
