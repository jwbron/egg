"""Tests for ``PRMetadata`` post-slice-2 of #2777-replan.

Slice-2 hard-removes three of the four legacy ``pr.context_*`` fields
(``context_branch``, ``context_title``, ``context_description``) added in
#2548. The one remaining field — ``context_pr_number`` — stays because
the orchestrator still records the GitHub PR number for the
``egg/<id>/work → main`` context PR. The schema bumps ``1.1 → 1.2`` and
a load-time migration drops the three removed keys from on-disk legacy
contracts so the loader does not refuse to deserialise them.

This file covers:

* The kept fields (``context_pr_number``, ``deferred_actions``) still
  round-trip cleanly.
* ``PRMetadata`` rejects the three removed keys at construction
  (``extra='forbid'``) — surfacing planner-prompt regressions loudly.
* The ``1.1 → 1.2`` migration silently drops the three removed keys on
  load (preserving on-disk fixtures), preserves the kept fields, and
  promotes ``schemaVersion`` to ``"1.2"``.
* The ``schemaVersion`` default for brand-new contracts is ``"1.2"``.
* Cross-codebase grep regression: no production module imports the
  three deleted attribute names anywhere outside test scaffolding.

The tests live at ``tests/shared/egg_contracts/`` (the canonical test
root per ``[tool.pytest.ini_options].testpaths``) rather than at the
in-package ``shared/egg_contracts/tests/`` path so ``make test`` /
``make test-all`` discover them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from egg_contracts.models import (
    Contract,
    DeferredAction,
    IssueInfo,
    PRMetadata,
)
from pydantic import ValidationError

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _minimal_contract_payload(*, schema_version: str = "1.1") -> dict[str, Any]:
    """Return a minimal contract payload at the requested schema version.

    The contract has an ``IssueInfo`` and a single ``PRMetadata`` with
    the legacy required field (``title``) populated. Used to drive the
    1.1 → 1.2 migration round-trips below.
    """
    return {
        "schemaVersion": schema_version,
        "issue": {
            "number": 2777,
            "title": "sliced implementation phase cleanup",
            "url": "https://example.com/i/2777",
        },
        "current_phase": "refine",
        "slices": [],
        "decisions": [],
        "audit_log": [],
        "pr": {
            "title": "Cleanup: drop context-PR scaffold + PR phase",
            "description": "",
            "test_plan": "",
            "manual_steps": "",
            "deferred_actions": [],
        },
    }


# ---------------------------------------------------------------------------
# Kept fields — ``context_pr_number`` and ``deferred_actions`` still work
# ---------------------------------------------------------------------------


class TestPRMetadataKeptFields:
    """The kept fields (``context_pr_number``, ``deferred_actions``)
    still round-trip after the slice-2 deletions."""

    def test_context_pr_number_default_is_none(self):
        """Construction without ``context_pr_number`` leaves it ``None``.

        The orchestrator stamps the PR number on the contract after
        opening the context PR; before then it must be ``None`` (the
        sentinel the renderer keys off of).
        """
        pr = PRMetadata(title="t")
        assert pr.context_pr_number is None

    def test_context_pr_number_round_trip(self):
        """A populated ``context_pr_number`` round-trips via ``model_dump`` + JSON."""
        pr = PRMetadata(title="t", context_pr_number=4242)
        dumped = pr.model_dump()
        assert dumped["context_pr_number"] == 4242

        # JSON round-trip — the on-disk path.
        as_json = pr.model_dump_json()
        decoded = json.loads(as_json)
        assert decoded["context_pr_number"] == 4242

        round_trip = PRMetadata.model_validate_json(as_json)
        assert round_trip.context_pr_number == 4242

    def test_deferred_actions_default_is_empty_list(self):
        """``deferred_actions`` defaults to ``[]`` — kept by slice-2."""
        pr = PRMetadata(title="t")
        assert pr.deferred_actions == []

    def test_deferred_actions_round_trip(self):
        """Populated ``deferred_actions`` round-trips."""
        pr = PRMetadata(
            title="t",
            deferred_actions=[
                DeferredAction(
                    reviewer="reviewer_code",
                    condition="must rename foo → bar before merge",
                )
            ],
        )
        as_json = pr.model_dump_json()
        decoded = json.loads(as_json)
        assert len(decoded["deferred_actions"]) == 1
        assert decoded["deferred_actions"][0]["reviewer"] == "reviewer_code"
        assert decoded["deferred_actions"][0]["condition"] == "must rename foo → bar before merge"

        round_trip = PRMetadata.model_validate_json(as_json)
        assert len(round_trip.deferred_actions) == 1
        assert round_trip.deferred_actions[0].reviewer == "reviewer_code"


class TestPRMetadataContextPRNumberValidator:
    """``context_pr_number`` must only accept positive integers (``ge=1``).

    Inherited from #2548; pinned post-slice-2 so the validator does not
    regress to accept ``0`` or negatives during the schema cleanup.
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

    def test_context_pr_number_accepts_large_int(self):
        """A high GitHub PR number must round-trip — no ``int32`` cap."""
        pr = PRMetadata(title="t", context_pr_number=10_000_000)
        assert pr.context_pr_number == 10_000_000
        round_trip = PRMetadata.model_validate(pr.model_dump())
        assert round_trip.context_pr_number == 10_000_000


