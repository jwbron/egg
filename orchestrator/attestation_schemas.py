"""Per-role attestation schemas for BRC consensus protocol.

Attestations are structured claims that agents include in their proposals
and reviews. They serve as costly signals — harder to produce without
actually doing the work — and enable cross-verification by reviewers.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AttestationStrictness(StrEnum):
    """Strictness level for attestation validation."""

    STRICT = "strict"  # All fields required, non-empty
    RELAXED = "relaxed"  # Partial fields allowed


# --- Producer attestations ---


class CoderAttestation(BaseModel):
    """Attestation for coder role proposals."""

    commit_shas: list[str] = Field(default_factory=list, description="Commit SHAs produced")
    files_changed: list[str] = Field(default_factory=list, description="Files modified")
    test_summary: str = Field(default="", description="Test pass/fail summary")
    risk_considered: str = Field(default="", description="One risk considered and why acceptable")


class TesterAttestation(BaseModel):
    """Attestation for tester role proposals (includes lint/type-check responsibilities)."""

    tests_written: int = Field(default=0, description="Number of tests written")
    tests_run: int = Field(default=0, description="Number of tests run")
    tests_execution_blocked: bool = Field(
        default=False,
        description="True if tests could not be executed (e.g., private network mode blocks module downloads)",
    )
    tests_execution_blocked_reason: str = Field(
        default="", description="Why tests could not be executed"
    )
    no_test_changes_needed: bool = Field(
        default=False,
        description=(
            "True if the slice/diff warrants no new tests — pure refactor "
            "(symbol moves, no behavior change) or doc-only changes (#2431). "
            "Distinct from tests_execution_blocked: the tester ran the "
            "configured checks and they passed; there was simply nothing "
            "new to author. Reviewers should verify the diff really is "
            "behavior-preserving before ACKing this."
        ),
    )
    no_test_changes_reason: str = Field(
        default="",
        description=(
            "Why no new tests are warranted (e.g. 'pure refactor: symbol "
            "moves between submodules, no behavior change; existing test "
            "coverage applies'). Required when no_test_changes_needed=true."
        ),
    )
    coverage_delta: str = Field(default="", description="Coverage change")
    edge_cases: list[str] = Field(default_factory=list, description="Edge cases covered")
    concern_considered: str = Field(default="", description="One concern considered")
    lint_results: str = Field(default="", description="Lint results summary")
    type_results: str = Field(default="", description="Type check results")
    auto_fixes: list[str] = Field(default_factory=list, description="Auto-fixes applied")
    checks_passed: list[str] = Field(
        default_factory=list,
        description="Names of configured checks that passed (e.g. ['lint', 'test'])",
    )


class DocumenterAttestation(BaseModel):
    """Attestation for documenter role proposals."""

    sections_updated: list[str] = Field(default_factory=list, description="Doc sections updated")
    links_verified: list[str] = Field(default_factory=list, description="Links verified")
    concern_considered: str = Field(default="", description="One concern considered")


# --- Reviewer attestations ---


class ReviewerCodeAttestation(BaseModel):
    """Attestation for code reviewer ACK/NACK."""

    files_reviewed: list[str] = Field(
        default_factory=list, description="Specific file paths reviewed"
    )
    issues_found: int = Field(default=0, description="Issues found")
    issues_resolved: int = Field(default=0, description="Issues resolved")
    risk_considered: str = Field(default="", description="One risk considered")


class ReviewerContractAttestation(BaseModel):
    """Attestation for contract reviewer ACK/NACK."""

    tasks_verified: list[str] = Field(default_factory=list, description="Task IDs verified")
    acceptance_criteria_checked: list[str] = Field(
        default_factory=list, description="Criteria checked"
    )
    gaps_identified: list[str] = Field(default_factory=list, description="Gaps found")


# --- Payload wrappers ---

# Map role names to their attestation model
PRODUCER_ATTESTATION_MODELS: dict[str, type[BaseModel]] = {
    "coder": CoderAttestation,
    "tester": TesterAttestation,
    "documenter": DocumenterAttestation,
}

REVIEWER_ATTESTATION_MODELS: dict[str, type[BaseModel]] = {
    "reviewer_code": ReviewerCodeAttestation,
    "reviewer_contract": ReviewerContractAttestation,
    "tester": TesterAttestation,  # Tester is also a reviewer (includes lint/type-check)
}


class ProposalPayload(BaseModel):
    """Payload for CONSENSUS_PROPOSE messages.

    Wraps the producer's work summary and attestation. Validates that
    artifact references are non-empty (prevents generic "looks good").
    """

    summary: str = Field(..., description="Summary of work done")
    attestation: dict[str, Any] = Field(
        default_factory=dict, description="Role-specific attestation"
    )
    artifacts: list[str] = Field(
        default_factory=list, description="Artifact references (file paths, commit SHAs)"
    )
    risk_considered: str = Field(default="", description="One risk considered and why acceptable")
    commit_sha: str = Field(
        default="",
        description="Commit SHA pushed to the remote branch before proposing (#1473)",
    )

    @model_validator(mode="after")
    def validate_artifacts_non_empty(self) -> ProposalPayload:
        """Reject proposals with no artifact references."""
        if not self.artifacts:
            raise ValueError(
                "Proposal must reference at least one artifact (file path, commit SHA, etc.)"
            )
        return self

    @model_validator(mode="after")
    def validate_commit_sha_present(self) -> ProposalPayload:
        """Require commit_sha so reviewers can verify pushed code (#1473)."""
        if not self.commit_sha:
            raise ValueError(
                "Proposal must include commit_sha referencing a pushed commit. "
                "Commit and push your work before proposing consensus."
            )
        return self


class ReviewPayload(BaseModel):
    """Payload for CONSENSUS_ACK and CONSENSUS_NACK messages.

    Wraps the reviewer's verdict and attestation. Validates that
    artifact references are non-empty (prevents rubber-stamping).
    """

    verdict: str = Field(..., description="ACK or NACK")
    attestation: dict[str, Any] = Field(
        default_factory=dict, description="Role-specific attestation"
    )
    artifact_references: list[str] = Field(
        default_factory=list, description="Specific artifacts reviewed"
    )
    reason: str = Field(default="", description="Reason for verdict (required for NACK)")
    risk_considered: str = Field(default="", description="One risk considered")
    pre_merge_condition: str = Field(
        default="",
        max_length=1000,
        description=(
            "Structured obligation that must be performed by a human before "
            "merging the PR (issue #1998). Only valid alongside an ACK verdict "
            "— reviewers issue a conditional ACK when the work is otherwise "
            "correct but requires a merge-time human action the agents cannot "
            "perform (e.g. a git mv, a config rotation). Empty string means "
            "an unconditional ACK."
        ),
    )
    pre_merge_condition_resolved_in_diff: str = Field(
        default="",
        max_length=200,
        pattern=r"^[A-Fa-f0-9]{0,200}$",
        description=(
            "Optional commit SHA that satisfied ``pre_merge_condition`` within "
            "the same PR's diff (issue #2336). Set this on a re-ACK when the "
            "obligation has been met in-pipeline since your initial conditional "
            "ACK — the PR-body renderer demotes resolved obligations out of "
            "the merge-blocking section so reviewers don't see boilerplate "
            "'do not merge' text on busywork. Only meaningful when "
            "``pre_merge_condition`` is also non-empty. Hex-only to prevent "
            "newline injection bending the rendered PR-body markdown."
        ),
    )

    @model_validator(mode="after")
    def validate_artifact_references(self) -> ReviewPayload:
        """Reject reviews with no artifact references."""
        if not self.artifact_references:
            raise ValueError(
                "Review must reference specific artifacts (file paths, line numbers, commit SHAs)"
            )
        return self

    @model_validator(mode="after")
    def validate_nack_has_reason(self) -> ReviewPayload:
        """Require reason for NACK verdicts."""
        if self.verdict == "NACK" and not self.reason:
            raise ValueError("NACK verdict must include a reason")
        return self

    @model_validator(mode="after")
    def validate_condition_only_on_ack(self) -> ReviewPayload:
        """Reject a pre_merge_condition attached to a NACK.

        A conditional NACK is nonsensical — NACK already blocks the producer,
        so there's nothing for a human to approve or defer (#1998).
        """
        if self.pre_merge_condition and self.verdict != "ACK":
            raise ValueError(
                "pre_merge_condition is only valid on ACK verdicts (conditional ACK); "
                "NACK already blocks the producer"
            )
        return self

    @model_validator(mode="after")
    def validate_resolution_requires_condition(self) -> ReviewPayload:
        """Reject a resolution SHA without an accompanying obligation.

        ``pre_merge_condition_resolved_in_diff`` only makes sense when the ACK
        also carries a ``pre_merge_condition`` to resolve (#2336). A resolution
        SHA on a plain ACK has nothing to attach to and would be silently
        dropped downstream — fail loudly at the boundary instead.
        """
        if self.pre_merge_condition_resolved_in_diff and not self.pre_merge_condition:
            raise ValueError(
                "pre_merge_condition_resolved_in_diff requires a non-empty "
                "pre_merge_condition; a resolution SHA has nothing to resolve "
                "on a plain ACK"
            )
        return self


def validate_attestation(
    role: str,
    attestation_data: dict[str, Any],
    strictness: AttestationStrictness = AttestationStrictness.STRICT,
    is_producer: bool = True,
) -> BaseModel:
    """Validate attestation data against role-specific schema.

    Args:
        role: Agent role name
        attestation_data: Raw attestation dict
        strictness: Validation strictness level
        is_producer: Whether this is a producer or reviewer attestation

    Returns:
        Validated attestation model

    Raises:
        ValueError: If role unknown or validation fails
    """
    models = PRODUCER_ATTESTATION_MODELS if is_producer else REVIEWER_ATTESTATION_MODELS
    model_cls = models.get(role)
    if not model_cls:
        raise ValueError(f"No attestation schema for role '{role}' (is_producer={is_producer})")

    instance = model_cls(**attestation_data)

    if strictness == AttestationStrictness.STRICT:
        # In strict mode, check that key fields are non-empty
        # This is role-specific — check the fields that matter most
        _validate_strict(role, instance, is_producer)

    return instance


def _validate_strict(role: str, instance: BaseModel, is_producer: bool) -> None:
    """Strict validation: ensure key attestation fields are populated."""
    if is_producer:
        if role == "coder" and isinstance(instance, CoderAttestation):
            if not instance.commit_shas:
                raise ValueError(
                    "Coder attestation requires at least one commit SHA in strict mode"
                )
            if not instance.files_changed:
                raise ValueError(
                    "Coder attestation requires at least one changed file in strict mode"
                )
        elif role == "tester" and isinstance(instance, TesterAttestation):
            # Mutual exclusion (#2431): tests_execution_blocked and
            # no_test_changes_needed describe different failure modes —
            # the former means the tester couldn't run checks; the
            # latter means it ran them and found nothing new to author.
            # A proposal that asserts both is incoherent.
            if instance.tests_execution_blocked and instance.no_test_changes_needed:
                raise ValueError(
                    "Tester attestation has both tests_execution_blocked=true and "
                    "no_test_changes_needed=true — these are mutually exclusive. "
                    "Pick one: 'blocked' means the configured checks could not run; "
                    "'no_test_changes_needed' means they ran and passed but the slice "
                    "warrants no new tests."
                )
            if instance.tests_execution_blocked:
                if not instance.tests_execution_blocked_reason:
                    raise ValueError(
                        "Tester attestation requires tests_execution_blocked_reason "
                        "when tests_execution_blocked is true"
                    )
                if instance.tests_run > 0:
                    raise ValueError(
                        "Tester attestation has tests_execution_blocked=true but "
                        "tests_run > 0 — these are mutually exclusive. If some tests "
                        "ran, set tests_execution_blocked=false and report normally"
                    )
            elif instance.no_test_changes_needed:
                # No-op propose path for refactor / doc-only slices (#2431).
                # The tester still ran the configured checks (checks_passed
                # is required below); it just authored no new tests.
                if not instance.no_test_changes_reason.strip():
                    raise ValueError(
                        "Tester attestation requires no_test_changes_reason "
                        "when no_test_changes_needed is true. Explain why the "
                        "slice warrants no new tests (e.g. 'pure refactor: "
                        "symbol moves, no behavior change; existing test "
                        "coverage applies')."
                    )
            elif instance.tests_run == 0:
                raise ValueError(
                    "Tester attestation requires tests_run > 0 in strict mode. "
                    "If the slice is a pure refactor or doc-only change with no "
                    "new tests warranted, set no_test_changes_needed=true and "
                    "populate no_test_changes_reason instead."
                )
            # Require checks_passed to be populated when the tester ran
            # the configured checks — covers both the normal path and
            # the no_test_changes_needed path. Skip only when
            # tests_execution_blocked since the tester may not have been
            # able to run any checks (issues #1459, #1467, #2431).
            if not instance.tests_execution_blocked and not instance.checks_passed:
                raise ValueError(
                    "Tester attestation requires checks_passed to list the checks that "
                    "passed (e.g. ['lint', 'test']). Only include checks that actually "
                    "passed — do not include checks that failed."
                )
        elif role == "documenter" and isinstance(instance, DocumenterAttestation):
            if not instance.sections_updated:
                raise ValueError(
                    "Documenter attestation requires at least one section updated in strict mode"
                )
    else:
        if role == "reviewer_code" and isinstance(instance, ReviewerCodeAttestation):
            if not instance.files_reviewed:
                raise ValueError(
                    "Code reviewer attestation requires at least one file reviewed in strict mode"
                )
        elif role == "reviewer_contract" and isinstance(instance, ReviewerContractAttestation):
            if not instance.tasks_verified:
                raise ValueError(
                    "Contract reviewer attestation requires at least one task verified in strict mode"
                )
