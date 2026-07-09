"""Tests for the server-side computed verdict from findings (#3523, slice-3).

Covers ``orchestrator/review_findings_verdict.py`` — the "mechanics" half of
the S3 determinism-boundary move: the reviewer emits structured findings
(``shared/egg_contracts/review_findings.py``, S2), and this pure module
computes the edge verdict, deduplicates by causal mechanism, resolves the
``EGG_REVIEW_FINDINGS_MODE`` staged flag, and builds the computed-vs-legacy
``log``-mode record.

Per task-3-2 acceptance, this file exercises the pure-layer half:

- the three documented outcomes: blocking => NACK, advisory-only => ACK
  (with obligations), empty => ACK;
- a blocking finding lacking a ``failure_scenario`` degrades to advisory and
  does NOT produce a NACK (the vibe-NACK cannot reach the verdict);
- mechanism-level dedup attaches >=2 producing lenses to a merged finding and
  raises its confidence;
- ``review_findings_mode`` resolves a flag typo to ``off`` (never silently to
  ``on``), exactly like ``slice_green_gate.green_gate_mode``;
- ``verdict_log_record`` records the computed verdict against the legacy one.

The matrix-integration half (``record_findings_verdict`` routing + the
log-mode parity of the *acted* verdict) lives in
``test_approval_matrix.py``.
"""

from __future__ import annotations

import pytest
from egg_contracts.review_findings import (
    Finding,
    FindingAnchor,
    FindingConfidence,
    FindingSeverity,
    FindingsPayload,
)
from review_findings_verdict import (
    FINDINGS_MODE_ENV_VAR,
    VERDICT_ACK,
    VERDICT_NACK,
    compute_verdict,
    merge_findings_by_mechanism,
    review_findings_mode,
    verdict_log_record,
)


def make_finding(
    finding_id: str,
    role: str,
    *,
    severity: FindingSeverity = FindingSeverity.ADVISORY,
    failure_scenario: str = "",
    confidence: FindingConfidence = FindingConfidence.MEDIUM,
    summary: str = "a finding",
    path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    slice_level: bool = False,
    pre_merge_obligation: str | None = None,
    **extra: object,
) -> Finding:
    """Build a Finding with a concrete anchor and optional extra fields."""
    anchor = FindingAnchor(
        path=path,
        line_start=line_start,
        line_end=line_end,
        slice_level=slice_level,
    )
    return Finding(
        id=finding_id,
        role=role,
        summary=summary,
        severity=severity,
        failure_scenario=failure_scenario,
        confidence=confidence,
        anchor=anchor,
        pre_merge_obligation=pre_merge_obligation,
        **extra,
    )


def blocking_finding(
    finding_id: str = "f-1",
    role: str = "reviewer_security",
    **overrides: object,
) -> Finding:
    """A blocking-eligible finding (blocking severity + failure scenario)."""
    kwargs: dict[str, object] = {
        "severity": FindingSeverity.BLOCKING,
        "failure_scenario": (
            "Given a payload whose role is empty, the matrix NACKs before the "
            "boundary validator rejects it, escalating a healthy slice to HITL."
        ),
        "confidence": FindingConfidence.HIGH,
        "path": "orchestrator/approval_matrix.py",
        "line_start": 40,
        "line_end": 52,
    }
    kwargs.update(overrides)
    return make_finding(finding_id, role, **kwargs)


def payload(findings: list[Finding], role: str = "reviewer_security") -> FindingsPayload:
    return FindingsPayload(role=role, findings=findings)


# --- the three documented outcomes -------------------------------------------


