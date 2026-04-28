"""
Pydantic models for SDLC contract schema.

These models match the JSON schema defined in .egg/schemas/contract.schema.json
and provide validation and type safety for contract operations.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


class TaskStatus(StrEnum):
    """Status values for tasks."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


class SliceStatus(StrEnum):
    """Status values for slices.

    Renamed from ``PhaseStatus`` (#2137 — slice the implement phase). The
    enum values are preserved verbatim so on-disk contracts that wrote
    ``"pending"`` / ``"in_progress"`` / ``"complete"`` / ``"blocked"``
    continue to load. ``PhaseStatus`` remains as a backward-compat alias
    of this enum so existing imports (``from egg_contracts.models import
    PhaseStatus``) keep working during the transition window.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"


# Backward-compat alias — see ``SliceStatus`` docstring.
PhaseStatus = SliceStatus


class PipelinePhase(StrEnum):
    """Current pipeline phase."""

    REFINE = "refine"
    PLAN = "plan"
    IMPLEMENT = "implement"
    PR = "pr"


class DecisionType(StrEnum):
    """Types of decisions."""

    HITL = "hitl"
    AUTO = "auto"


class CheckStatus(StrEnum):
    """Status values for check results."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class HumanReviewMechanism(StrEnum):
    """Mechanism for human review in a phase."""

    ISSUE_CHECKBOX = "ISSUE_CHECKBOX"
    PR_REVIEW = "PR_REVIEW"


