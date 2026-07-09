"""Tests for the findings-computed verdict routed through the matrix (#3523, S3).

Covers ``ApprovalMatrix.record_findings_verdict`` — the integration point
where a computed verdict (``review_findings_verdict.compute_verdict``) is
recorded through the SAME ``record_ack`` / ``record_nack`` primitives the
legacy prose path uses. Per task-3-2, this file exercises the *acted*-verdict
half:

- a blocking verdict routes to a real NACK (edge NACKED, reason carried);
- an advisory-only verdict routes to an ACK carrying the advisory obligations
  as the conditional-ACK ``pre_merge_condition``;
- an empty verdict routes to an unconditional ACK;
- **log-mode parity**: in ``off`` / ``log`` mode the *acted* verdict stays the
  legacy one and the computed path never mutates the matrix (an explicit
  parity assertion against the legacy state), diverging only in ``on`` mode;
- the flag-typo-fails-to-off guarantee is asserted here too (the acting path
  is only reached in ``on`` mode).

The pure compute/dedup/mode/log-record layer is tested in
``test_review_findings_verdict.py``.
"""

from __future__ import annotations

from approval_matrix import ApprovalMatrix, ApprovalState
from egg_contracts.review_findings import (
    Finding,
    FindingAnchor,
    FindingConfidence,
    FindingSeverity,
    FindingsPayload,
)
from review_findings_verdict import (
    FINDINGS_MODE_ENV_VAR,
    compute_verdict,
    review_findings_mode,
    verdict_log_record,
)
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph


def make_matrix() -> ApprovalMatrix:
    """A matrix with a single reviewer_security -> coder critical edge."""
    graph = ReviewGraph(
        [
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
        ]
    )
    return ApprovalMatrix(graph)


def blocking_finding(finding_id: str = "f-1", role: str = "reviewer_security") -> Finding:
    return Finding(
        id=finding_id,
        role=role,
        summary="Verdict computed before findings validated.",
        severity=FindingSeverity.BLOCKING,
        failure_scenario=(
            "Given an empty-role payload, the matrix NACKs the edge before the "
            "validator rejects it, escalating a healthy slice to HITL."
        ),
        confidence=FindingConfidence.HIGH,
        anchor=FindingAnchor(path="orchestrator/approval_matrix.py", line_start=40, line_end=52),
    )


def advisory_finding(
    finding_id: str = "f-adv",
    role: str = "reviewer_security",
    obligation: str | None = "git mv old/path new/path before merging",
) -> Finding:
    return Finding(
        id=finding_id,
        # Distinct summary per finding so mechanism-dedup does not merge two
        # unanchored advisory findings (which would collapse their obligations).
        summary=f"Advisory obligation {finding_id}.",
        role=role,
        severity=FindingSeverity.ADVISORY,
        confidence=FindingConfidence.MEDIUM,
        pre_merge_obligation=obligation,
    )


def payload(findings: list[Finding]) -> FindingsPayload:
    return FindingsPayload(role="reviewer_security", findings=findings)


# --- record_findings_verdict routing -----------------------------------------


class TestRecordFindingsVerdictRouting:
    def test_blocking_verdict_records_nack(self):
        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        computed = compute_verdict(payload([blocking_finding()]))

        entry = matrix.record_findings_verdict(
            "reviewer_security",
            "coder",
            version,
            computed,
            reason="1 blocking finding must be addressed.",
        )
        assert entry.state == ApprovalState.NACKED
        assert entry.reason == "1 blocking finding must be addressed."
        assert matrix.has_unresolved_nacks_as_producer("coder") is True
        assert matrix.is_fully_acked("coder") is False

    def test_advisory_only_verdict_records_conditional_ack(self):
        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        computed = compute_verdict(payload([advisory_finding()]))

        entry = matrix.record_findings_verdict("reviewer_security", "coder", version, computed)
        assert entry.state == ApprovalState.ACKED
        # The advisory obligation rode through as the conditional-ACK condition.
        assert entry.pre_merge_condition == "git mv old/path new/path before merging"
        assert matrix.is_fully_acked("coder") is True

        conditions = matrix.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["condition"] == "git mv old/path new/path before merging"
        assert conditions[0]["producer"] == "coder"
        assert conditions[0]["version"] == version

    def test_empty_verdict_records_unconditional_ack(self):
        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        computed = compute_verdict(payload([]))

        entry = matrix.record_findings_verdict("reviewer_security", "coder", version, computed)
        assert entry.state == ApprovalState.ACKED
        assert entry.pre_merge_condition == ""
        assert matrix.is_fully_acked("coder") is True
        # An unconditional ACK surfaces no pre-merge obligation.
        assert matrix.get_pre_merge_conditions() == []

    def test_multiple_advisory_obligations_join_into_condition(self):
        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        computed = compute_verdict(
            payload(
                [
                    advisory_finding("f-1", obligation="run migration 0007"),
                    advisory_finding("f-2", obligation="bump the schema version"),
                ]
            )
        )
        entry = matrix.record_findings_verdict("reviewer_security", "coder", version, computed)
        assert entry.state == ApprovalState.ACKED
        assert entry.pre_merge_condition == "run migration 0007\nbump the schema version"

    def test_degraded_blocking_verdict_acks_not_nacks(self):
        # A blocking-severity finding with no failure_scenario degrades to
        # advisory, so routing it through the matrix ACKs rather than NACKs.
        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        degraded = Finding(
            id="f-1",
            role="reviewer_security",
            summary="looks scary",
            severity=FindingSeverity.BLOCKING,
            failure_scenario="",
            anchor=FindingAnchor(path="orchestrator/x.py", line_start=1),
        )
        computed = compute_verdict(payload([degraded]))
        entry = matrix.record_findings_verdict("reviewer_security", "coder", version, computed)
        assert entry.state == ApprovalState.ACKED
        assert matrix.has_unresolved_nacks_as_producer("coder") is False


