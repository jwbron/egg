"""Per-role attestation schemas for BRC consensus protocol.

Attestations are structured claims that agents include in their proposals
and reviews. They serve as costly signals — harder to produce without
actually doing the work — and enable cross-verification by reviewers.
"""

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

_COMMIT_SHA_PATTERN = re.compile(r"[A-Za-z0-9_]{7,64}")


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


class ConsideredCandidate(BaseModel):
    """A decision candidate weighed and dispositioned away, not registered (#3526).

    The structured unit of the explicit-none ledger: instead of one
    free-form rationale paragraph, the producer names each open choice it
    considered and why it is not an operator decision. ``deferred_to_plan``
    candidates are carried into the plan phase as pre-seeded candidates
    the planner must register or disposition; deferral becomes a
    handoff, not a disappearance.
    """

    question: str = Field(..., description="The candidate decision, phrased as a question")
    disposition: str = Field(
        ...,
        description=(
            "Why this is not registered: 'not_operator_grade' (a design "
            "call the planner/implementer owns) or 'deferred_to_plan' "
            "(potentially operator-grade, better asked once the plan is "
            "concrete; the plan phase must pick it up)"
        ),
    )
    why: str = Field(..., description="One sentence justifying the disposition")


class DeferredResolution(BaseModel):
    """A refine-deferred question's plan-phase resolution (#3564).

    Refine's ``deferred_to_plan`` candidates arrive in the plan prompt
    with a stable ``dq-<hash>`` id. The plan producer echoes each id here
    with what became of it: ``registered`` (possibly reframed) as a
    ``cq-N``, or ``not_operator_grade`` because the design dissolved the
    choice. The propose-time gate recomputes the ids from the refine
    attestation and NACKs any deferred question left unaccounted — the
    echo is what makes exact matching safe while the planner freely
    reframes the question text.
    """

    deferred_id: str = Field(
        ...,
        description=(
            "The dq-<hash> id from the 'Deferred from refine' section of "
            "the plan prompt, copied verbatim"
        ),
    )
    resolution: str = Field(
        ...,
        description=(
            "What became of the question: 'registered' (as a cq-N, "
            "possibly reframed) or 'not_operator_grade' (the design "
            "dissolved it into a call the planner/implementer owns)"
        ),
    )
    cq: str = Field(
        default="",
        description="The cq-N id it was registered as (required when resolution='registered')",
    )
    why: str = Field(
        default="",
        description=(
            "How the design dissolved the question (required when resolution='not_operator_grade')"
        ),
    )