class AuditAction(StrEnum):
    """Types of audit actions."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    TRANSITION = "transition"


class AuditRole(StrEnum):
    """Roles for audit entries."""

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    HUMAN = "human"
    SYSTEM = "system"


class IssueInfo(BaseModel):
    """Issue metadata."""

    number: int = Field(..., ge=1, description="GitHub issue number")
    title: str = Field(..., min_length=1, description="Issue title")
    url: str = Field(..., description="Issue URL")


class AcceptanceCriterion(BaseModel):
    """Top-level acceptance criterion."""

    id: str = Field(..., pattern=r"^ac-[0-9]+$", description="Unique identifier")
    description: str = Field(..., min_length=1, description="Human-readable description")
    verified: bool = Field(default=False, description="Whether verified by reviewer")


class ReviewFeedback(BaseModel):
    """Feedback from reviewer on a task."""

    timestamp: datetime = Field(..., description="When feedback was given")
    task_id: str = Field(..., description="Task this feedback applies to")
    feedback: str = Field(..., min_length=1, description="Reviewer feedback")
    status: TaskStatus | None = Field(default=None, description="Status assigned by reviewer")


def _normalize_commit(v: Any) -> str | None:
    """Normalize commit SHA values: treat None and empty string as None."""
    if v is None or v == "":
        return None
    return str(v)


def _normalise_slice_id(value: str) -> str:
    """Return the canonical ``slice-<N>`` form of a slice/phase ID.

    Helper added in #2137 to keep ``get_slice`` / ``get_phase`` and any
    DAG-edge comparisons agnostic to whether the ID was written under
    the legacy ``phase-<N>`` shape or the canonical ``slice-<N>``
    shape. Returns the input unchanged for non-matching strings.
    """
    if isinstance(value, str) and value.startswith("phase-"):
        return "slice-" + value[len("phase-") :]
    return value


class TaskGap(BaseModel):
    """Tester→coder coverage-gap handoff record.

    Added in iteration 2 of the agent-facing MCP tools (#1917) so the
    tester role can structure gap handoffs as first-class contract
    records instead of freeform NACK reasons.  Written by
    :func:`egg_agent_tools.handlers.task.task_mark_gap` onto
    ``Task.gaps``; the gateway's existing contract/mutate path enforces
    role authorization.
    """

    id: str = Field(
        ...,
        pattern=r"^gap-[0-9]+$",
        description="Unique gap identifier of the form 'gap-<N>'",
    )
    from_role: str = Field(..., min_length=1, description="Agent role that recorded the gap")
    to_role: str = Field(..., min_length=1, description="Target role (usually 'coder')")
    description: str = Field(..., min_length=1, description="Gap description")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the gap was recorded (ISO-8601 UTC)",
    )
    resolved: bool = Field(default=False, description="Set True when the gap is addressed")


class Task(BaseModel):
    """A task within a phase."""

    id: str = Field(
        ...,
        pattern=r"^task-[0-9]+(-[0-9]+)?$",
        description="Unique task identifier (task-N or task-P-N format)",
    )
    description: str = Field(..., min_length=1, description="Task description")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Task status")
    commit: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA",
    )
    checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^ckpt-[a-f0-9]{8,16}$",
        description="Checkpoint ID associated with this task's commit",
    )
    notes: str = Field(default="", description="Implementation notes")
    acceptance_criteria: str = Field(default="", description="Acceptance criteria")
    files_affected: list[str] = Field(default_factory=list, description="Files affected")
    # Validation is intentionally deferred to the parser/schema layers so that
    # new roles can be added without a model change — the JSON schema and
    # plan_parser.py are the authoritative validators.
    role: str | None = Field(
        default=None,
        description="Execution role assigned to this task (coder, tester, or documenter)",
    )
    review_cycles: int = Field(default=0, ge=0, description="Number of review cycles")
    max_cycles: int = Field(default=3, ge=1, description="Max cycles before escalation")
    escalated: bool = Field(default=False, description="Whether escalated")
    gaps: list[TaskGap] = Field(
        default_factory=list,
        description=(
            "Coverage-gap records recorded by the tester role for the coder; "
            "defaults to an empty list so contracts written before iteration 2 "
            "load with a stable shape."
        ),
    )

    @field_validator("commit", mode="before")
    @classmethod
    def validate_commit(cls, v: Any) -> str | None:
        return _normalize_commit(v)


class Slice(BaseModel):
    """An implementation slice containing tasks.

    Renamed from ``Phase`` in #2137 to support the slice-DAG implement
    model: each slice is an independent unit (its own branch, agent
    team, BRC consensus, and PR). The on-disk schema accepts either
    ``slice-<N>`` IDs (canonical) or legacy ``phase-<N>`` IDs (the
    loader migration shim rewrites the latter to the former). The
    backward-compat alias ``Phase = Slice`` is exported below so older
    imports keep working during the transition window.
    """

    id: str = Field(
        ...,
        pattern=r"^(?:slice|phase)-[0-9]+$",
        description=(
            "Unique slice identifier — canonical ``slice-<N>``; "
            "``phase-<N>`` is accepted for backward compatibility "
            "with pre-#2137 contracts."
        ),
    )
    name: str = Field(..., min_length=1, description="Human-readable slice name")
    status: SliceStatus = Field(default=SliceStatus.PENDING, description="Slice status")
    review_cycles: int = Field(default=0, ge=0, description="Number of review cycles")
    max_cycles: int = Field(default=3, ge=1, description="Max cycles before escalation")
    escalated: bool = Field(default=False, description="Whether escalated")
    escalation_reason: str | None = Field(default=None, description="Reason for escalation")
    tasks: list[Task] = Field(default_factory=list, description="Tasks in this slice")
    dependencies: list[str] = Field(
        default_factory=list,
        description=(
            "Slice IDs this slice depends on (e.g., ['slice-1', 'slice-2']). "
            "After the #2137 forest constraint, each slice has at most one "
            "DAG parent — ingestion validates this."
        ),
    )
    serialized_chain_order: list[str] = Field(
        default_factory=list,
        description=(
            "Planner-emitted ordering for would-be multi-parent slices "
            "(#2137). When the planner identifies a slice that would "
            "naturally have >1 parents, it serialises the upstream "
            "slices into a chain and records the chosen order here on "
            "the downstream slice. Empty for slices with ≤1 natural "
            "parent."
        ),
    )
    parent_branch_at_creation: str | None = Field(
        default=None,
        description=(
            "Git branch the slice's integration branch was forked off "
            "of when its worktree was provisioned (#2137 TASK-4-2). "
            "Root slices record the pipeline branch (``egg/issue-N``); "
            "child slices record the parent slice's integration branch. "
            "Read by the stacked-PR reconciler (TASK-5-3) when the "
            "parent's branch has been deleted by a PR merge so it can "
            "compute the correct rebase target. ``None`` for slices "
            "that have not yet been provisioned."
        ),
    )
    commit: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA linked to this slice",
    )
    review_feedback: list[ReviewFeedback] = Field(
        default_factory=list, description="Feedback from reviewer"
    )

    @field_validator("commit", mode="before")
    @classmethod
    def validate_commit(cls, v: Any) -> str | None:
        return _normalize_commit(v)


# Backward-compat alias — see ``Slice`` docstring. ``Phase`` was the
# original name pre-#2137; new code should reference ``Slice`` directly.
Phase = Slice


class DecisionOption(BaseModel):
    """An option for a decision."""

    id: str = Field(..., description="Option identifier")
    label: str = Field(..., description="Option label")
    description: str | None = Field(default=None, description="Option description")


class Decision(BaseModel):
    """A HITL decision point."""

    id: str = Field(..., pattern=r"^decision-[0-9]+$", description="Unique decision identifier")
    question: str = Field(..., min_length=1, description="The decision question")
    type: DecisionType = Field(..., description="Decision type")
    phase: PipelinePhase | None = Field(
        default=None,
        description="Pipeline phase this decision belongs to. Used to block "
        "phase-complete while the phase still has unresolved decisions.",
    )
    options: list[DecisionOption] = Field(default_factory=list, description="Available options")
    resolved: bool = Field(default=False, description="Whether resolved")
    resolution: str | None = Field(default=None, description="Selected resolution")
    resolved_by: str | None = Field(default=None, description="Who resolved")
    resolved_at: datetime | None = Field(default=None, description="When resolved")
    debounce_until: datetime | None = Field(default=None, description="Debounce expiration")


class PRMetadata(BaseModel):
    """Planner-generated PR metadata: title, description, test plan, and manual steps."""

    title: str = Field(..., min_length=1, description="PR title (recommended max 70 chars)")
    description: str = Field(default="", description="PR description/body")
    test_plan: str = Field(
        default="",
        description="Test plan: automated tests and manual verification steps",
    )
    manual_steps: str = Field(
        default="",
        description="Manual pre/post-merge steps (migrations, config changes, etc.)",
    )
    deferred_actions: list[str] = Field(
        default_factory=list,
        description=(
            "Durable record of pre-merge obligations from conditional ACKs "
            "(#1998/#2004). Each entry is a human-readable line (e.g. "
            "'reviewer_code: git mv legacy/x new/x'). Written when the 3-way "
            "HITL gate at complete_phase resolves as approve+accept, so "
            "obligations survive tracker teardown between phase close and "
            "PR creation."
        ),
    )


class CheckDefinition(BaseModel):
    """Definition of a check to run during a phase."""

    id: str = Field(
        ...,
        pattern=r"^check-[a-z0-9-]+$",
        description="Unique check identifier (e.g., check-lint)",
    )
    name: str = Field(..., min_length=1, description="Human-readable check name")
    script: str = Field(..., min_length=1, description="Script to run for this check")
    required: bool = Field(default=True, description="Whether this check must pass")
    retry_on_fail: bool = Field(default=False, description="Whether to retry on failure")
    max_retries: int = Field(default=0, ge=0, description="Maximum number of retries")


class CheckResult(BaseModel):
    """Result of running a check."""

    check_id: str = Field(
        ...,
        pattern=r"^check-[a-z0-9-]+$",
        description="ID of the check that was run",
    )
    status: CheckStatus = Field(..., description="Check result status")
    message: str = Field(default="", description="Human-readable result message")
    details: dict[str, Any] = Field(default_factory=dict, description="Additional details")
    fixable: bool = Field(default=False, description="Whether this failure can be auto-fixed")


class PhaseConfig(BaseModel):
    """Configuration for a pipeline phase."""

    checks: list[CheckDefinition] = Field(
        default_factory=list, description="Checks to run in this phase"
    )
    max_review_cycles: int = Field(
        default=3, ge=1, description="Max review cycles before escalation"
    )
    human_review_mechanism: HumanReviewMechanism = Field(
        default=HumanReviewMechanism.ISSUE_CHECKBOX,
        description="Mechanism for human review",
    )


class FeedbackQuestion(BaseModel):
    """A question for human feedback."""

    id: str = Field(..., pattern=r"^Q[0-9]+$", description="Unique question identifier (e.g., Q1)")
    question: str = Field(..., min_length=1, description="The question text")
    answer: str | None = Field(default=None, description="Human-provided answer (free-form text)")


class Feedback(BaseModel):
    """Feedback request for collecting open-ended questions from humans."""

    id: str = Field(
        ...,
        pattern=r"^feedback-[0-9]+$",
        description="Unique feedback identifier (e.g., feedback-1)",
    )
    phase: PipelinePhase | None = Field(
        default=None, description="Pipeline phase this feedback belongs to"
    )
    questions: list[FeedbackQuestion] = Field(
        ..., min_length=1, description="List of questions for the human"
    )
    submitted: bool = Field(default=False, description="Whether the feedback has been submitted")
    submitted_by: str | None = Field(
        default=None, description="GitHub username who submitted the feedback"
    )
    submitted_at: datetime | None = Field(
        default=None, description="When the feedback was submitted"
    )
    comment_id: int | None = Field(
        default=None, description="GitHub comment ID containing this feedback"
    )
    debounce_until: datetime | None = Field(
        default=None, description="Debounce expiration timestamp"
    )

    def get_question(self, question_id: str) -> FeedbackQuestion | None:
        """Get a specific question by ID."""
        for question in self.questions:
            if question.id == question_id:
                return question
        return None

    def get_unanswered_questions(self) -> list[FeedbackQuestion]:
        """Get all questions that haven't been answered."""
        return [q for q in self.questions if q.answer is None]

    def all_questions_answered(self) -> bool:
        """Check if all questions have been answered."""
        return all(q.answer is not None for q in self.questions)


class AuditEntry(BaseModel):
    """Audit log entry for contract modifications."""

    timestamp: datetime = Field(..., description="When the action occurred")
    actor: str = Field(..., description="Who performed the action")
    role: AuditRole = Field(..., description="Role of the actor")
    action: AuditAction = Field(..., description="Action performed")
    field_path: str = Field(..., description="JSON path of modified field")
    old_value: Any = Field(default=None, description="Previous value")
    new_value: Any = Field(default=None, description="New value")
    reason: str | None = Field(default=None, description="Reason for change")
    checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^ckpt-[a-f0-9]{8,16}$",
        description="Checkpoint ID if this entry relates to a commit with a checkpoint",
    )


