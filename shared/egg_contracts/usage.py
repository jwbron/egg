"""
Pydantic models for token usage aggregates.

These models define the data structures for tracking token usage across
multiple dimensions: session, issue, workflow, and PR. Usage aggregates
are pre-computed JSON files stored in the egg/checkpoints/v1 orphaned
branch alongside checkpoint data.

Architecture:
    - UsageAggregate: Base model for token usage summaries
    - SessionUsage: Usage for a single session
    - IssueUsage: Aggregated usage for an issue (all sessions/checkpoints)
    - WorkflowUsage: Usage for a workflow run
    - PRUsage: Aggregated usage for a pull request
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

# Per-model pricing (USD per million tokens)
# Cache ratios per Anthropic docs: read = 0.1x input, write (5-min) = 1.25x input
MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "opus": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
        "cache_read": Decimal("0.50"),
        "cache_write": Decimal("6.25"),
    },
    "sonnet": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_read": Decimal("0.30"),
        "cache_write": Decimal("3.75"),
    },
    "haiku": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_read": Decimal("0.10"),
        "cache_write": Decimal("1.25"),
    },
}

# Default to opus pricing (most common model in the system)
DEFAULT_INPUT_COST_PER_MTOK = MODEL_PRICING["opus"]["input"]
DEFAULT_OUTPUT_COST_PER_MTOK = MODEL_PRICING["opus"]["output"]
DEFAULT_CACHE_READ_COST_PER_MTOK = MODEL_PRICING["opus"]["cache_read"]
DEFAULT_CACHE_WRITE_COST_PER_MTOK = MODEL_PRICING["opus"]["cache_write"]


def get_model_pricing(model: str | None) -> dict[str, Decimal]:
    """Look up pricing for a model alias or full model ID.

    Resolves full model IDs (e.g. 'claude-opus-4-5-20251101') to their
    base alias ('opus'). Falls back to opus pricing for unknown models.

    Expected inputs:
        - Short aliases: "opus", "sonnet", "haiku"
        - Full Anthropic model IDs: "claude-opus-4-5-20251101",
          "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"

    Note: Uses substring matching, so model IDs must contain exactly one of
    the known aliases (opus, sonnet, haiku) to match correctly.
    """
    if model is None:
        return MODEL_PRICING["opus"]
    model_lower = model.lower()
    for alias in MODEL_PRICING:
        if alias in model_lower:
            return MODEL_PRICING[alias]
    return MODEL_PRICING["opus"]


class TokenCounts(BaseModel):
    """Token counts across different categories."""

    input_tokens: int = Field(default=0, ge=0, description="Total input tokens")
    output_tokens: int = Field(default=0, ge=0, description="Total output tokens")
    cache_read_tokens: int = Field(default=0, ge=0, description="Tokens read from cache")
    cache_creation_tokens: int = Field(default=0, ge=0, description="Tokens written to cache")

    def total_tokens(self) -> int:
        """Calculate total tokens (input + output)."""
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenCounts") -> "TokenCounts":
        """Add another TokenCounts to this one, returning a new instance."""
        return TokenCounts(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )

    def calculate_cost(
        self,
        input_cost_per_mtok: Decimal = DEFAULT_INPUT_COST_PER_MTOK,
        output_cost_per_mtok: Decimal = DEFAULT_OUTPUT_COST_PER_MTOK,
        cache_read_cost_per_mtok: Decimal = DEFAULT_CACHE_READ_COST_PER_MTOK,
        cache_write_cost_per_mtok: Decimal = DEFAULT_CACHE_WRITE_COST_PER_MTOK,
        *,
        model: str | None = None,
    ) -> Decimal:
        """
        Calculate estimated cost in USD.

        If ``model`` is provided, its pricing overrides the individual
        cost parameters. Otherwise the explicit parameters (defaulting
        to opus pricing) are used.

        Args:
            input_cost_per_mtok: Cost per million input tokens
            output_cost_per_mtok: Cost per million output tokens
            cache_read_cost_per_mtok: Cost per million cache read tokens
            cache_write_cost_per_mtok: Cost per million cache write tokens
            model: Optional model name/alias to look up pricing for

        Returns:
            Estimated cost in USD as Decimal
        """
        if model is not None:
            pricing = get_model_pricing(model)
            input_cost_per_mtok = pricing["input"]
            output_cost_per_mtok = pricing["output"]
            cache_read_cost_per_mtok = pricing["cache_read"]
            cache_write_cost_per_mtok = pricing["cache_write"]

        mtok = Decimal("1000000")
        # Regular input (non-cached)
        regular_input = self.input_tokens - self.cache_read_tokens
        cost = Decimal(regular_input) / mtok * input_cost_per_mtok
        cost += Decimal(self.output_tokens) / mtok * output_cost_per_mtok
        cost += Decimal(self.cache_read_tokens) / mtok * cache_read_cost_per_mtok
        cost += Decimal(self.cache_creation_tokens) / mtok * cache_write_cost_per_mtok
        return cost


class CheckpointReference(BaseModel):
    """Reference to a checkpoint that contributed to usage."""

    checkpoint_id: str = Field(..., description="Checkpoint ID (ckpt-...)")
    commit_sha: str = Field(..., description="Associated commit SHA")
    created_at: datetime = Field(..., description="When checkpoint was created")


class UsageAggregate(BaseModel):
    """
    Base model for usage aggregates.

    Contains common fields for all usage aggregate types.
    """

    schemaVersion: str = Field(  # noqa: N815
        default="1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    tokens: TokenCounts = Field(default_factory=TokenCounts, description="Aggregated token counts")
    estimated_cost_usd: float = Field(default=0.0, ge=0.0, description="Estimated cost in USD")
    checkpoint_count: int = Field(default=0, ge=0, description="Number of checkpoints included")
    first_checkpoint_at: datetime | None = Field(
        default=None, description="Timestamp of first checkpoint"
    )
    last_checkpoint_at: datetime | None = Field(
        default=None, description="Timestamp of most recent checkpoint"
    )
    last_updated: datetime = Field(..., description="When this aggregate was last updated")

    def update_cost(self, model: str | None = None) -> None:
        """Recalculate estimated cost from token counts."""
        cost = self.tokens.calculate_cost(model=model)
        self.estimated_cost_usd = float(cost)


class SessionUsage(UsageAggregate):
    """
    Usage aggregate for a single session.

    Sessions are identified by the Claude Code session ID or container ID.
    """

    session_id: str = Field(..., description="Session identifier")
    container_id: str | None = Field(default=None, description="Container ID if in sandbox")
    agent_role: str | None = Field(default=None, description="Agent role (coder, tester, etc.)")
    model: str | None = Field(default=None, description="Model used")
    issue_number: int | None = Field(default=None, ge=1, description="Associated issue number")
    pr_number: int | None = Field(default=None, ge=1, description="Associated PR number")
    checkpoints: list[CheckpointReference] = Field(
        default_factory=list, description="Checkpoints in this session"
    )

    def update_cost(self, model: str | None = None) -> None:
        """Recalculate estimated cost using this session's model."""
        cost = self.tokens.calculate_cost(model=model or self.model)
        self.estimated_cost_usd = float(cost)


