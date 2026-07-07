"""Tests for the versioned structured-finding schema (#3523, slice-2).

Covers ``shared/egg_contracts/review_findings.py`` — the schema and boundary
validator that replace the prose-only NACK reason. Per task-2-2 acceptance,
this exercises:

- a valid blocking finding round-trips (dict + validator);
- a blocking finding with no ``failure_scenario`` is flagged
  non-blocking-eligible — never a valid blocking finding, never an error;
- ``severity`` / ``confidence`` enums reject junk (at the validator boundary);
- the ``schema_version`` field is present and stable;
- serialization round-trips (Finding, FindingAnchor, FindingsPayload);
- additive unknown fields do not break validation (and, via ``extra="allow"``,
  survive a constructor-level ``model_dump`` round-trip).

Verdict computation is out of scope for this slice (S3); these tests assert
only the schema shape and the ``is_blocking_eligible`` / ``effective_severity``
rule the verdict path will later consult.
"""

from __future__ import annotations

import pytest

from egg_contracts import (
    FINDINGS_SCHEMA_VERSION,
    Finding,
    FindingAnchor,
    FindingConfidence,
    FindingSeverity,
    FindingsPayload,
    non_blocking_eligible_warnings,
    validate_findings_payload,
)


def _blocking_finding_dict(**overrides: object) -> dict:
    """A structurally-complete blocking finding dict (with a failure scenario)."""
    base = {
        "id": "f-1",
        "role": "reviewer_security",
        "anchor": {
            "path": "orchestrator/approval_matrix.py",
            "line_start": 40,
            "line_end": 52,
            "slice_level": False,
        },
        "summary": "Verdict is computed before findings are validated.",
        "failure_scenario": (
            "Given a payload whose severity is 'blocking' but role is empty, "
            "the matrix NACKs the edge before the boundary validator rejects it, "
            "escalating a healthy slice to HITL."
        ),
        "severity": "blocking",
        "confidence": "high",
        "evidence": "verdict = compute(edge)  # runs before validate_findings_payload",
    }
    base.update(overrides)
    return base


def _payload_dict(findings: list[dict], **overrides: object) -> dict:
    base = {"role": "reviewer_security", "findings": findings}
    base.update(overrides)
    return base


class TestValidBlockingFindingRoundTrips:
    def test_from_dict_to_dict_round_trip(self):
        data = _blocking_finding_dict()
        finding = Finding.from_dict(data)
        assert finding.is_blocking_eligible() is True
        assert finding.effective_severity() == FindingSeverity.BLOCKING
        out = finding.to_dict()
        # Round-trip is stable: re-hydrating the dict yields an equal model.
        assert Finding.from_dict(out).to_dict() == out
        # Field fidelity on the core anchored fields.
        assert out["severity"] == "blocking"
        assert out["confidence"] == "high"
        assert out["anchor"]["path"] == "orchestrator/approval_matrix.py"
        assert out["anchor"]["line_start"] == 40
        assert out["failure_scenario"] == data["failure_scenario"]

    def test_valid_blocking_payload_validates_with_no_warnings(self):
        payload = validate_findings_payload(_payload_dict([_blocking_finding_dict()]))
        assert isinstance(payload, FindingsPayload)
        assert len(payload.findings) == 1
        assert payload.blocking_eligible_findings() == payload.findings
        assert payload.non_blocking_eligible_findings() == []
        assert non_blocking_eligible_warnings(payload) == []

    def test_payload_round_trips(self):
        data = _payload_dict([_blocking_finding_dict(), _blocking_finding_dict(id="f-2")])
        payload = FindingsPayload.from_dict(data)
        assert payload.to_dict() == FindingsPayload.from_dict(payload.to_dict()).to_dict()
        assert [f.id for f in payload.findings] == ["f-1", "f-2"]


class TestBlockingWithoutFailureScenario:
    """A blocking finding with no failure_scenario: representable, flagged, never erroring."""

    def test_representable_but_not_blocking_eligible(self):
        data = _blocking_finding_dict(failure_scenario="")
        finding = Finding.from_dict(data)  # must NOT raise
        assert finding.severity == FindingSeverity.BLOCKING
        assert finding.is_blocking_eligible() is False
        # Downgraded to advisory for verdict purposes.
        assert finding.effective_severity() == FindingSeverity.ADVISORY

    def test_whitespace_only_scenario_is_not_eligible(self):
        finding = Finding.from_dict(_blocking_finding_dict(failure_scenario="   \n\t "))
        assert finding.is_blocking_eligible() is False
        assert finding.effective_severity() == FindingSeverity.ADVISORY

    def test_validator_flags_but_does_not_raise(self):
        payload = validate_findings_payload(
            _payload_dict([_blocking_finding_dict(failure_scenario="")])
        )
        # It is surfaced as non-blocking-eligible, not as a blocking finding.
        assert payload.blocking_eligible_findings() == []
        assert len(payload.non_blocking_eligible_findings()) == 1
        warnings = non_blocking_eligible_warnings(payload)
        assert len(warnings) == 1
        assert "f-1" in warnings[0]
        assert "advisory" in warnings[0].lower()

    def test_advisory_without_scenario_is_fine_and_unwarned(self):
        finding = Finding.from_dict(
            _blocking_finding_dict(severity="advisory", failure_scenario="")
        )
        assert finding.is_blocking_eligible() is False
        assert finding.effective_severity() == FindingSeverity.ADVISORY
        payload = validate_findings_payload(
            _payload_dict([_blocking_finding_dict(severity="advisory", failure_scenario="")])
        )
        assert non_blocking_eligible_warnings(payload) == []