# ---------------------------------------------------------------------------
# Removed fields — PRMetadata rejects the three deleted keys
# ---------------------------------------------------------------------------


class TestPRMetadataRemovedFieldsRejected:
    """The three deleted ``context_*`` fields must not be accepted at
    direct construction.

    Slice-2 deletes ``context_branch`` / ``context_title`` /
    ``context_description`` from the model. With ``extra='forbid'``
    pydantic raises ``ValidationError`` when a caller (planner code, a
    test fixture, a hand-edited contract) passes any of them directly
    to ``PRMetadata(...)``. The migration shim on ``Contract`` strips
    the keys from legacy on-disk payloads BEFORE they reach
    ``PRMetadata`` (covered in :class:`TestPRMetadataSchemaVersionMigration`
    below) — so direct construction is the path these tests exercise.

    Without ``extra='forbid'`` pydantic's default ``extra='ignore'``
    would silently swallow the unknown keys, masking planner-prompt
    regressions that re-introduce the deleted vocabulary.
    """

    @pytest.mark.parametrize(
        "removed_field",
        [
            "context_branch",
            "context_title",
            "context_description",
        ],
    )
    def test_removed_field_rejected_at_construction(self, removed_field):
        """Passing any of the three deleted keys to ``PRMetadata(...)`` must raise.

        This is the slice-2 acceptance criterion: ``PRMetadata`` no
        longer accepts the deleted field names. We probe each key
        independently so the failure message points at the regression
        precisely.
        """
        kwargs: dict[str, Any] = {"title": "t"}
        kwargs[removed_field] = "stray-value"
        with pytest.raises(ValidationError) as excinfo:
            PRMetadata(**kwargs)
        # The error must name the offending key so a regressed
        # planner-prompt path is easy to debug.
        assert removed_field in str(excinfo.value), (
            f"ValidationError should name the rejected key {removed_field!r}; "
            f"got: {excinfo.value!s}"
        )

    def test_all_three_removed_fields_rejected_together(self):
        """Passing all three keys simultaneously must also raise.

        Adversarial: a regression that ignored extras one-at-a-time
        (e.g. a sloppy ``__init__`` override that popped the first
        unknown key) could mask the second and third.
        """
        with pytest.raises(ValidationError):
            PRMetadata(
                title="t",
                context_branch="egg/issue-2548/context",
                context_title="Strategic plan",
                context_description="Refine + plan artifacts.",
            )

    def test_pr_metadata_has_no_removed_field_attributes(self):
        """The model class must not expose attributes for the deleted fields.

        Defence-in-depth: even if ``extra='forbid'`` regresses, the
        attribute-level check here catches the case where the field
        definition itself sneaks back in (e.g. via a rebase that
        resurrects the old declaration).
        """
        field_names = set(PRMetadata.model_fields.keys())
        assert "context_branch" not in field_names, (
            f"PRMetadata.model_fields must not contain 'context_branch'; got: {sorted(field_names)}"
        )
        assert "context_title" not in field_names
        assert "context_description" not in field_names

    def test_removed_field_attribute_access_raises(self):
        """Reading the deleted attributes off a valid ``PRMetadata`` raises.

        A pre-slice-2 caller doing ``pr.context_branch`` should now hit
        ``AttributeError`` so the regression is immediately visible at
        the call site instead of returning a silent ``None``.
        """
        pr = PRMetadata(title="t")
        for attr in ("context_branch", "context_title", "context_description"):
            with pytest.raises(AttributeError):
                getattr(pr, attr)


