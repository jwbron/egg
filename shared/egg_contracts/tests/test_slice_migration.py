"""Schema-rename / migration tests for ``Phase`` → ``Slice`` (#2137).

These tests verify the load-time migration shim that translates legacy
``phases: [...]`` JSON into the canonical ``slices: [...]`` shape on
``Contract.from_dict`` / pydantic validation. Covered by TASK-1-4.

Concretely, they assert:

* A brand-new ``slices: [...]`` payload loads as a no-op (no migration
  side-effects).
* A legacy ``phases: [...]`` payload (no ``slices`` key) is migrated:
  ``Contract.slices`` populates, ``phase-N`` IDs become ``slice-N``,
  ``dependencies`` strings of the same shape are rewritten, and the
  original payload is stashed on the private ``_legacy_phases``
  attribute so audit tooling can link back during the transition.
* Both keys present at once is *not* the migration-trigger path
  (``slices`` wins).
* Round-trip dump → reload of a migrated contract is a no-op on the
  second load (``_legacy_phases`` stays ``None``) — the canonical
  output only emits ``slices``.
* Real legacy contract fixtures from ``.egg-state/contracts/*.json``
  load through the migration without raising. (Tested generically
  on a small synthetic fixture so the test does not depend on a
  specific in-tree contract that may be edited.)
* The ``Phase = Slice`` and ``PhaseStatus = SliceStatus`` aliases
  resolve to the renamed types so legacy ``from egg_contracts.models
  import Phase, PhaseStatus`` keeps working.
"""

from __future__ import annotations

import pytest

from egg_contracts.models import (
    Contract,
    IssueInfo,
    Phase,  # backward-compat alias
    PhaseStatus,  # backward-compat alias
    Slice,
    SliceStatus,
)


def _legacy_payload() -> dict:
    """Return a minimal pre-#2137 contract payload."""
    return {
        "schemaVersion": "1.0",
        "issue": {
            "number": 2137,
            "title": "slice the implement phase",
            "url": "https://example.com/i/2137",
        },
        "phases": [
            {
                "id": "phase-1",
                "name": "first",
                "tasks": [],
                "dependencies": [],
            },
            {
                "id": "phase-2",
                "name": "second",
                "tasks": [],
                "dependencies": ["phase-1"],
            },
        ],
    }


def _canonical_payload() -> dict:
    """Return a post-#2137 contract payload using the canonical key."""
    return {
        "schemaVersion": "1.0",
        "issue": {
            "number": 2137,
            "title": "slice the implement phase",
            "url": "https://example.com/i/2137",
        },
        "slices": [
            {
                "id": "slice-1",
                "name": "first",
                "tasks": [],
                "dependencies": [],
            },
            {
                "id": "slice-2",
                "name": "second",
                "tasks": [],
                "dependencies": ["slice-1"],
            },
        ],
    }


class TestBackwardCompatAliases:
    """``Phase`` / ``PhaseStatus`` are aliases for the renamed types."""

    def test_phase_alias_is_slice(self) -> None:
        assert Phase is Slice

    def test_phasestatus_alias_is_slicestatus(self) -> None:
        assert PhaseStatus is SliceStatus

    def test_phase_alias_can_construct_a_slice(self) -> None:
        # ``Phase(...)`` should produce a ``Slice`` instance and round-trip
        # cleanly through pydantic so legacy callers keep working.
        instance = Phase(id="slice-1", name="legacy import path")
        assert isinstance(instance, Slice)
        assert instance.id == "slice-1"
        assert instance.status is SliceStatus.PENDING


class TestNoOpOnCanonicalPayload:
    """Loading a canonical ``slices: [...]`` payload is a no-op shim path."""

    def test_canonical_payload_loads(self) -> None:
        contract = Contract.model_validate(_canonical_payload())
        assert [s.id for s in contract.slices] == ["slice-1", "slice-2"]
        assert contract.slices[1].dependencies == ["slice-1"]

    def test_canonical_payload_does_not_set_legacy_phases(self) -> None:
        contract = Contract.model_validate(_canonical_payload())
        assert contract._legacy_phases is None

    def test_legacy_phases_alias_property_reads_through(self) -> None:
        # ``Contract.phases`` is a backward-compat property over
        # ``Contract.slices`` so legacy readers don't break.
        contract = Contract.model_validate(_canonical_payload())
        assert [p.id for p in contract.phases] == ["slice-1", "slice-2"]


