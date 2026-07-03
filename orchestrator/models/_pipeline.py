"""Repository spec, the root Pipeline model, and slice-repo resolution.

Extracted from the monolithic ``models.py`` (#3450, slice-1 of #3312).
Every symbol re-exports through the ``models`` barrel (stable public API).
"""

import re
from datetime import UTC, datetime
from typing import Literal

from egg_contracts.models import PipelinePhase, Slice
from pydantic import BaseModel, Field, field_validator, model_validator

from ._config import PipelineConfig
from ._decisions import HITLDecision
from ._enums import DecisionStatus, PipelineMode, PipelineStatus
from ._execution import PhaseExecution


class RepoSpec(BaseModel):
    """One repository a pipeline operates in (#3393, multi-repo pipelines).

    A pipeline carries a *list* of these on ``Pipeline.repos`` — one entry
    per repository the run coordinates PRs across. Each repo pins its own
    ``base_branch`` (PRs in that repo are opened against it), so the repo
    set is genuinely list-shaped: there is no primary+secondary shape baked
    into the data model, and nothing may assume ``len(repos)`` ∈ {1, 2}.

    The ``repo`` is ``owner/name``-shaped — the full slug, not the bare
    short name — so two repos with the same short name under different
    owners stay distinct downstream (the worktree map is re-keyed by the
    full slug in slice 3).
    """

    repo: str = Field(..., description="Repository in owner/name format")
    base_branch: str | None = Field(
        default=None,
        description=(
            "Base branch PRs in this repo are opened against. When None, "
            "auto-detected from the repo's default branch (mirrors the "
            "legacy ``Pipeline.base_branch`` semantics)."
        ),
    )