class TestComputedOutcomes:
    def test_blocking_finding_yields_nack(self):
        computed = compute_verdict(payload([blocking_finding()]))
        assert computed.verdict == VERDICT_NACK
        assert computed.is_nack is True
        assert len(computed.blocking_findings) == 1
        assert computed.advisory_findings == []
        assert computed.obligations == []

    def test_advisory_only_yields_ack_with_obligations(self):
        finding = make_finding(
            "f-1",
            "reviewer_contract",
            severity=FindingSeverity.ADVISORY,
            pre_merge_obligation="git mv old/path new/path before merging",
        )
        computed = compute_verdict(payload([finding]))
        assert computed.verdict == VERDICT_ACK
        assert computed.is_nack is False
        assert computed.blocking_findings == []
        assert len(computed.advisory_findings) == 1
        # The advisory obligation routes through the conditional-ACK condition.
        assert computed.obligations == ["git mv old/path new/path before merging"]
        assert computed.obligation_text == "git mv old/path new/path before merging"

    def test_empty_findings_yields_unconditional_ack(self):
        computed = compute_verdict(payload([]))
        assert computed.verdict == VERDICT_ACK
        assert computed.blocking_findings == []
        assert computed.advisory_findings == []
        assert computed.obligations == []
        # No obligations => an unconditional ACK (empty condition string).
        assert computed.obligation_text == ""

    def test_advisory_without_obligation_is_unconditional_ack(self):
        finding = make_finding("f-1", "reviewer_code", severity=FindingSeverity.ADVISORY)
        computed = compute_verdict(payload([finding]))
        assert computed.verdict == VERDICT_ACK
        assert len(computed.advisory_findings) == 1
        assert computed.obligations == []
        assert computed.obligation_text == ""

    def test_mixed_blocking_and_advisory_yields_nack_and_keeps_obligation(self):
        computed = compute_verdict(
            payload(
                [
                    blocking_finding("f-1", "reviewer_security"),
                    make_finding(
                        "f-2",
                        "reviewer_contract",
                        severity=FindingSeverity.ADVISORY,
                        pre_merge_obligation="update CHANGELOG",
                    ),
                ]
            )
        )
        assert computed.verdict == VERDICT_NACK
        assert len(computed.blocking_findings) == 1
        assert len(computed.advisory_findings) == 1
        assert computed.obligations == ["update CHANGELOG"]


# --- the vibe-NACK cannot reach the verdict ----------------------------------


class TestBlockingWithoutFailureScenarioDegrades:
    def test_blocking_without_scenario_does_not_nack(self):
        finding = make_finding(
            "f-1",
            "reviewer_security",
            severity=FindingSeverity.BLOCKING,
            failure_scenario="",
            path="orchestrator/x.py",
            line_start=1,
        )
        computed = compute_verdict(payload([finding]))
        # Degraded to advisory: no failure scenario => not blocking-eligible.
        assert computed.verdict == VERDICT_ACK
        assert computed.blocking_findings == []
        assert len(computed.advisory_findings) == 1

    def test_whitespace_only_scenario_does_not_nack(self):
        finding = make_finding(
            "f-1",
            "reviewer_security",
            severity=FindingSeverity.BLOCKING,
            failure_scenario="   \n\t ",
            path="orchestrator/x.py",
            line_start=1,
        )
        computed = compute_verdict(payload([finding]))
        assert computed.verdict == VERDICT_ACK
        assert computed.blocking_findings == []

    def test_degraded_finding_alongside_real_blocker_still_nacks(self):
        computed = compute_verdict(
            payload(
                [
                    make_finding(
                        "f-degraded",
                        "reviewer_code",
                        severity=FindingSeverity.BLOCKING,
                        failure_scenario="",
                        path="orchestrator/a.py",
                        line_start=5,
                    ),
                    blocking_finding("f-real", "reviewer_security"),
                ]
            )
        )
        assert computed.verdict == VERDICT_NACK
        # Only the real blocker blocks; the degraded one is advisory.
        assert [f.id for f in computed.blocking_findings] == ["f-real"]
        assert [f.id for f in computed.advisory_findings] == ["f-degraded"]


# --- mechanism-level dedup + convergence -------------------------------------