class IssueUsage(UsageAggregate):
    """
    Usage aggregate for a GitHub issue.

    Aggregates usage across all sessions and checkpoints for an issue.
    """

    issue_number: int = Field(..., ge=1, description="GitHub issue number")
    pr_number: int | None = Field(default=None, ge=1, description="Associated PR number if any")
    session_ids: list[str] = Field(default_factory=list, description="Session IDs that contributed")
    branch: str | None = Field(default=None, description="Primary branch for this issue")
    pipeline_phases: list[str] = Field(default_factory=list, description="Pipeline phases seen")


class WorkflowUsage(UsageAggregate):
    """
    Usage aggregate for a workflow/job run.

    Workflows are identified by GitHub Actions run ID or similar.
    """

    workflow_id: str = Field(..., description="Workflow run identifier")
    workflow_name: str | None = Field(default=None, description="Workflow name")
    job_name: str | None = Field(default=None, description="Job name within workflow")
    issue_number: int | None = Field(default=None, ge=1, description="Associated issue number")
    pr_number: int | None = Field(default=None, ge=1, description="Associated PR number")
    trigger_event: str | None = Field(default=None, description="Event that triggered the workflow")
    session_ids: list[str] = Field(default_factory=list, description="Session IDs in this workflow")


class PRUsage(UsageAggregate):
    """
    Usage aggregate for a pull request.

    Aggregates usage across all sessions and checkpoints for a PR.
    """

    pr_number: int = Field(..., ge=1, description="GitHub PR number")
    issue_number: int | None = Field(default=None, ge=1, description="Associated issue number")
    branch: str | None = Field(default=None, description="PR head branch")
    base_branch: str | None = Field(default=None, description="PR base branch")
    session_ids: list[str] = Field(default_factory=list, description="Session IDs that contributed")
    pipeline_phases: list[str] = Field(default_factory=list, description="Pipeline phases seen")


class UsageIndex(BaseModel):
    """
    Index of all usage aggregates.

    Provides fast lookup of usage by different dimensions.
    """

    schemaVersion: str = Field(  # noqa: N815
        default="1.0", pattern=r"^[0-9]+\.[0-9]+$", description="Schema version"
    )
    last_updated: datetime = Field(..., description="When the index was last updated")

    # Counts for quick overview
    total_sessions: int = Field(default=0, ge=0, description="Total sessions tracked")
    total_issues: int = Field(default=0, ge=0, description="Total issues tracked")
    total_prs: int = Field(default=0, ge=0, description="Total PRs tracked")
    total_workflows: int = Field(default=0, ge=0, description="Total workflows tracked")

    # Aggregate totals
    total_tokens: TokenCounts = Field(
        default_factory=TokenCounts, description="Total tokens across all usage"
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="Total estimated cost")

    # Lists of IDs for enumeration
    session_ids: list[str] = Field(default_factory=list, description="All session IDs")
    issue_numbers: list[int] = Field(default_factory=list, description="All issue numbers")
    pr_numbers: list[int] = Field(default_factory=list, description="All PR numbers")
    workflow_ids: list[str] = Field(default_factory=list, description="All workflow IDs")