# ---------------------------------------------------------------------------
# Schema 1.1 → 1.2 migration: drop the three removed fields on load
# ---------------------------------------------------------------------------


class TestPRMetadataSchemaVersionMigration:
    """The ``1.1 → 1.2`` migration must silently drop the three removed
    keys from legacy contracts and promote ``schemaVersion``.

    The plan (TASK-2-4): "The migration entry must (a) drop the three
    fields when present on load, (b) preserve ``context_pr_number`` and
    ``deferred_actions``, (c) leave fresh-v1.2 contracts untouched."
    """

    def test_default_schemaversion_is_1_2(self):
        """Brand-new ``Contract`` defaults ``schemaVersion`` to ``"1.2"``."""
        contract = Contract(
            issue=IssueInfo(
                number=2777,
                title="t",
                url="https://github.com/o/r/issues/2777",
            )
        )
        assert contract.schemaVersion == "1.2"

    def test_legacy_1_1_payload_promotes_to_1_2(self):
        """Loading a ``1.1`` payload must bump ``schemaVersion`` to ``"1.2"``."""
        payload = _minimal_contract_payload(schema_version="1.1")
        contract = Contract.model_validate(payload)
        assert contract.schemaVersion == "1.2"

    def test_legacy_1_1_payload_with_removed_fields_loads_cleanly(self):
        """A ``1.1`` payload carrying any of the three deleted keys loads
        without error — the migration strips them before validation.

        This is the in-flight-fixtures path: on-disk contracts written
        before slice-2 (e.g. ``issue-2548.json``) carry the old keys,
        and the loader must tolerate them rather than refuse to load.
        """
        payload = _minimal_contract_payload(schema_version="1.1")
        payload["pr"]["context_title"] = "Strategic plan for #2548"
        payload["pr"]["context_description"] = "Refine + plan artifacts."
        payload["pr"]["context_branch"] = "egg/issue-2548/context"
        payload["pr"]["context_pr_number"] = 4242

        contract = Contract.model_validate(payload)

        # schemaVersion was promoted.
        assert contract.schemaVersion == "1.2"
        # The kept field survives.
        assert contract.pr is not None
        assert contract.pr.context_pr_number == 4242
        # The three removed fields are no longer accessible on the model.
        for attr in ("context_branch", "context_title", "context_description"):
            with pytest.raises(AttributeError):
                getattr(contract.pr, attr)

    def test_legacy_1_0_payload_with_removed_fields_loads_cleanly(self):
        """A ``1.0`` payload (pre-#2548) carrying the keys also loads.

        Adversarial: the 1.0 → 1.1 migration was additive; the
        1.1 → 1.2 migration is reductive. Both must compose so a 1.0
        fixture lands cleanly at 1.2 with the three removed keys
        stripped.
        """
        payload = _minimal_contract_payload(schema_version="1.0")
        payload["pr"]["context_title"] = "Strategic plan for #2548"
        payload["pr"]["context_branch"] = "egg/issue-2548/context"

        contract = Contract.model_validate(payload)

        # Composed migrations land at the post-slice-2 version.
        assert contract.schemaVersion == "1.2"
        # No leftover attributes on the model.
        assert contract.pr is not None
        for attr in ("context_branch", "context_title", "context_description"):
            with pytest.raises(AttributeError):
                getattr(contract.pr, attr)

    def test_migration_preserves_context_pr_number_and_deferred_actions(self):
        """The migration must NOT drop ``context_pr_number`` or
        ``deferred_actions`` when stripping the three removed keys.

        Adversarial: a too-eager migration that rebuilt the ``pr``
        dict from scratch could lose the kept fields. Pin them
        explicitly.
        """
        payload = _minimal_contract_payload(schema_version="1.1")
        payload["pr"]["context_branch"] = "egg/issue-2548/context"  # removed
        payload["pr"]["context_title"] = "Strategic plan"  # removed
        payload["pr"]["context_description"] = "..."  # removed
        payload["pr"]["context_pr_number"] = 4242  # KEPT
        payload["pr"]["deferred_actions"] = [
            {
                "reviewer": "reviewer_code",
                "condition": "must rename foo → bar before merge",
                "resolved_in_diff": "",
            }
        ]  # KEPT

        contract = Contract.model_validate(payload)

        assert contract.pr is not None
        # Kept fields survived.
        assert contract.pr.context_pr_number == 4242
        assert len(contract.pr.deferred_actions) == 1
        assert contract.pr.deferred_actions[0].reviewer == "reviewer_code"
        # Legacy non-context fields also survive.
        assert contract.pr.title == "Cleanup: drop context-PR scaffold + PR phase"

    def test_legacy_1_1_round_trip_persists_at_1_2(self):
        """After 1.1 → 1.2 promotion, dump→reload must stay at 1.2.

        Adversarial: a faulty migration on the *input* path could
        re-trigger on the second load and silently re-bump or
        downgrade. Pin idempotency across multiple round-trips.
        """
        payload = _minimal_contract_payload(schema_version="1.1")
        first = Contract.model_validate(payload)
        assert first.schemaVersion == "1.2"

        dumped = first.model_dump()
        assert dumped["schemaVersion"] == "1.2"

        second = Contract.model_validate(dumped)
        assert second.schemaVersion == "1.2"

        third = Contract.model_validate(second.model_dump())
        assert third.schemaVersion == "1.2"

    def test_fresh_1_2_payload_loads_unchanged(self):
        """A fresh ``1.2`` payload (with no removed keys) must be a no-op.

        The migration is conditional on ``schemaVersion == "1.1"`` (per
        the existing migration-shim pattern in the codebase). A future
        ``2.0`` payload must not get silently downgraded to ``1.2``.
        """
        payload = _minimal_contract_payload(schema_version="1.2")
        contract = Contract.model_validate(payload)
        assert contract.schemaVersion == "1.2"
        assert contract.pr is not None
        # No spurious deleted-field attributes appear on the model.
        for attr in ("context_branch", "context_title", "context_description"):
            with pytest.raises(AttributeError):
                getattr(contract.pr, attr)

    def test_unrecognized_schemaversion_not_silently_downgraded(self):
        """A ``schemaVersion`` outside the migration set must NOT be rewritten.

        Adversarial: the migration shim must be selective. A future
        ``2.0`` loading on the post-slice-2 binary should keep its
        declared version, not get silently downgraded to ``1.2``.
        """
        payload = _minimal_contract_payload(schema_version="2.0")
        contract = Contract.model_validate(payload)
        assert contract.schemaVersion == "2.0"

    def test_invalid_schemaversion_format_rejected(self):
        """``schemaVersion`` must match the ``M.N`` regex — freeform
        strings like ``"1.2-rc1"`` or ``"v1.2"`` must raise.
        """
        payload = _minimal_contract_payload(schema_version="1.2-rc1")
        with pytest.raises(ValidationError):
            Contract.model_validate(payload)

        payload = _minimal_contract_payload(schema_version="v1.2")
        with pytest.raises(ValidationError):
            Contract.model_validate(payload)

    def test_combined_phases_and_schemaversion_migration_through_1_2(self):
        """A legacy contract with both ``phases:`` (pre-#2137) AND
        ``schemaVersion=1.0`` (pre-#2548) AND the three removed keys
        (pre-slice-2) must be migrated correctly by every validator.

        Adversarial probe: three migrations live on the same model.
        Pin the combined invariant — legacy keys re-map, the
        schemaVersion lands at the post-slice-2 value, and the three
        removed keys are stripped.
        """
        payload = _minimal_contract_payload(schema_version="1.0")
        del payload["slices"]
        payload["phases"] = [
            {"id": "phase-1", "name": "first", "tasks": []},
            {
                "id": "phase-2",
                "name": "second",
                "tasks": [],
                "dependencies": ["phase-1"],
            },
        ]
        payload["pr"]["context_branch"] = "egg/issue-2548/context"
        payload["pr"]["context_title"] = "Strategic plan"

        contract = Contract.model_validate(payload)

        assert contract.schemaVersion == "1.2"
        assert len(contract.slices) == 2
        assert contract.slices[0].id == "slice-1"
        assert contract.slices[1].id == "slice-2"
        assert contract.slices[1].dependencies == ["slice-1"]
        assert contract.pr is not None
        for attr in ("context_branch", "context_title", "context_description"):
            with pytest.raises(AttributeError):
                getattr(contract.pr, attr)