class TestMechanismDedupAndConvergence:
    def test_two_lenses_same_anchor_merge_with_converged_roles(self):
        merged = merge_findings_by_mechanism(
            [
                blocking_finding("f-a", "reviewer_security"),
                blocking_finding("f-b", "reviewer_concurrency"),
            ]
        )
        assert len(merged) == 1
        # >=2 distinct producing lenses attached, sorted.
        assert merged[0].converged_roles == ["reviewer_concurrency", "reviewer_security"]

    def test_convergence_raises_confidence_one_rung(self):
        merged = merge_findings_by_mechanism(
            [
                blocking_finding("f-a", "reviewer_security", confidence=FindingConfidence.LOW),
                blocking_finding("f-b", "reviewer_concurrency", confidence=FindingConfidence.LOW),
            ]
        )
        assert len(merged) == 1
        # Both LOW => merged confidence raised one rung to MEDIUM.
        assert merged[0].confidence == FindingConfidence.MEDIUM

    def test_convergence_confidence_saturates_at_high(self):
        merged = merge_findings_by_mechanism(
            [
                blocking_finding("f-a", "reviewer_security", confidence=FindingConfidence.HIGH),
                blocking_finding("f-b", "reviewer_concurrency", confidence=FindingConfidence.HIGH),
            ]
        )
        assert merged[0].confidence == FindingConfidence.HIGH

    def test_explicit_mechanism_tag_merges_across_different_anchors(self):
        merged = merge_findings_by_mechanism(
            [
                make_finding(
                    "f-a",
                    "reviewer_security",
                    severity=FindingSeverity.BLOCKING,
                    failure_scenario="deadlock repro A",
                    path="orchestrator/a.py",
                    line_start=1,
                    mechanism="lock-ordering-deadlock",
                ),
                make_finding(
                    "f-b",
                    "reviewer_concurrency",
                    severity=FindingSeverity.BLOCKING,
                    failure_scenario="deadlock repro B",
                    path="orchestrator/z.py",
                    line_start=99,
                    mechanism="lock-ordering-deadlock",
                ),
            ]
        )
        # Same mechanism id => merged despite different file anchors.
        assert len(merged) == 1
        assert merged[0].converged_roles == ["reviewer_concurrency", "reviewer_security"]

    def test_single_lens_duplicate_dedupes_without_convergence(self):
        merged = merge_findings_by_mechanism(
            [
                blocking_finding("f-a", "reviewer_security"),
                blocking_finding("f-b", "reviewer_security"),
            ]
        )
        # Same mechanism, one lens filing twice: dedupes but records no convergence.
        assert len(merged) == 1
        assert merged[0].converged_roles == []

    def test_distinct_mechanisms_do_not_merge(self):
        merged = merge_findings_by_mechanism(
            [
                blocking_finding("f-a", "reviewer_security", path="a.py", line_start=1),
                blocking_finding("f-b", "reviewer_concurrency", path="b.py", line_start=2),
            ]
        )
        assert len(merged) == 2

    def test_merged_group_is_blocking_if_any_constituent_blocks(self):
        # An advisory + a blocking finding at the same anchor merge into one
        # blocking-eligible finding (most-severe representative).
        merged = merge_findings_by_mechanism(
            [
                make_finding(
                    "f-adv",
                    "reviewer_code",
                    severity=FindingSeverity.ADVISORY,
                    path="orchestrator/approval_matrix.py",
                    line_start=40,
                    line_end=52,
                ),
                blocking_finding("f-block", "reviewer_security"),
            ]
        )
        assert len(merged) == 1
        assert merged[0].effective_severity() == FindingSeverity.BLOCKING

    def test_compute_verdict_surfaces_convergence(self):
        computed = compute_verdict(
            payload(
                [
                    blocking_finding("f-a", "reviewer_security"),
                    blocking_finding("f-b", "reviewer_concurrency"),
                ]
            )
        )
        assert computed.verdict == VERDICT_NACK
        assert len(computed.findings) == 1
        assert len(computed.converged_findings) == 1
        assert computed.converged_findings[0].converged_roles == [
            "reviewer_concurrency",
            "reviewer_security",
        ]

    def test_merge_is_deterministic_and_order_stable(self):
        findings = [
            blocking_finding("f-a", "reviewer_security", path="a.py", line_start=1),
            blocking_finding("f-b", "reviewer_concurrency", path="b.py", line_start=2),
            blocking_finding("f-c", "reviewer_code", path="a.py", line_start=1),
        ]
        first = merge_findings_by_mechanism(findings)
        second = merge_findings_by_mechanism(findings)
        assert [f.to_dict() for f in first] == [f.to_dict() for f in second]
        # First-seen group order is preserved: a.py group, then b.py group.
        assert len(first) == 2