# --- log-mode parity of the *acted* verdict ----------------------------------


class TestLogModeParity:
    """log mode records computed-vs-legacy without changing the acted verdict.

    ``record_findings_verdict`` is the ACTING path — a caller reaches it only
    in ``on`` mode. In ``off`` / ``log`` mode the legacy ``record_ack`` /
    ``record_nack`` calls remain authoritative, and the only findings-specific
    work is ``verdict_log_record`` — a pure function that takes no matrix and
    therefore cannot mutate the acted verdict. These tests assert that
    invariant explicitly against the legacy matrix state.
    """

    def test_log_record_does_not_mutate_the_acted_legacy_verdict(self, monkeypatch):
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, "log")
        assert review_findings_mode() == "log"

        # Legacy path is authoritative in log mode: the reviewer legacy-ACKed.
        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_security", "coder", version)
        legacy_state = matrix.to_dict()

        # The computed verdict DISAGREES (it is a NACK), but in log mode the
        # only findings work is producing the record — which cannot touch the
        # matrix (it takes no matrix argument).
        computed = compute_verdict(payload([blocking_finding()]))
        record = verdict_log_record(
            payload([blocking_finding()]),
            computed,
            legacy_verdict="ACK",
        )

        # Parity: the acted verdict is byte-identical to the legacy ACK.
        assert matrix.to_dict() == legacy_state
        assert matrix.get_entry("reviewer_security", "coder").state == ApprovalState.ACKED
        # ...and the record captured the would-be-divergent computed verdict.
        assert record["computed_verdict"] == "NACK"
        assert record["legacy_verdict"] == "ACK"
        assert record["verdicts_agree"] is False

    def test_off_and_log_modes_produce_identical_acted_state(self, monkeypatch):
        # The acted (legacy) verdict must be identical whether the flag is off
        # or log — only `on` may diverge. Apply the same legacy NACK under both
        # modes and compare the resulting matrix state.
        def strip_timestamps(state: dict) -> dict:
            # record_nack stamps datetime.now(), which differs between the two
            # runs; the acted *verdict* (state/version/reason/condition) is what
            # parity is about, so normalize the wall-clock fields out.
            for entry in state["entries"].values():
                entry.pop("timestamp", None)
                entry.pop("obligation_resolved_at", None)
            return state

        def acted_state_under(mode: str) -> dict:
            monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, mode)
            assert review_findings_mode() == mode
            matrix = make_matrix()
            version = matrix.record_proposal("coder")
            # Legacy verdict is a prose NACK in both off and log.
            matrix.record_nack("reviewer_security", "coder", version, reason="legacy prose")
            if review_findings_mode() == "log":
                # log additionally emits the record — side-effect-free.
                verdict_log_record(
                    payload([blocking_finding()]),
                    compute_verdict(payload([blocking_finding()])),
                    legacy_verdict="NACK",
                )
            return strip_timestamps(matrix.to_dict())

        assert acted_state_under("off") == acted_state_under("log")

    def test_on_mode_acting_path_diverges_from_legacy(self, monkeypatch):
        # The contrast that makes the parity meaningful: in `on` mode the
        # computed verdict DOES act, flipping the edge the legacy ACK held.
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, "on")
        assert review_findings_mode() == "on"

        matrix = make_matrix()
        version = matrix.record_proposal("coder")
        computed = compute_verdict(payload([blocking_finding()]))
        matrix.record_findings_verdict(
            "reviewer_security", "coder", version, computed, reason="blocking"
        )
        assert matrix.get_entry("reviewer_security", "coder").state == ApprovalState.NACKED

    def test_flag_typo_resolves_to_off(self, monkeypatch):
        # A flag typo must degrade to "legacy path unchanged"; the acting path
        # is gated on `on`, which a typo never resolves to.
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, "onn")
        assert review_findings_mode() == "off"
        monkeypatch.setenv(FINDINGS_MODE_ENV_VAR, "l0g")
        assert review_findings_mode() == "off"