class TestMigrationOnLegacyPayload:
    """Legacy ``phases: [...]`` payloads are migrated on load."""

    def test_phase_ids_rewritten_to_slice_ids(self) -> None:
        contract = Contract.model_validate(_legacy_payload())
        assert [s.id for s in contract.slices] == ["slice-1", "slice-2"]

    def test_dependency_strings_rewritten(self) -> None:
        contract = Contract.model_validate(_legacy_payload())
        # The legacy dep ``phase-1`` must surface as ``slice-1`` so the
        # DAG keeps resolving post-rename.
        assert contract.slices[1].dependencies == ["slice-1"]

    def test_legacy_payload_stashed_on_private_attr(self) -> None:
        contract = Contract.model_validate(_legacy_payload())
        # The original ``phases[]`` payload is stashed so audit tooling
        # can link legacy log entries back during the transition window.
        assert contract._legacy_phases is not None
        assert isinstance(contract._legacy_phases, list)
        assert contract._legacy_phases[0]["id"] == "phase-1"

    def test_migration_keeps_field_order_one_to_one(self) -> None:
        # The migrated list preserves the legacy order — a slice
        # scheduler that relies on declared ordering for tie-breaking
        # must still see slice-1 before slice-2.
        contract = Contract.model_validate(_legacy_payload())
        assert contract.slices[0].name == "first"
        assert contract.slices[1].name == "second"

    def test_both_keys_present_means_slices_wins(self) -> None:
        # Defensive: when a hand-edited payload carries BOTH keys, the
        # canonical ``slices`` is treated as authoritative. The migration
        # must not silently merge the two lists or the operator gets
        # surprise duplicates.
        payload = _legacy_payload()
        payload["slices"] = [
            {"id": "slice-99", "name": "canonical-only", "tasks": [], "dependencies": []},
        ]
        contract = Contract.model_validate(payload)
        assert [s.id for s in contract.slices] == ["slice-99"]
        # And no migration should fire (legacy stays unmigrated).
        assert contract._legacy_phases is None

    def test_malformed_phases_value_does_not_silently_migrate(self) -> None:
        # If ``phases`` is not a list, the migration shim restores the
        # original value and falls through to pydantic. Since the
        # contract's pydantic model no longer declares ``phases`` as a
        # field, the malformed value is simply ignored — but crucially
        # the malformed value does NOT silently appear under
        # ``contract.slices``. The slice list stays empty (no
        # half-migrated rows from the bad input).
        payload = _legacy_payload()
        payload["phases"] = "not-a-list"
        contract = Contract.model_validate(payload)
        assert contract.slices == []
        assert contract._legacy_phases is None


class TestRoundTripInvariant:
    """Round-trip dump → reload of a migrated contract is idempotent."""

    def test_dump_only_emits_slices_key(self) -> None:
        contract = Contract.model_validate(_legacy_payload())
        dumped = contract.model_dump()
        assert "slices" in dumped
        # The canonical dump must NOT carry a legacy ``phases`` key —
        # otherwise the second load would re-run the migration and the
        # ``_legacy_phases`` invariant breaks.
        assert "phases" not in dumped

    def test_reload_after_dump_does_not_remigrate(self) -> None:
        original = Contract.model_validate(_legacy_payload())
        # Sanity: original migrated.
        assert original._legacy_phases is not None
        reloaded = Contract.model_validate(original.model_dump())
        # The second load is on a canonical payload, so the shim
        # should leave ``_legacy_phases`` unset.
        assert reloaded._legacy_phases is None
        # And the slice list must be identical to the migrated original.
        assert [s.id for s in reloaded.slices] == [s.id for s in original.slices]
        assert reloaded.slices[1].dependencies == ["slice-1"]


class TestLegacyIdAcceptedDuringTransition:
    """The Slice ``id`` pattern accepts both ``slice-N`` and ``phase-N``.

    The migration rewrites ``phase-N`` to ``slice-N`` on load, but the
    pattern itself accepts both so a contract authored mid-migration
    (mixed ``slices`` key with one ``phase-3`` entry) loads instead of
    raising at the field-validation step.
    """

    def test_slice_with_phase_n_id_pattern_validates(self) -> None:
        # Direct construction (no migration shim) — the pattern allows
        # ``phase-N`` so callers that have not yet flipped to canonical
        # ids keep working.
        instance = Slice(id="phase-7", name="mid-rename")
        assert instance.id == "phase-7"

    def test_slice_with_slice_n_id_pattern_validates(self) -> None:
        instance = Slice(id="slice-7", name="post-rename")
        assert instance.id == "slice-7"


class TestContractIssueOrPipelineIdRequirement:
    """Sanity: the ``_require_issue_or_pipeline_id`` post-validator still fires."""

    def test_neither_issue_nor_pipeline_id_raises(self) -> None:
        # Pydantic raises ValidationError, but we catch the broader
        # base for forward-compat since the error type may evolve
        # across pydantic versions and this test documents the
        # invariant that *some* error must surface.
        with pytest.raises(ValueError):
            Contract.model_validate({"slices": []})

    def test_issue_only_loads(self) -> None:
        contract = Contract.model_validate(
            {
                "slices": [],
                "issue": {"number": 2137, "title": "x", "url": "u"},
            }
        )
        assert isinstance(contract.issue, IssueInfo)
        assert contract.issue.number == 2137

    def test_pipeline_id_only_loads(self) -> None:
        contract = Contract.model_validate({"slices": [], "pipeline_id": "issue-2137"})
        assert contract.pipeline_id == "issue-2137"