# --- staged-flag resolution (typo => off) ------------------------------------


class TestReviewFindingsMode:
    @pytest.mark.parametrize("raw", ["on", "1", "true", "yes", "ON", "  on  ", "Yes"])
    def test_enabled_values_resolve_to_on(self, monkeypatch, raw):
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, raw)
        assert review_findings_mode() == "on"

    @pytest.mark.parametrize("raw", ["log", "log-only", "log_only", "LOG", " Log-Only "])
    def test_log_values_resolve_to_log(self, monkeypatch, raw):
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, raw)
        assert review_findings_mode() == "log"

    @pytest.mark.parametrize(
        "raw", ["off", "", "  ", "nope", "onn", "l0g", "enabled", "2", "false", "logg"]
    )
    def test_typo_and_unknown_resolve_to_off(self, monkeypatch, raw):
        # A flag typo must degrade to "legacy path unchanged", never silently
        # to "computed verdict drives consensus".
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, raw)
        assert review_findings_mode() == "off"

    def test_unset_defaults_to_off(self, monkeypatch):
        monkeypatch.delenv(FINDINGS_MODE_ENV_VAR, raising=False)
        assert review_findings_mode() == "off"


# --- computed-vs-legacy log record -------------------------------------------


class TestVerdictLogRecord:
    def test_records_computed_and_legacy_disagreement(self):
        computed = compute_verdict(payload([blocking_finding()]))
        record = verdict_log_record(
            payload([blocking_finding()]),
            computed,
            legacy_verdict="ACK",
            legacy_reason="looked fine to me",
        )
        assert record["mode"] == "log"
        assert record["role"] == "reviewer_security"
        assert record["computed_verdict"] == VERDICT_NACK
        assert record["legacy_verdict"] == "ACK"
        assert record["verdicts_agree"] is False
        assert record["legacy_reason"] == "looked fine to me"
        assert record["blocking_count"] == 1
        assert record["advisory_count"] == 0

    def test_records_agreement_when_verdicts_match(self):
        computed = compute_verdict(payload([blocking_finding()]))
        record = verdict_log_record(payload([blocking_finding()]), computed, legacy_verdict="nack")
        # Case-insensitive comparison against the computed verdict.
        assert record["verdicts_agree"] is True

    def test_agrees_is_none_without_legacy_verdict(self):
        computed = compute_verdict(payload([]))
        record = verdict_log_record(payload([]), computed)
        assert record["computed_verdict"] == VERDICT_ACK
        assert record["legacy_verdict"] is None
        assert record["verdicts_agree"] is None

    def test_record_reports_obligations_and_convergence(self):
        computed = compute_verdict(
            payload(
                [
                    blocking_finding("f-a", "reviewer_security"),
                    blocking_finding("f-b", "reviewer_concurrency"),
                    make_finding(
                        "f-adv",
                        "reviewer_contract",
                        severity=FindingSeverity.ADVISORY,
                        pre_merge_obligation="update docs",
                    ),
                ]
            )
        )
        record = verdict_log_record(payload([]), computed, legacy_verdict="NACK")
        assert record["obligation_count"] == 1
        assert len(record["converged"]) == 1
        assert record["converged"][0]["converged_roles"] == [
            "reviewer_concurrency",
            "reviewer_security",
        ]
        # The full deduped finding set is embedded for the operator to inspect.
        assert isinstance(record["findings"], list)
        assert all(isinstance(f, dict) for f in record["findings"])

    def test_log_record_is_pure_json_serializable(self):
        import json

        computed = compute_verdict(payload([blocking_finding()]))
        record = verdict_log_record(payload([blocking_finding()]), computed, legacy_verdict="ACK")
        # A pure record: JSON round-trips without custom encoders.
        assert json.loads(json.dumps(record))["computed_verdict"] == VERDICT_NACK