class Pipeline(BaseModel):
    """Complete state of an SDLC pipeline execution.

    This is the root model stored in .egg-state/pipelines/{id}.json.
    It tracks all state needed to orchestrate a pipeline from issue to PR.
    """

    id: str = Field(
        ..., description="Unique pipeline ID (e.g., 'issue-496' or 'pipeline-a1b2c3d4')"
    )
    issue_number: int | None = Field(default=None, ge=1, description="GitHub issue number")
    repo: str | None = Field(default=None, description="Repository in owner/name format")
    branch: str | None = Field(default=None, description="Work branch name")
    base_branch: str | None = Field(
        default=None,
        description="Base branch for PR creation. When None, auto-detected from repo's default branch.",
    )
    repos: list[RepoSpec] = Field(
        default_factory=list,
        description=(
            "The full list of repositories this pipeline coordinates PRs "
            "across (#3393, multi-repo pipelines). List-shaped end to end — "
            "an arbitrary number of repos, each with its own ``base_branch``; "
            "no two-repo special case and no primary+secondary shape is baked "
            "in. The legacy singleton ``repo``/``base_branch`` scalars above "
            "are kept in sync by ``_sync_repos_and_legacy_singleton``: a "
            "pipeline persisted before this field existed synthesizes "
            "``repos=[RepoSpec(repo, base_branch)]`` from the singleton on "
            "load, and ``repos[0]`` is mirrored back onto the scalars so "
            "legacy readers keep working until slice 3 rewires them. Use the "
            "``primary_repo`` property (not ``repos[0]``) for naming/"
            "defaulting; use ``resolve_slice_repo`` to resolve a slice's repo."
        ),
    )
    prompt: str | None = Field(default=None, description="User prompt for prompt-driven pipelines")
    status: PipelineStatus = Field(
        default=PipelineStatus.PENDING, description="Overall pipeline status"
    )
    current_phase: PipelinePhase = Field(default=PipelinePhase.REFINE, description="Current phase")
    config: PipelineConfig = Field(
        default_factory=PipelineConfig, description="Pipeline configuration"
    )
    phases: dict[str, PhaseExecution] = Field(
        default_factory=dict, description="Phase execution state by phase name"
    )
    decisions: list[HITLDecision] = Field(default_factory=list, description="HITL decisions")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="When pipeline was created"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Last update time"
    )
    contract_synced: bool = Field(default=True, description="Whether state is synced with contract")
    network_mode: str | None = Field(
        default=None,
        description="Network mode for spawned containers: 'public', 'private', or None (auto from pipeline mode)",
    )
    mode: PipelineMode = Field(
        default=PipelineMode.ISSUE,
        description="Pipeline execution mode (currently only 'issue' for standard SDLC)",
    )
    pr_number: int | None = Field(
        default=None,
        ge=1,
        description="Number of the context PR opened by this pipeline at the "
        "plan→implement boundary (#2777) — used by the #1557 reverse-index "
        "in-flight detector. None until the context PR is opened, and for "
        "local-mode pipelines that have no remote.",
    )
    pr_head_sha: str | None = Field(
        default=None,
        description="Head commit SHA of the context PR opened at the "
        "plan→implement boundary (#2777), captured when the PR is opened. "
        "None until the context PR is opened, and for local-mode pipelines "
        "that have no remote.",
    )

    @field_validator("pr_head_sha")
    @classmethod
    def _validate_pr_head_sha(cls, v: str | None) -> str | None:
        if v is not None and v == "":
            return None
        if v is not None and not re.fullmatch(r"[0-9a-f]{7,40}", v):
            raise ValueError("pr_head_sha must be a 7-40 char hex string")
        return v

    has_contract: bool = Field(
        default=True,
        description="Whether this pipeline has an upstream SDLC contract "
        "(plan/refine artifacts, .egg-state/contracts/<issue>.json). "
        "Controls whether the implement-phase reviewer roster includes "
        "reviewer_contract. Default True for standard issue-mode pipelines.",
    )
    error: str | None = Field(default=None, description="Error if failed")
    analysis: str | None = Field(
        default=None,
        max_length=200_000,
        description="Pre-generated analysis for short flow pipelines (written to drafts on first run)",
    )
    plan: str | None = Field(
        default=None,
        max_length=200_000,
        description="Pre-generated plan with yaml-tasks appendix for short flow pipelines "
        "(written to drafts and parsed into contract on first run)",
    )
    source_branch: str | None = Field(
        default=None,
        description="Source branch to read prior-run artifacts from (plan, analysis). "
        "When set, the orchestrator reads drafts from this branch via git show "
        "instead of requiring inline content.",
    )
    source_artifact_prefix: str | None = Field(
        default=None,
        description="Explicit prefix for draft filenames on the source branch "
        "(e.g. 'issue-1570-v3'). Overrides the default pipeline_id-based "
        "prefix resolution when reading artifacts from source_branch.",
    )
    run_epoch: datetime | None = Field(
        default=None,
        description="Thread ownership epoch — bumped on restart_phase and start_pipeline "
        "recovery so lingering _run_pipeline threads detect the change and exit. "
        "Separate from created_at which is user-facing.",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Optimistic locking version (incremented on each save)",
    )
    jira_ticket: str | None = Field(
        default=None,
        description="Optional Atlassian Jira ticket key (e.g. 'ENG-1234') the "
        "pipeline is working against. Advisory only — exported to the sandbox "
        'as EGG_JIRA_TICKET so agents can call `jira ticket get "$EGG_JIRA_TICKET"` '
        "without hard-coding a key. The gateway does NOT use this for policy "
        "gating; only the project allowlist in config/context-filters.yaml "
        "can authorise a Jira call (issue #1556 refine decision #9).",
    )
    # Jira-epic SDLC support (issue #1557). When ``is_epic`` is true the
    # orchestrator schedules an APPLY phase after every HITL approval so
    # the APPLIER role can drive Jira mutations (epic-Description writes,
    # child creates, link creates, ``Won't Do`` transitions). ``pipeline_
    # mode`` distinguishes fresh-epic (no children yet) from reassess
    # (existing children to classify). Both default to falsy values so
    # contracts written before #1557 load with stable shape.
    is_epic: bool = Field(
        default=False,
        description=(
            "True when ``jira_ticket`` resolves to a Jira issue with "
            "``issuetype.name == 'Epic'`` (or the operator passed "
            "``mode='fresh' | 'reassess'`` to ``submit_task``). The "
            "orchestrator inspects this flag to decide whether to "
            "insert the APPLY phase between PLAN and IMPLEMENT — "
            "non-epic pipelines continue to advance PLAN → IMPLEMENT "
            "directly. Persisted alongside ``jira_ticket`` and round-"
            "trips through the state-store."
        ),
    )
    pipeline_mode: Literal["fresh", "reassess"] | None = Field(
        default=None,
        description=(
            "Epic-mode sub-classification (issue #1557). ``'fresh'`` "
            "when the epic has no children yet (the planner produces "
            "all-net-new ``jira_action='create'`` tasks); ``'reassess'`` "
            "when the epic already has children to classify "
            "(Done/In-flight/Updatable) and the planner emits a mix of "
            "``edit`` / ``create`` / ``wontdo`` / ``split-of`` / "
            "``consolidate-into`` actions. ``None`` for non-epic "
            "pipelines and for epic pipelines where the operator "
            "explicitly disabled APPLY (e.g. dry-run inspections)."
        ),
    )
    pr_url: str | None = Field(
        default=None,
        description=(
            "Full URL of the context PR opened by this pipeline at the "
            "plan→implement boundary (#2777; #1557 slice-2 — reverse-index "
            "in-flight detection). Populated alongside ``pr_number`` when "
            "the context PR is opened; consumed by the reassess sweep's "
            "in-flight classifier so existing children with an open PR "
            "aren't re-mutated without operator confirmation. ``None`` "
            "until the context PR is opened, and for local-mode pipelines "
            "that have no remote."
        ),
    )

    @field_validator("jira_ticket")
    @classmethod
    def _validate_jira_ticket(cls, v: str | None) -> str | None:
        """Permit either None or a standard Atlassian ticket key."""
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("jira_ticket must be a string")
        trimmed = v.strip()
        if trimmed == "":
            return None
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", trimmed):
            raise ValueError("jira_ticket must match '<PROJECT>-<number>' (e.g. 'ENG-1234')")
        return trimmed

    @field_validator("pr_url")
    @classmethod
    def _validate_pr_url(cls, v: str | None) -> str | None:
        """Permit either None or a non-empty HTTPS URL string.

        Kept deliberately permissive: the orchestrator stamps whatever
        the GitHub API returns for the PR's ``html_url``, and we don't
        want a regex tightening to break older contracts that captured
        a slightly different shape (e.g. http→https redirect).
        """
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("pr_url must be a string")
        trimmed = v.strip()
        if trimmed == "":
            return None
        if not (trimmed.startswith("http://") or trimmed.startswith("https://")):
            raise ValueError("pr_url must be an http(s) URL")
        return trimmed

    def get_phase_execution(self, phase: PipelinePhase) -> PhaseExecution:
        """Get or create phase execution state."""
        if phase.value not in self.phases:
            self.phases[phase.value] = PhaseExecution(phase=phase)
        return self.phases[phase.value]

    def get_pending_decisions(self) -> list[HITLDecision]:
        """Get all pending HITL decisions."""
        return [d for d in self.decisions if d.status == DecisionStatus.PENDING]

    def add_decision(
        self,
        question: str,
        options: list[str] | None = None,
        decision_type: Literal["phase_gate", "choice", "feedback"] = "choice",
        questions: list[dict[str, str]] | None = None,
        phase: PipelinePhase | None = None,
        content_changed: bool | None = None,
    ) -> HITLDecision:
        """Add a new HITL decision request."""
        decision_id = f"decision-{len(self.decisions) + 1}"
        decision = HITLDecision(
            id=decision_id,
            question=question,
            options=options or [],
            decision_type=decision_type,
            questions=questions or [],
            phase=phase,
            content_changed=content_changed,
        )
        self.decisions.append(decision)
        self.updated_at = datetime.now(UTC)
        return decision

    def resolve_decision(self, decision_id: str, resolution: str) -> HITLDecision | None:
        """Resolve a HITL decision."""
        for decision in self.decisions:
            if decision.id == decision_id and decision.status == DecisionStatus.PENDING:
                decision.status = DecisionStatus.RESOLVED
                decision.resolution = resolution
                decision.resolved_at = datetime.now(UTC)
                self.updated_at = datetime.now(UTC)
                return decision
        return None

    @model_validator(mode="after")
    def _sync_repos_and_legacy_singleton(self) -> Pipeline:
        """Keep ``repos`` and the legacy ``repo``/``base_branch`` in sync (#3393).

        Back-compat bridge for the multi-repo migration. Runs at every load
        (``mode="after"``) and is idempotent:

        * **Synthesize (legacy → list):** when ``repos`` is empty but the
          legacy singleton ``repo`` is set — a pipeline persisted before the
          ``repos`` field existed — synthesize
          ``repos=[RepoSpec(repo=self.repo, base_branch=self.base_branch)]``
          so list-shaped consumers see the repo.
        * **Mirror (list → legacy):** when ``repos`` is non-empty, mirror
          ``repos[0]`` back onto the legacy ``repo``/``base_branch`` scalars
          so legacy readers (and the three ``repos[0]`` collapse sites that
          slice 3 has not yet rewired) keep working.

        No behavioural change for N=1 pipelines: a single-repo pipeline's
        singleton and its one-element ``repos`` list agree after this runs.
        A repo-less pipeline (local mode, ``repo is None``) is left untouched
        — ``repos`` stays empty and the scalars stay ``None``.

        The absent-``Slice.repo``⇒primary default is NOT resolved here; that
        lives in ``resolve_slice_repo`` (a slice needs the pipeline as a
        second input, which only the orchestrator layer has).
        """
        if not self.repos:
            if self.repo is not None:
                self.repos = [RepoSpec(repo=self.repo, base_branch=self.base_branch)]
        else:
            primary = self.repos[0]
            self.repo = primary.repo
            self.base_branch = primary.base_branch
        return self

    @property
    def primary_repo(self) -> str | None:
        """The pipeline's primary repo — ``repos[0].repo`` (#3393).

        The INTENTIONAL named-primary accessor for naming and defaulting.
        Explicitly NOT one of the three ``repos[0]`` collapse sites removed
        in slice 3: those collapse the agent-facing repo *set* to a single
        repo, discarding the others; this exposes a named primary while the
        full ``repos`` list stays available to every other consumer.

        Returns ``None`` only for a repo-less pipeline (local mode with no
        ``repo`` and no ``repos``) — the ``_sync_repos_and_legacy_singleton``
        validator guarantees a singleton-only pipeline has a populated
        ``repos`` by the time this is read, so the ``self.repo`` fallback is
        just belt-and-braces for a pre-validation read.
        """
        if self.repos:
            return self.repos[0].repo
        return self.repo


def resolve_slice_repo(slice: Slice, pipeline: Pipeline) -> str | None:
    """Resolve the repository a slice operates in (#3393, multi-repo pipelines).

    This is the RUNTIME home of the absent-``Slice.repo``⇒primary default —
    the contract migration deliberately leaves ``Slice.repo`` as ``None`` on a
    legacy load because the ``Contract`` model has no repo field and cannot see
    the orchestrator ``Pipeline`` (risk_analyst R1 / architect aeb3528). This
    resolver takes the pipeline as a second input, so it is the correct layer
    to apply the default:

    * an explicit ``slice.repo`` (``owner/name``) wins, else
    * fall back to ``pipeline.primary_repo``.

    For an N=1 pipeline every slice's ``repo`` is ``None`` and this returns the
    single repo — behaviourally identical to the pre-multi-repo world.
    """
    return slice.repo if slice.repo else pipeline.primary_repo