class DecisionSurfacingAttestation(BaseModel):
    """Decision-ledger attestation for refine/plan producers (#3390, #3526).

    Refine/plan producers own the phase's operator-decision surface: every
    HITL decision they identify must be registered via ``egg-contract
    add-decision`` / ``mcp__sdlc__register_open_question``, and their
    proposal must attest to the resulting ledger — either the list of
    registered ``cq-N`` ids or an explicit rationale for why the phase
    deliberately raises none. This is what makes "0 decisions at the
    gate" trustworthy as *deliberately none* rather than *forgot to
    register* (the motivating failure of #3390).

    The explicit-none form additionally requires ``candidates_considered``
    (#3526): the enumerated open choices the producer weighed and
    dispositioned away. A single free-form paragraph proved trivially
    satisfiable (surfaced decisions collapsed to near zero within weeks
    of the rationale form landing), so the empty ledger must now name
    what was considered.

    The shape is validated here (via the shared
    ``decision_attestation_errors`` helper, so this model and the
    propose-time signal validator cannot drift) and enforced regardless
    of the pipeline's attestation strictness: an incoherent ledger claim
    is malformed input, not a relaxed-mode allowance. Cross-checking the
    attested ids against the contract's registered decisions requires
    contract access and lives in the propose-time signal validator
    (``routes/signals/_validation.py``), not here.
    """

    decisions_registered: list[str] = Field(
        default_factory=list,
        description=(
            "Every cq-N decision id this producer registered for the "
            "current phase (the ids returned by `egg-contract add-decision`)"
        ),
    )
    no_decisions_rationale: str = Field(
        default="",
        description=(
            "Why this phase deliberately raises no operator decisions. "
            "Required when decisions_registered is empty — an explicit "
            "empty ledger, never an omission."
        ),
    )
    candidates_considered: list[ConsideredCandidate] = Field(
        default_factory=list,
        description=(
            "The decision candidates weighed and dispositioned away "
            "rather than registered (#3526). Required (>= 1 entry) with "
            "no_decisions_rationale; optional alongside "
            "decisions_registered."
        ),
    )
    deferred_resolutions: list[DeferredResolution] = Field(
        default_factory=list,
        description=(
            "Plan producers only (#3564): one entry per refine-deferred "
            "dq-<hash> id surfaced in the plan prompt, recording whether "
            "it was registered (as a cq-N) or dissolved "
            "(not_operator_grade). The propose-time gate NACKs a plan "
            "proposal whose deferred questions are not all covered."
        ),
    )

    @model_validator(mode="after")
    def validate_ledger_shape(self) -> DecisionSurfacingAttestation:
        """Require exactly one of decisions_registered / no_decisions_rationale."""
        from egg_contracts.decisions import decision_attestation_errors

        errors = decision_attestation_errors(
            self.decisions_registered,
            self.no_decisions_rationale,
            [c.model_dump() for c in self.candidates_considered],
            [d.model_dump() for d in self.deferred_resolutions],
        )
        if errors:
            raise ValueError("Decision-ledger attestation invalid: " + " ".join(errors))
        return self


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
    # Refine/plan producers attest their decision ledger (#3390). The
    # simplifier is deliberately absent: its human-focused companion
    # summarizes the upstream draft and owns no decision surface.
    "refiner": DecisionSurfacingAttestation,
    "task_planner": DecisionSurfacingAttestation,
    "architect": DecisionSurfacingAttestation,
    "risk_analyst": DecisionSurfacingAttestation,
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
    no_changes_needed: bool = Field(
        default=False,
        description=(
            "Generic no-op propose (#3027). True when this producer has no "
            "work to contribute in this slice — it inspected the slice/diff "
            "and either has no assigned task or its domain is not impacted. "
            "A no-op proposal carries no artifacts and no commit_sha (the "
            "``artifacts`` / ``commit_sha`` validators are skipped); it still "
            "counts as 'proposed' so the global zero-proposal guard clears, "
            "and reviewers treat it as a non-blocking no-op (they neither "
            "review nor NACK it). Works for any producer role — no per-role "
            "flag needed. Requires ``no_changes_reason``."
        ),
    )
    no_changes_reason: str = Field(
        default="",
        description=(
            "Why this producer has no work in this slice (e.g. 'no assigned "
            "task in this slice; coder diff touches no documented surface'). "
            "Required when ``no_changes_needed=true`` so the no-op is a "
            "justified attestation, not a silent skip."
        ),
    )

    @model_validator(mode="after")
    def validate_artifacts_non_empty(self) -> ProposalPayload:
        """Reject proposals with no artifact references.

        Skipped for a no-op propose (#3027): a producer with no work has
        nothing to reference. The no-op is justified by ``no_changes_reason``
        instead (validated below).
        """
        if self.no_changes_needed:
            return self
        if not self.artifacts:
            raise ValueError(
                "Proposal must reference at least one artifact (file path, commit SHA, etc.)"
            )
        return self

    @model_validator(mode="after")
    def validate_no_changes_reason(self) -> ProposalPayload:
        """Require a reason for a no-op propose so it stays a justified attestation (#3027)."""
        if self.no_changes_needed and not self.no_changes_reason.strip():
            raise ValueError(
                "no_changes_needed=true requires a non-empty no_changes_reason "
                "explaining why this producer has no work in this slice."
            )
        return self

    @model_validator(mode="after")
    def validate_no_changes_blocked_mutual_exclusion(self) -> ProposalPayload:
        """Reject ``no_changes_needed`` combined with ``tests_execution_blocked`` (#3027 follow-up).

        The retired per-role ``no_test_changes_needed`` flag had a paired
        mutual-exclusion check against ``tests_execution_blocked`` —
        "blocked" means the configured checks could not run; "no changes
        needed" means they ran and there was nothing to author. Folding
        per-role flags into the proposal-level ``no_changes_needed``
        dropped the check; restore it at the proposal layer so the
        incoherent combination is rejected before the strict validator
        is short-circuited by RELAXED mode.
        """
        if self.no_changes_needed and bool(self.attestation.get("tests_execution_blocked", False)):
            raise ValueError(
                "Proposal has both no_changes_needed=true and "
                "attestation.tests_execution_blocked=true — these are "
                "mutually exclusive. 'no_changes_needed' means the producer "
                "has no work in this slice; 'tests_execution_blocked' means "
                "the tester tried to run the configured checks and could not. "
                "Pick one: if you have no work, drop tests_execution_blocked; "
                "if your checks were blocked, drop no_changes_needed and "
                "report the blocked-execution path."
            )
        return self

    @model_validator(mode="after")
    def validate_commit_sha_present(self) -> ProposalPayload:
        """Require commit_sha so reviewers can verify pushed code (#1473).

        Skipped for a no-op propose (#3027): there is no commit to point at.
        """
        if self.no_changes_needed:
            return self
        if not self.commit_sha:
            raise ValueError(
                "Proposal must include commit_sha referencing a pushed commit. "
                "Commit and push your work before proposing consensus."
            )
        return self

    @model_validator(mode="after")
    def validate_commit_sha_format(self) -> ProposalPayload:
        """Reject commit_sha values containing shell metacharacters (#3076).

        The producer-supplied SHA is interpolated into rendered shell
        commands in the reviewer's event prompt (the per-producer
        ``git log <sha>..<proposal_sha>`` and ``git show <sha>:<path>``
        renders in ``orchestrator/routes/event_prompt.py``).
        ``_extract_proposal_sha_for_producer`` gates the read path with
        a stricter hex-only regex, but downstream consumers
        (``orchestrator/peer_consensus.py``,
        ``orchestrator/routes/signals.py``,
        ``orchestrator/routes/pipelines.py``) read
        ``_proposal_commit_shas`` directly without revalidating — so
        enforce a shell-safe baseline at the writer too: only
        ``[A-Za-z0-9_]`` permitted, 7-64 chars. This rejects every
        dangerous form (whitespace, ``;``, ``$(…)``, ``..``, ranges)
        while accepting reconstruction sentinels like
        ``RECONSTRUCTED_NO_SHA`` that downstream code keys on. Skipped
        for a no-op propose (#3027): ``commit_sha`` is empty by design
        there.

        Asymmetric regex with
        ``orchestrator/routes/event_prompt.py::_extract_proposal_sha_for_producer``
        is intentional: the strict hex-only check there is the
        shell-interpolation boundary (rejects sentinels before they
        reach a rendered ``git`` command), while this loose
        alphanumeric+underscore check is the writer-side baseline
        (admits sentinels so they can round-trip through
        ``_proposal_commit_shas`` to non-shell consumers). Do not
        unify — tightening this regex breaks the sentinel round-trip;
        loosening the reader regex re-opens the shell-injection gap.

        Relies on ``validate_commit_sha_present`` running first
        (pydantic ``model_validator(mode="after")`` honours definition
        order) to reject empty non-no-op proposals before this check
        sees them; the ``not self.commit_sha`` guard below is
        defence-in-depth and unreachable in practice.
        """
        if self.no_changes_needed or not self.commit_sha:
            return self
        if not _COMMIT_SHA_PATTERN.fullmatch(self.commit_sha):
            raise ValueError(
                "Proposal commit_sha must be 7-64 alphanumeric/underscore "
                "characters (no shell metacharacters); got "
                f"{self.commit_sha!r}."
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
            # Strict validation only runs for real proposals. A producer
            # with no work in the slice does a generic no-op propose
            # (``ProposalPayload.no_changes_needed=true``, #3027), which the
            # caller validates in RELAXED mode — so we never reach here for
            # a no-op and don't need a per-role no-op escape hatch.
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
            elif instance.tests_run == 0:
                raise ValueError(
                    "Tester attestation requires tests_run > 0 in strict mode. "
                    "If the slice warrants no new tests (pure refactor / doc-only / "
                    "no behavior change), submit a no-op propose instead: set "
                    "no_changes_needed=true with a non-empty no_changes_reason on "
                    "the proposal."
                )
            # Require checks_passed when the tester ran the configured
            # checks. Skip only when tests_execution_blocked since the
            # tester may not have been able to run any checks (issues
            # #1459, #1467).
            if not instance.tests_execution_blocked and not instance.checks_passed:
                raise ValueError(
                    "Tester attestation requires checks_passed to list the checks that "
                    "passed (e.g. ['lint', 'test']). Only include checks that actually "
                    "passed — do not include checks that failed."
                )
        elif role == "documenter" and isinstance(instance, DocumenterAttestation):
            # Strict validation only runs for real proposals; a documenter
            # with no doc surface to touch submits a generic no-op propose
            # (``ProposalPayload.no_changes_needed=true``, #3027) validated
            # in RELAXED mode, so we never reach here for a no-op.
            if not instance.sections_updated:
                raise ValueError(
                    "Documenter attestation requires at least one section "
                    "updated in strict mode. If the slice warrants no doc "
                    "updates (pure refactor / test-only / no-doc-surface), "
                    "submit a no-op propose instead: set no_changes_needed=true "
                    "with a non-empty no_changes_reason on the proposal."
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