# ---------------------------------------------------------------------------
# Cross-codebase grep: no production module reads the three deleted attrs
# ---------------------------------------------------------------------------


class TestNoSurvivingReadSites:
    """No production module may import or read the three deleted
    attribute names.

    The plan (TASK-2-10 AC-3): "Any test in ``tests/`` or
    ``orchestrator/tests/`` that imports ``context_branch`` /
    ``context_title`` / ``context_description`` from ``PRMetadata`` —
    grep ``tests/ orchestrator/tests/ integration_tests/`` before
    completing to catch stragglers." We grep the broader set of
    production paths too because a stray read at runtime is a
    ``AttributeError`` regression.

    Excludes:
      * ``.egg-state/`` (legacy on-disk contracts; covered by the
        migration shim).
      * ``brc-history/`` (frozen historical transcripts).
      * This file's own assertion strings (the regex carves itself
        out).
      * Test files whose entire purpose is to assert the fields are
        absent (allow-list).
    """

    # Paths that are allowed to mention the three removed names (their
    # purpose is to verify the removal).
    ALLOWED_PATHS = (
        # The model file itself may carry a removal note in a comment.
        "shared/egg_contracts/models.py",
        # This file — the assertion strings reference the deleted names.
        "tests/shared/egg_contracts/test_pr_metadata.py",
        # The doc-terminology regression test asserts the docs do NOT
        # mention the deleted fields (regression-by-grep).
        "tests/docs/test_context_pr_doc_terminology.py",
        # The migration shim itself names the keys it drops.
        # (Path may shift; matched as a substring.)
    )

    @pytest.mark.parametrize("needle", ["context_branch", "context_title", "context_description"])
    def test_no_production_reads_of_removed_fields(self, needle):
        """No production code (outside the allow-list) may reference the
        three deleted attribute names.

        Uses ``git grep`` rather than recursive ``rg`` so the search
        respects ``.gitignore`` and skips generated trees.
        """
        try:
            result = subprocess.run(
                [
                    "git",
                    "grep",
                    "-l",
                    needle,
                    "--",
                    "orchestrator/",
                    "shared/",
                    "gateway/",
                    "integration_tests/",
                    "tests/",
                ],
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git not available in test environment")
        # git grep exits non-zero when there are no matches; treat that
        # as a pass.
        if result.returncode != 0 and not result.stdout:
            return
        offending: list[str] = []
        for line in result.stdout.splitlines():
            path = line.strip()
            if not path:
                continue
            if any(allowed in path for allowed in self.ALLOWED_PATHS):
                continue
            # The legacy migration shim is allowed to name the keys.
            if "migration" in path.lower() or "_migrate" in path.lower():
                continue
            offending.append(path)
        assert not offending, (
            f"Found surviving references to deleted PRMetadata field "
            f"{needle!r} in production code; slice-2 TASK-2-10 AC-3 "
            f"requires zero hits outside the allow-list. Offending "
            f"files:\n  " + "\n  ".join(offending)
        )