class TestEnumRejectsJunk:
    def test_junk_severity_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_findings_payload(_payload_dict([_blocking_finding_dict(severity="kinda-bad")]))

    def test_junk_confidence_raises_valueerror(self):
        with pytest.raises(ValueError):
            validate_findings_payload(
                _payload_dict([_blocking_finding_dict(confidence="pretty-sure")])
            )

    def test_valid_enum_members(self):
        assert FindingSeverity("blocking") is FindingSeverity.BLOCKING
        assert FindingSeverity("advisory") is FindingSeverity.ADVISORY
        assert FindingConfidence("high") is FindingConfidence.HIGH
        assert FindingConfidence("medium") is FindingConfidence.MEDIUM
        assert FindingConfidence("low") is FindingConfidence.LOW


class TestBoundaryValidatorRejectsMalformed:
    def test_missing_role_on_payload_raises(self):
        with pytest.raises(ValueError):
            validate_findings_payload({"findings": []})

    def test_missing_id_on_finding_raises(self):
        bad = _blocking_finding_dict()
        del bad["id"]
        with pytest.raises(ValueError):
            validate_findings_payload(_payload_dict([bad]))

    def test_missing_summary_on_finding_raises(self):
        bad = _blocking_finding_dict()
        del bad["summary"]
        with pytest.raises(ValueError):
            validate_findings_payload(_payload_dict([bad]))

    def test_empty_findings_list_is_valid(self):
        payload = validate_findings_payload(_payload_dict([]))
        assert payload.findings == []
        assert payload.blocking_eligible_findings() == []


class TestSchemaVersion:
    def test_version_constant_is_stable_positive_int(self):
        assert isinstance(FINDINGS_SCHEMA_VERSION, int)
        assert FINDINGS_SCHEMA_VERSION >= 1

    def test_finding_defaults_to_current_version(self):
        finding = Finding.from_dict(_blocking_finding_dict())
        assert finding.schema_version == FINDINGS_SCHEMA_VERSION
        assert finding.to_dict()["schema_version"] == FINDINGS_SCHEMA_VERSION

    def test_payload_defaults_to_current_version(self):
        payload = validate_findings_payload(_payload_dict([]))
        assert payload.schema_version == FINDINGS_SCHEMA_VERSION

    def test_explicit_version_survives_round_trip(self):
        finding = Finding.from_dict(_blocking_finding_dict(schema_version=1))
        assert finding.to_dict()["schema_version"] == 1


class TestAdditiveUnknownFields:
    """Additive evolution: a newer producer's extra fields don't break an older validator.

    The acceptance guarantee is *non-breakage* — an unknown field must not turn a
    structurally-valid payload into a validation error, and the known fields must
    still parse correctly. (The coder's ``from_dict`` reconstructs from known keys,
    so extras arriving via the dict/validator path are tolerated but not retained;
    the ``extra="allow"`` config additionally lets the pydantic constructor retain
    them, asserted separately below.)
    """

    def test_extra_field_on_finding_does_not_break_validation(self):
        finding = Finding.from_dict(_blocking_finding_dict(future_field="from a newer producer"))
        # Unknown field is tolerated; the known contract still parses.
        assert finding.is_blocking_eligible() is True
        assert finding.id == "f-1"
        assert finding.severity == FindingSeverity.BLOCKING

    def test_extra_field_on_payload_does_not_break_validation(self):
        payload = validate_findings_payload(
            _payload_dict([_blocking_finding_dict()], future_envelope_field=123)
        )
        assert isinstance(payload, FindingsPayload)
        assert len(payload.findings) == 1
        assert payload.role == "reviewer_security"

    def test_extra_field_on_anchor_does_not_break_validation(self):
        anchor = FindingAnchor.from_dict(
            {"path": "x.py", "line_start": 1, "future_anchor_field": True}
        )
        assert anchor.path == "x.py"
        assert anchor.line_start == 1

    def test_constructor_extra_allow_retains_unknown_field(self):
        # extra="allow" means the pydantic constructor keeps unknown fields, so
        # they survive a model_dump round-trip even before any schema bump.
        finding = Finding(id="f-1", role="reviewer_security", summary="s", future_field="kept")
        assert finding.model_dump()["future_field"] == "kept"
        payload = FindingsPayload(role="reviewer_security", envelope_extra=7)
        assert payload.model_dump()["envelope_extra"] == 7


class TestConvergenceAndAnchorShape:
    def test_converged_roles_round_trip(self):
        finding = Finding.from_dict(
            _blocking_finding_dict(converged_roles=["reviewer_security", "reviewer_concurrency"])
        )
        assert finding.converged_roles == ["reviewer_security", "reviewer_concurrency"]
        assert finding.to_dict()["converged_roles"] == [
            "reviewer_security",
            "reviewer_concurrency",
        ]

    def test_slice_level_anchor_without_path(self):
        finding = Finding.from_dict(
            _blocking_finding_dict(
                anchor={"slice_level": True}, failure_scenario="cross-cutting repro"
            )
        )
        assert finding.anchor.slice_level is True
        assert finding.anchor.path is None
        assert finding.is_blocking_eligible() is True

    def test_default_anchor_when_absent(self):
        data = _blocking_finding_dict()
        del data["anchor"]
        finding = Finding.from_dict(data)
        assert isinstance(finding.anchor, FindingAnchor)
        assert finding.anchor.slice_level is False