class AgentExecutionStatus(StrEnum):
    """Status values for agent executions."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class AgentRoleType(StrEnum):
    """Agent role types for multi-agent orchestration."""

    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    # Plan-phase roles
    ARCHITECT = "architect"
    TASK_PLANNER = "task_planner"
    RISK_ANALYST = "risk_analyst"
    # Refine-phase roles
    REFINER = "refiner"
    # Reviewer roles
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
    REVIEWER_REFINE = "reviewer_refine"
    REVIEWER_PLAN = "reviewer_plan"


class AgentExecutionModel(BaseModel):
    """Tracks the execution state of a single agent.

    Used by the orchestrator to track which agents have run,
    their results, and any handoff data they produced.
    """

    role: AgentRoleType = Field(..., description="The agent role")
    phase_id: str | None = Field(
        default=None,
        description="Plan phase ID this execution belongs to (e.g., 'phase-1'). "
        "None for Tier 2 (role-only) keying.",
    )
    status: AgentExecutionStatus = Field(
        default=AgentExecutionStatus.PENDING, description="Current execution status"
    )
    started_at: datetime | None = Field(default=None, description="When agent started")
    completed_at: datetime | None = Field(default=None, description="When agent completed")
    commit: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{7,40}$",
        description="Git commit SHA if agent made changes",
    )
    checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^ckpt-[a-f0-9]{8,16}$",
        description="Checkpoint ID associated with agent's commit",
    )
    outputs: dict[str, Any] = Field(
        default_factory=dict, description="Handoff data produced by agent"
    )
    error: str | None = Field(default=None, description="Error message if failed")
    retry_count: int = Field(default=0, ge=0, description="Number of retry attempts")
    conflicts: list[str] = Field(
        default_factory=list, description="Files with unresolved merge conflicts"
    )


class Contract(BaseModel):
    """The complete SDLC contract."""

    schemaVersion: str = Field(  # noqa: N815
        default="1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    issue: IssueInfo | None = Field(default=None, description="Issue metadata")
    pipeline_id: str | None = Field(
        default=None,
        description=(
            "Canonical pipeline identifier — the on-disk contract key. "
            "For issue-driven pipelines this is ``issue-<N>`` (optionally "
            "``issue-<N>-<qualifier>``); for JIRA-ticket pipelines this is "
            "the ticket ID (e.g., ``KORE-1234``). Optional only for legacy "
            "contracts that predate the key unification."
        ),
    )
    current_phase: PipelinePhase = Field(
        default=PipelinePhase.REFINE, description="Current pipeline phase"
    )
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        default_factory=list, description="Top-level acceptance criteria"
    )
    # ``slices`` is the canonical field name post-#2137 (slice the implement
    # phase). The legacy alias ``phases`` is preserved as a Pydantic
    # validation alias so contract JSON written before the rename keeps
    # loading without an explicit migration step. See
    # ``Contract._migrate_phases_to_slices`` (model_validator) which also
    # handles the case where both ``slices`` and ``phases`` keys are
    # absent vs. present.
    slices: list[Slice] = Field(
        default_factory=list,
        description="Implementation slices (renamed from ``phases`` in #2137)",
    )
    # Stash of the legacy ``phases[]`` payload populated by the
    # ``_migrate_phases_to_slices`` model validator when a contract is
    # loaded from pre-#2137 JSON. Cleared on round-trip so a re-loaded
    # already-migrated contract does NOT re-run the migration. Private
    # attribute so it does not appear in serialised output.
    _legacy_phases: list[dict[str, Any]] | None = PrivateAttr(default=None)
    decisions: list[Decision] = Field(default_factory=list, description="HITL decisions")
    workflow_owner: str | None = Field(
        default=None,
        description="GitHub username of the user who initiated the SDLC workflow",
    )
    audit_log: list[AuditEntry] = Field(default_factory=list, description="Audit trail")
    refine_review_cycles: int = Field(
        default=0, ge=0, description="Number of refine phase review cycles"
    )
    refine_review_feedback: str = Field(default="", description="Feedback from last refine review")
    plan_review_cycles: int = Field(
        default=0, ge=0, description="Number of plan phase review cycles"
    )
    plan_review_feedback: str = Field(default="", description="Feedback from last plan review")
    pr: PRMetadata | None = Field(
        default=None, description="Planner-generated PR metadata for use during PR creation"
    )
    feedback: Feedback | None = Field(
        default=None, description="Active feedback request for collecting open-ended questions"
    )
    phase_configs: dict[PipelinePhase, PhaseConfig] | None = Field(
        default=None,
        description="Optional phase-specific configurations (overrides defaults)",
    )
    # Multi-agent orchestration fields
    agent_executions: list[AgentExecutionModel] = Field(
        default_factory=list,
        description="Execution state for each agent in multi-agent mode",
    )

    @model_validator(mode="wrap")
    @classmethod
    def _migrate_phases_to_slices(cls, data: Any, handler: Any) -> "Contract":
        """Translate legacy ``phases: [...]`` JSON to ``slices: [...]``.

        Added in #2137. Detects pre-rename contract JSON (no ``slices``
        key, ``phases`` key present) and:

        1. Copies ``phases[]`` into ``slices[]`` so the rename is a
           no-op for already-shipped contract files.
        2. Rewrites each item's ``id`` from ``phase-<N>`` to
           ``slice-<N>`` so the post-rename ID pattern matches.
        3. Rewrites ``dependencies[]`` entries the same way so the DAG
           edges keep resolving after the rename.
        4. Stashes the original ``phases[]`` payload on the private
           ``_legacy_phases`` attribute (after pydantic constructs the
           instance) so audit / migration tooling can link legacy log
           entries back during the transition window.

        On a brand-new ``slices: [...]`` JSON load (no ``phases`` key)
        the shim leaves the data untouched and ``_legacy_phases``
        remains ``None``. On a round-trip dump → reload of a migrated
        contract the dump only emits ``slices`` (the field name on the
        model), so the second load takes the no-op path and does NOT
        re-run the migration — which is precisely what the round-trip
        invariant in TASK-1-4 asserts. ``mode="wrap"`` is used so the
        validator can both transform input *and* set the private
        attribute on the constructed instance in one place.
        """
        if not isinstance(data, dict):
            return cast("Contract", handler(data))

        has_slices = "slices" in data
        has_phases = "phases" in data

        if has_slices or not has_phases:
            return cast("Contract", handler(data))

        legacy_phases = data.pop("phases")
        if not isinstance(legacy_phases, list):
            # Malformed input — restore for pydantic to surface the
            # error normally.
            data["phases"] = legacy_phases
            return cast("Contract", handler(data))

        migrated: list[Any] = []
        for entry in legacy_phases:
            if not isinstance(entry, dict):
                migrated.append(entry)
                continue
            new_entry = dict(entry)
            old_id = new_entry.get("id")
            if isinstance(old_id, str) and old_id.startswith("phase-"):
                new_entry["id"] = "slice-" + old_id[len("phase-") :]
            deps = new_entry.get("dependencies")
            if isinstance(deps, list):
                new_entry["dependencies"] = [
                    "slice-" + d[len("phase-") :]
                    if isinstance(d, str) and d.startswith("phase-")
                    else d
                    for d in deps
                ]
            migrated.append(new_entry)

        data["slices"] = migrated
        instance: Contract = cast("Contract", handler(data))
        instance._legacy_phases = legacy_phases
        return instance

    @model_validator(mode="after")
    def _require_issue_or_pipeline_id(self) -> "Contract":
        """At least one of issue or pipeline_id must be set."""
        if self.issue is None and self.pipeline_id is None:
            raise ValueError("At least one of 'issue' or 'pipeline_id' must be set")
        return self

    @property
    def phases(self) -> list[Slice]:
        """Backward-compat alias for ``slices`` (renamed in #2137).

        Existing call sites that read ``contract.phases`` continue to
        work; new code should reference ``contract.slices`` directly.
        Returns the live list, so mutations propagate to ``slices``.
        """
        return self.slices

    @phases.setter
    def phases(self, value: list[Slice]) -> None:
        """Backward-compat setter — writes through to ``slices``.

        Some contract-mutation paths assign ``contract.phases = [...]``
        wholesale (e.g., when re-populating from a parsed plan). The
        setter forwards the assignment so those paths keep working
        without each having to be updated to use ``slices`` directly.
        """
        self.slices = value

    @property
    def contract_key(self) -> str:
        """Return the canonical pipeline-id string used for file naming.

        Every contract is keyed by its pipeline_id (e.g., ``issue-1759``,
        ``issue-1759-v2``, or a JIRA-style identifier). For legacy contracts
        that predate the unification and only have ``issue`` populated, a
        canonical pipeline-id is synthesized from the issue number.
        """
        if self.pipeline_id is not None:
            return self.pipeline_id
        assert self.issue is not None
        return f"issue-{self.issue.number}"

    def get_task(self, phase_id: str, task_id: str) -> Task | None:
        """Get a specific task by slice/phase and task ID.

        Accepts either ``slice-<N>`` (canonical post-#2137) or
        ``phase-<N>`` (legacy) for ``phase_id``; the lookup matches both
        forms by normalising the prefix before comparison.
        """
        normalised = _normalise_slice_id(phase_id)
        for slice_ in self.slices:
            if _normalise_slice_id(slice_.id) == normalised:
                for task in slice_.tasks:
                    if task.id == task_id:
                        return task
        return None

    def get_phase(self, phase_id: str) -> Slice | None:
        """Backward-compat alias for ``get_slice`` (renamed in #2137).

        Accepts either ``slice-<N>`` (canonical) or ``phase-<N>``
        (legacy) IDs.
        """
        return self.get_slice(phase_id)

    def get_slice(self, slice_id: str) -> Slice | None:
        """Get a specific slice by ID.

        Accepts either ``slice-<N>`` (canonical) or ``phase-<N>``
        (legacy) IDs.
        """
        normalised = _normalise_slice_id(slice_id)
        for slice_ in self.slices:
            if _normalise_slice_id(slice_.id) == normalised:
                return slice_
        return None

    def get_decision(self, decision_id: str) -> Decision | None:
        """Get a specific decision by ID."""
        for decision in self.decisions:
            if decision.id == decision_id:
                return decision
        return None

    def get_agent_execution(self, role: AgentRoleType | str) -> AgentExecutionModel | None:
        """Get the execution state for an agent role.

        Args:
            role: The agent role (string or AgentRoleType enum)

        Returns:
            AgentExecutionModel for the role, or None if not found
        """
        if isinstance(role, str):
            role = AgentRoleType(role)
        for execution in self.agent_executions:
            if execution.role == role:
                return execution
        return None

    def get_pending_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that are pending execution.

        Returns:
            List of AgentExecutionModel with PENDING status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.PENDING]

    def get_running_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that are currently running.

        Returns:
            List of AgentExecutionModel with RUNNING status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.RUNNING]

    def get_completed_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that have completed (successfully).

        Returns:
            List of AgentExecutionModel with COMPLETE status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.COMPLETE]

    def get_failed_agents(self) -> list[AgentExecutionModel]:
        """Get all agents that have failed.

        Returns:
            List of AgentExecutionModel with FAILED status
        """
        return [ex for ex in self.agent_executions if ex.status == AgentExecutionStatus.FAILED]

    def all_agents_complete(self) -> bool:
        """Check if all agents have completed (successfully or skipped).

        Returns:
            True if all agents are in a terminal successful state
        """
        if not self.agent_executions:
            return True  # No agents means nothing to complete
        return all(
            ex.status in (AgentExecutionStatus.COMPLETE, AgentExecutionStatus.SKIPPED)
            for ex in self.agent_executions
        )
