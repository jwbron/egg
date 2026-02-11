"""Tests for usage models."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from egg_contracts.usage import (
    MODEL_PRICING,
    CheckpointReference,
    IssueUsage,
    PRUsage,
    SessionUsage,
    TokenCounts,
    UsageIndex,
    WorkflowUsage,
    get_model_pricing,
)


class TestTokenCounts:
    """Tests for TokenCounts model."""

    def test_default_values(self):
        """Test default values for token counts."""
        counts = TokenCounts()
        assert counts.input_tokens == 0
        assert counts.output_tokens == 0
        assert counts.cache_read_tokens == 0
        assert counts.cache_creation_tokens == 0
        assert counts.total_tokens() == 0

    def test_total_tokens(self):
        """Test total_tokens calculation."""
        counts = TokenCounts(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_creation_tokens=100,
        )
        assert counts.total_tokens() == 1500  # input + output

    def test_add_token_counts(self):
        """Test adding two TokenCounts together."""
        counts1 = TokenCounts(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_creation_tokens=100,
        )
        counts2 = TokenCounts(
            input_tokens=2000,
            output_tokens=1000,
            cache_read_tokens=400,
            cache_creation_tokens=200,
        )
        result = counts1.add(counts2)
        assert result.input_tokens == 3000
        assert result.output_tokens == 1500
        assert result.cache_read_tokens == 600
        assert result.cache_creation_tokens == 300

    def test_calculate_cost_basic(self):
        """Test cost calculation with default pricing (opus)."""
        counts = TokenCounts(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
        cost = counts.calculate_cost()
        # 1M input at $5/MTok + 1M output at $25/MTok = $30
        expected = Decimal("5.00") + Decimal("25.00")
        assert cost == expected

    def test_calculate_cost_with_cache(self):
        """Test cost calculation with cache tokens."""
        counts = TokenCounts(
            input_tokens=1_000_000,
            output_tokens=500_000,
            cache_read_tokens=500_000,  # Half of input is from cache
            cache_creation_tokens=100_000,
        )
        cost = counts.calculate_cost()
        # Regular input: 500K at $5/MTok = $2.50
        # Cache read: 500K at $0.50/MTok = $0.25
        # Output: 500K at $25/MTok = $12.50
        # Cache write: 100K at $6.25/MTok = $0.625
        # Total: $15.875
        expected = Decimal("2.50") + Decimal("0.25") + Decimal("12.50") + Decimal("0.625")
        assert cost == expected

    def test_calculate_cost_with_model(self):
        """Test cost calculation with explicit model."""
        counts = TokenCounts(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        opus_cost = counts.calculate_cost(model="opus")
        sonnet_cost = counts.calculate_cost(model="sonnet")
        haiku_cost = counts.calculate_cost(model="haiku")

        assert opus_cost == Decimal("30.00")   # $5 + $25
        assert sonnet_cost == Decimal("18.00")  # $3 + $15
        assert haiku_cost == Decimal("6.00")    # $1 + $5
        assert opus_cost > sonnet_cost > haiku_cost

    def test_calculate_cost_with_full_model_id(self):
        """Test that full model IDs resolve to the correct pricing."""
        counts = TokenCounts(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        cost = counts.calculate_cost(model="claude-opus-4-5-20251101")
        assert cost == Decimal("30.00")  # Should resolve to opus pricing

    def test_validation_non_negative(self):
        """Test that negative values are rejected."""
        with pytest.raises(ValueError):
            TokenCounts(input_tokens=-1)


class TestModelPricing:
    """Tests for model pricing lookup."""

    def test_known_aliases(self):
        """Test that all known aliases have pricing."""
        for alias in ("opus", "sonnet", "haiku"):
            pricing = get_model_pricing(alias)
            assert "input" in pricing
            assert "output" in pricing
            assert "cache_read" in pricing
            assert "cache_write" in pricing

    def test_full_model_id_resolution(self):
        """Test resolving full model IDs to aliases."""
        assert get_model_pricing("claude-opus-4-5-20251101") == MODEL_PRICING["opus"]
        assert get_model_pricing("claude-sonnet-4-5-20250929") == MODEL_PRICING["sonnet"]
        assert get_model_pricing("claude-haiku-4-5-20251001") == MODEL_PRICING["haiku"]

    def test_none_defaults_to_opus(self):
        """Test that None model defaults to opus."""
        assert get_model_pricing(None) == MODEL_PRICING["opus"]

    def test_unknown_model_defaults_to_opus(self):
        """Test that unknown models fall back to opus."""
        assert get_model_pricing("unknown-model") == MODEL_PRICING["opus"]

    def test_cache_ratios(self):
        """Test that cache pricing follows Anthropic ratios."""
        for alias, pricing in MODEL_PRICING.items():
            assert pricing["cache_read"] == pricing["input"] / 10, f"{alias} cache_read ratio"
            assert pricing["cache_write"] == pricing["input"] * Decimal("1.25"), (
                f"{alias} cache_write ratio"
            )


class TestCheckpointReference:
    """Tests for CheckpointReference model."""

    def test_create_reference(self):
        """Test creating a checkpoint reference."""
        now = datetime.now(UTC)
        ref = CheckpointReference(
            checkpoint_id="ckpt-abc123456789",
            commit_sha="abc123456789",
            created_at=now,
        )
        assert ref.checkpoint_id == "ckpt-abc123456789"
        assert ref.commit_sha == "abc123456789"
        assert ref.created_at == now


class TestSessionUsage:
    """Tests for SessionUsage model."""

    def test_minimal_session_usage(self):
        """Test creating session usage with minimal fields."""
        now = datetime.now(UTC)
        usage = SessionUsage(
            session_id="session-123",
            last_updated=now,
        )
        assert usage.session_id == "session-123"
        assert usage.tokens.input_tokens == 0
        assert usage.checkpoint_count == 0
        assert usage.checkpoints == []

    def test_full_session_usage(self):
        """Test creating session usage with all fields."""
        now = datetime.now(UTC)
        checkpoint_ref = CheckpointReference(
            checkpoint_id="ckpt-abc123456789",
            commit_sha="abc123456789",
            created_at=now,
        )
        usage = SessionUsage(
            session_id="session-456",
            container_id="container-789",
            agent_role="coder",
            model="claude-opus-4-5-20251101",
            issue_number=123,
            pr_number=456,
            tokens=TokenCounts(input_tokens=1000, output_tokens=500),
            estimated_cost_usd=0.01,
            checkpoint_count=1,
            first_checkpoint_at=now,
            last_checkpoint_at=now,
            last_updated=now,
            checkpoints=[checkpoint_ref],
        )
        assert usage.container_id == "container-789"
        assert usage.agent_role == "coder"
        assert usage.issue_number == 123
        assert usage.pr_number == 456
        assert len(usage.checkpoints) == 1

    def test_update_cost(self):
        """Test update_cost method uses default opus pricing."""
        now = datetime.now(UTC)
        usage = SessionUsage(
            session_id="session-123",
            tokens=TokenCounts(input_tokens=1_000_000, output_tokens=1_000_000),
            last_updated=now,
        )
        usage.update_cost()
        # 1M input at $5 + 1M output at $25 = $30
        assert usage.estimated_cost_usd == pytest.approx(30.0)

    def test_update_cost_uses_session_model(self):
        """Test that SessionUsage.update_cost uses its model field."""
        now = datetime.now(UTC)
        usage = SessionUsage(
            session_id="session-123",
            model="sonnet",
            tokens=TokenCounts(input_tokens=1_000_000, output_tokens=1_000_000),
            last_updated=now,
        )
        usage.update_cost()
        # 1M input at $3 + 1M output at $15 = $18
        assert usage.estimated_cost_usd == pytest.approx(18.0)


class TestIssueUsage:
    """Tests for IssueUsage model."""

    def test_minimal_issue_usage(self):
        """Test creating issue usage with minimal fields."""
        now = datetime.now(UTC)
        usage = IssueUsage(
            issue_number=519,
            last_updated=now,
        )
        assert usage.issue_number == 519
        assert usage.pr_number is None
        assert usage.session_ids == []
        assert usage.pipeline_phases == []

    def test_full_issue_usage(self):
        """Test creating issue usage with all fields."""
        now = datetime.now(UTC)
        usage = IssueUsage(
            issue_number=519,
            pr_number=522,
            session_ids=["session-1", "session-2"],
            branch="egg/issue-519",
            pipeline_phases=["refine", "plan", "implement"],
            tokens=TokenCounts(input_tokens=5000, output_tokens=2500),
            estimated_cost_usd=0.05,
            checkpoint_count=5,
            first_checkpoint_at=now,
            last_checkpoint_at=now,
            last_updated=now,
        )
        assert usage.pr_number == 522
        assert len(usage.session_ids) == 2
        assert "implement" in usage.pipeline_phases


class TestPRUsage:
    """Tests for PRUsage model."""

    def test_minimal_pr_usage(self):
        """Test creating PR usage with minimal fields."""
        now = datetime.now(UTC)
        usage = PRUsage(
            pr_number=522,
            last_updated=now,
        )
        assert usage.pr_number == 522
        assert usage.issue_number is None
        assert usage.branch is None

    def test_full_pr_usage(self):
        """Test creating PR usage with all fields."""
        now = datetime.now(UTC)
        usage = PRUsage(
            pr_number=522,
            issue_number=519,
            branch="egg/issue-519",
            base_branch="main",
            session_ids=["session-1", "session-2", "session-3"],
            pipeline_phases=["implement", "pr"],
            tokens=TokenCounts(input_tokens=10000, output_tokens=5000),
            estimated_cost_usd=0.10,
            checkpoint_count=10,
            first_checkpoint_at=now,
            last_checkpoint_at=now,
            last_updated=now,
        )
        assert usage.base_branch == "main"
        assert len(usage.session_ids) == 3


class TestWorkflowUsage:
    """Tests for WorkflowUsage model."""

    def test_minimal_workflow_usage(self):
        """Test creating workflow usage with minimal fields."""
        now = datetime.now(UTC)
        usage = WorkflowUsage(
            workflow_id="run-12345",
            last_updated=now,
        )
        assert usage.workflow_id == "run-12345"
        assert usage.workflow_name is None

    def test_full_workflow_usage(self):
        """Test creating workflow usage with all fields."""
        now = datetime.now(UTC)
        usage = WorkflowUsage(
            workflow_id="run-12345",
            workflow_name="egg-review",
            job_name="review",
            issue_number=519,
            pr_number=522,
            trigger_event="pull_request",
            session_ids=["session-1"],
            tokens=TokenCounts(input_tokens=2000, output_tokens=1000),
            estimated_cost_usd=0.02,
            checkpoint_count=2,
            first_checkpoint_at=now,
            last_checkpoint_at=now,
            last_updated=now,
        )
        assert usage.workflow_name == "egg-review"
        assert usage.trigger_event == "pull_request"


class TestUsageIndex:
    """Tests for UsageIndex model."""

    def test_empty_index(self):
        """Test creating an empty usage index."""
        now = datetime.now(UTC)
        index = UsageIndex(last_updated=now)
        assert index.total_sessions == 0
        assert index.total_issues == 0
        assert index.total_prs == 0
        assert index.total_workflows == 0
        assert index.total_cost_usd == 0.0
        assert index.session_ids == []
        assert index.issue_numbers == []
        assert index.pr_numbers == []

    def test_populated_index(self):
        """Test creating a populated usage index."""
        now = datetime.now(UTC)
        index = UsageIndex(
            last_updated=now,
            total_sessions=5,
            total_issues=3,
            total_prs=2,
            total_workflows=10,
            total_tokens=TokenCounts(input_tokens=100000, output_tokens=50000),
            total_cost_usd=1.50,
            session_ids=["s1", "s2", "s3", "s4", "s5"],
            issue_numbers=[519, 520, 521],
            pr_numbers=[522, 523],
            workflow_ids=["w1", "w2"],
        )
        assert index.total_sessions == 5
        assert index.total_cost_usd == 1.50
        assert len(index.session_ids) == 5
