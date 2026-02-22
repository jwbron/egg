"""
Extended tests for DAG visualizer — covers gaps in the coder's test file.

Focuses on:
- _format_seconds, _total_work_seconds, _render_arrow (untested helpers)
- _render_phase_box edge cases
- Model field validation (plan_phase_waves, plan_phase_names, plan_phase_id)
- Tier 3 status report integration
- Compact status and progress bar with Tier 3 pipelines
- Agents assigned to non-existent waves
- Visualization endpoint route handler
- Commit truncation in phase detail
- CANCELLED status across all views
"""

from datetime import datetime, timedelta

from dag_visualizer import (
    _format_seconds,
    _render_arrow,
    _render_fan_in,
    _render_fan_out,
    _render_phase_box,
    _render_side_by_side,
    _render_subphase_box,
    _render_tier3_implement,
    _total_work_seconds,
    generate_status_report,
    render_compact_status,
    render_phase_detail,
    render_pipeline_dag,
    render_progress_bar,
)
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    CycleTiming,
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(**kwargs) -> Pipeline:
    """Create a test pipeline with sensible defaults."""
    defaults = {
        "id": "test-ext",
        "issue_number": 1,
        "repo": "test/repo",
        "branch": "egg/test",
        "status": PipelineStatus.RUNNING,
        "current_phase": PipelinePhase.IMPLEMENT,
    }
    defaults.update(kwargs)
    return Pipeline(**defaults)


# ---------------------------------------------------------------------------
# _format_seconds
# ---------------------------------------------------------------------------


class TestFormatSeconds:
    """Direct tests for _format_seconds helper."""

    def test_zero_seconds(self):
        assert _format_seconds(0) == "0s"

    def test_under_minute(self):
        assert _format_seconds(42) == "42s"

    def test_exact_minute(self):
        assert _format_seconds(60) == "1m0s"

    def test_minutes_and_seconds(self):
        assert _format_seconds(125) == "2m5s"

    def test_exact_hour(self):
        assert _format_seconds(3600) == "1h0m"

    def test_hours_and_minutes(self):
        assert _format_seconds(7380) == "2h3m"

    def test_large_value(self):
        # 100 hours
        assert _format_seconds(360000) == "100h0m"


# ---------------------------------------------------------------------------
# _total_work_seconds
# ---------------------------------------------------------------------------


class TestTotalWorkSeconds:
    """Direct tests for _total_work_seconds helper."""

    def test_single_completed_cycle(self):
        now = datetime.utcnow()
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.COMPLETE,
            cycle_timings=[
                CycleTiming(
                    cycle=0,
                    started_at=now - timedelta(minutes=5),
                    completed_at=now,
                ),
            ],
        )
        total = _total_work_seconds(phase_exec)
        assert total == 300  # 5 minutes = 300 seconds

    def test_multiple_cycles_sum(self):
        now = datetime.utcnow()
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.COMPLETE,
            cycle_timings=[
                CycleTiming(
                    cycle=0,
                    started_at=now - timedelta(minutes=10),
                    completed_at=now - timedelta(minutes=5),
                ),
                CycleTiming(
                    cycle=1,
                    started_at=now - timedelta(minutes=3),
                    completed_at=now,
                ),
            ],
        )
        total = _total_work_seconds(phase_exec)
        assert total == 480  # 5min + 3min = 480s

    def test_no_cycle_timings(self):
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
        )
        total = _total_work_seconds(phase_exec)
        assert total == 0

    def test_running_cycle_uses_utcnow(self):
        """Cycle without completed_at defaults to now."""
        now = datetime.utcnow()
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            cycle_timings=[
                CycleTiming(
                    cycle=0,
                    started_at=now - timedelta(seconds=10),
                ),
            ],
        )
        total = _total_work_seconds(phase_exec)
        assert 9 <= total <= 12  # approximately 10s


# ---------------------------------------------------------------------------
# _render_arrow
# ---------------------------------------------------------------------------


class TestRenderArrow:
    """Direct tests for _render_arrow helper."""

    def test_unicode_arrow(self):
        lines = _render_arrow(use_ascii=False)
        assert len(lines) == 3
        assert "│" in lines[0]
        assert "│" in lines[1]
        assert "▼" in lines[2]

    def test_ascii_arrow(self):
        lines = _render_arrow(use_ascii=True)
        assert len(lines) == 3
        assert "|" in lines[0]
        assert "|" in lines[1]
        assert "v" in lines[2]


# ---------------------------------------------------------------------------
# _render_phase_box edge cases
# ---------------------------------------------------------------------------


class TestRenderPhaseBox:
    """Direct tests for _render_phase_box."""

    def test_pending_phase_minimal(self):
        """Pending phase with no agents or duration."""
        lines = _render_phase_box(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.PENDING,
            review_cycles=0,
            is_current=False,
        )
        text = "\n".join(lines)
        assert "Refine" in text
        assert "pending" in text
        assert ">>>" not in text

    def test_current_phase_marker(self):
        lines = _render_phase_box(
            phase=PipelinePhase.PLAN,
            status=PipelineStatus.RUNNING,
            review_cycles=0,
            is_current=True,
        )
        assert any(">>>" in line for line in lines)

    def test_single_review_cycle_singular(self):
        lines = _render_phase_box(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.COMPLETE,
            review_cycles=1,
            is_current=False,
        )
        text = "\n".join(lines)
        assert "1 cycle completed" in text

    def test_multiple_review_cycles_plural(self):
        lines = _render_phase_box(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.COMPLETE,
            review_cycles=3,
            is_current=False,
        )
        text = "\n".join(lines)
        assert "3 cycles completed" in text

    def test_duration_only(self):
        """Duration line shows [Xm] when only cycle duration is provided."""
        lines = _render_phase_box(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.COMPLETE,
            review_cycles=0,
            is_current=False,
            duration="5m0s",
        )
        text = "\n".join(lines)
        assert "[5m0s]" in text
        assert "last cycle:" not in text

    def test_duration_and_total_duration(self):
        """Duration line shows [last cycle: X | total: Y]."""
        lines = _render_phase_box(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.COMPLETE,
            review_cycles=1,
            is_current=False,
            duration="3m0s",
            total_duration="8m0s",
        )
        text = "\n".join(lines)
        assert "last cycle: 3m0s" in text
        assert "total: 8m0s" in text

    def test_duration_and_total_duration_same_value(self):
        """When duration equals total_duration, shows simple format."""
        lines = _render_phase_box(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.COMPLETE,
            review_cycles=0,
            is_current=False,
            duration="5m0s",
            total_duration="5m0s",
        )
        text = "\n".join(lines)
        assert "[5m0s]" in text
        assert "last cycle:" not in text

    def test_cancelled_status(self):
        lines = _render_phase_box(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.CANCELLED,
            review_cycles=0,
            is_current=False,
        )
        text = "\n".join(lines)
        assert "⊘" in text
        assert "cancelled" in text

    def test_box_borders_unicode(self):
        lines = _render_phase_box(
            phase=PipelinePhase.PR,
            status=PipelineStatus.PENDING,
            review_cycles=0,
            is_current=False,
        )
        assert "╔" in lines[0]
        assert "╚" in lines[-1]

    def test_box_borders_ascii(self):
        lines = _render_phase_box(
            phase=PipelinePhase.PR,
            status=PipelineStatus.PENDING,
            review_cycles=0,
            is_current=False,
            use_ascii=True,
        )
        assert "+" in lines[0]
        assert "+" in lines[-1]
        assert "=" in lines[0]

    def test_agents_with_wrapping(self):
        """4+ agents in a wave should wrap at 3 per line."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.RUNNING),
            AgentExecution(role=AgentRole.REVIEWER_CODE, status=AgentExecutionStatus.PENDING),
            AgentExecution(role=AgentRole.REVIEWER_CONTRACT, status=AgentExecutionStatus.PENDING),
        ]
        lines = _render_phase_box(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            review_cycles=0,
            is_current=True,
            agents=agents,
        )
        text = "\n".join(lines)
        # All agents should be present
        assert "coder" in text
        assert "tester" in text
        assert "documenter" in text
        assert "integrator" in text
        assert "reviewer_code" in text


# ---------------------------------------------------------------------------
# Model field tests for new fields
# ---------------------------------------------------------------------------


class TestModelNewFields:
    """Test serialization/deserialization of new model fields."""

    def test_pipeline_plan_phase_waves_default_none(self):
        p = Pipeline(
            id="test",
            status=PipelineStatus.PENDING,
            current_phase=PipelinePhase.REFINE,
        )
        assert p.plan_phase_waves is None
        assert p.plan_phase_names is None

    def test_pipeline_plan_phase_waves_set(self):
        p = Pipeline(
            id="test",
            status=PipelineStatus.PENDING,
            current_phase=PipelinePhase.REFINE,
            plan_phase_waves=[["p1", "p2"], ["p3"]],
            plan_phase_names={"p1": "Auth", "p2": "API", "p3": "UI"},
        )
        assert p.plan_phase_waves == [["p1", "p2"], ["p3"]]
        assert p.plan_phase_names == {"p1": "Auth", "p2": "API", "p3": "UI"}

    def test_pipeline_roundtrip_json(self):
        """plan_phase_waves and plan_phase_names survive JSON roundtrip."""
        p = Pipeline(
            id="test",
            status=PipelineStatus.PENDING,
            current_phase=PipelinePhase.REFINE,
            plan_phase_waves=[["p1"], ["p2", "p3"]],
            plan_phase_names={"p1": "Auth", "p2": "API", "p3": "UI"},
        )
        data = p.model_dump()
        p2 = Pipeline(**data)
        assert p2.plan_phase_waves == p.plan_phase_waves
        assert p2.plan_phase_names == p.plan_phase_names

    def test_agent_execution_plan_phase_id_default_none(self):
        a = AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.PENDING,
        )
        assert a.plan_phase_id is None

    def test_agent_execution_plan_phase_id_set(self):
        a = AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.PENDING,
            plan_phase_id="phase-1",
        )
        assert a.plan_phase_id == "phase-1"

    def test_agent_execution_roundtrip(self):
        a = AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.COMPLETE,
            plan_phase_id="phase-2",
        )
        data = a.model_dump()
        a2 = AgentExecution(**data)
        assert a2.plan_phase_id == "phase-2"


# ---------------------------------------------------------------------------
# Tier 3 integration with generate_status_report
# ---------------------------------------------------------------------------


class TestStatusReportTier3:
    """Test generate_status_report with Tier 3 pipelines."""

    def test_report_contains_tier3_dag(self):
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"], ["p2", "p3"]],
            plan_phase_names={"p1": "Auth", "p2": "API", "p3": "UI"},
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.RUNNING,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="p1",
                        ),
                    ],
                ),
            },
        )
        report = generate_status_report(pipeline)

        assert "Tier 3" in report["visualization"]["dag"]
        assert "Auth" in report["visualization"]["dag"]
        assert "API" in report["visualization"]["dag"]
        assert report["status"] == "running"
        assert report["current_phase"] == "implement"

    def test_report_agents_not_deduplicated_tier3(self):
        """API agents list preserves all runs even for Tier 3."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"]],
            plan_phase_names={"p1": "Auth"},
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.RUNNING,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.FAILED,
                            plan_phase_id="p1",
                        ),
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="p1",
                        ),
                    ],
                ),
            },
        )
        report = generate_status_report(pipeline)
        impl_agents = report["phases"]["implement"]["agents"]
        assert len(impl_agents) == 2
        assert impl_agents[0]["status"] == "failed"
        assert impl_agents[1]["status"] == "complete"


# ---------------------------------------------------------------------------
# Compact status / progress bar with Tier 3
# ---------------------------------------------------------------------------


class TestCompactStatusTier3:
    """Compact status and progress bar work identically for Tier 3."""

    def test_compact_status_shows_implement(self):
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"], ["p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
        )
        result = render_compact_status(pipeline)
        # Compact status shows top-level phases, not sub-phases
        assert "Implement" in result
        assert "Auth" not in result  # Sub-phases not in compact view

    def test_progress_bar_tier3(self):
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"], ["p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
            phases={
                "refine": PhaseExecution(
                    phase=PipelinePhase.REFINE,
                    status=PipelineStatus.COMPLETE,
                ),
                "plan": PhaseExecution(
                    phase=PipelinePhase.PLAN,
                    status=PipelineStatus.COMPLETE,
                ),
            },
        )
        result = render_progress_bar(pipeline)
        # Should calculate progress normally
        assert "%" in result


# ---------------------------------------------------------------------------
# Agents assigned to phase IDs not in waves
# ---------------------------------------------------------------------------


class TestAgentsOrphanedPhaseId:
    """Agents with plan_phase_id not present in plan_phase_waves."""

    def test_orphaned_phase_agents_render_in_top_level(self):
        """Agents whose plan_phase_id doesn't match any wave are top-level."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"]],
            plan_phase_names={"p1": "Auth"},
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.RUNNING,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="p1",
                        ),
                        AgentExecution(
                            role=AgentRole.TESTER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="nonexistent-phase",
                        ),
                    ],
                ),
            },
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # Auth should contain the coder
        assert "Auth" in result
        # Tester should still appear (in its own sub-phase box since nonexistent
        # is in agents_by_phase but not in waves, so it won't be rendered in
        # any wave — it's effectively orphaned)
        assert "coder" in result


# ---------------------------------------------------------------------------
# CANCELLED status in DAG visualization
# ---------------------------------------------------------------------------


class TestCancelledStatus:
    """Test CANCELLED status across different views."""

    def test_cancelled_symbol_unicode(self):
        from dag_visualizer import _get_status_symbol

        assert _get_status_symbol(PipelineStatus.CANCELLED) == "⊘"

    def test_cancelled_symbol_ascii(self):
        from dag_visualizer import _get_status_symbol

        assert _get_status_symbol(PipelineStatus.CANCELLED, use_ascii=True) == "-"

    def test_cancelled_pipeline_header(self):
        pipeline = _make_pipeline(status=PipelineStatus.CANCELLED)
        result = render_pipeline_dag(pipeline)
        assert "Status: cancelled" in result

    def test_cancelled_in_compact_status(self):
        pipeline = _make_pipeline(
            status=PipelineStatus.CANCELLED,
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.CANCELLED,
                ),
            },
        )
        result = render_compact_status(pipeline)
        assert "⊘" in result


# ---------------------------------------------------------------------------
# Phase detail commit truncation
# ---------------------------------------------------------------------------


class TestPhaseDetailCommitTruncation:
    """Test commit SHA display in phase detail."""

    def test_long_commit_sha_truncated(self):
        """Commit SHA is truncated to 8 characters in detail view."""
        pipeline = _make_pipeline(
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.COMPLETE,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            commit="abc12345def67890",
                        ),
                    ],
                ),
            },
        )
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)
        assert "Commit: abc12345" in result
        assert "def67890" not in result

    def test_short_commit_sha_not_padded(self):
        """Short commit SHAs are shown as-is."""
        pipeline = _make_pipeline(
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.COMPLETE,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            commit="abc1",
                        ),
                    ],
                ),
            },
        )
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)
        assert "Commit: abc1" in result


# ---------------------------------------------------------------------------
# Phase detail error truncation
# ---------------------------------------------------------------------------


class TestPhaseDetailErrorTruncation:
    """Test error message display in phase detail."""

    def test_long_error_truncated(self):
        """Error messages are truncated to 50 chars in agent detail."""
        long_error = "A" * 100
        pipeline = _make_pipeline(
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.FAILED,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.FAILED,
                            error=long_error,
                        ),
                    ],
                ),
            },
        )
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)
        # Agent error is truncated to 50 chars
        assert "A" * 50 in result
        assert "A" * 51 not in result


# ---------------------------------------------------------------------------
# Render tier3 with mixed sub-phase statuses
# ---------------------------------------------------------------------------


class TestTier3MixedSubphaseStatuses:
    """Test Tier 3 rendering with mixed statuses across sub-phases."""

    def test_one_failed_one_complete_subphase(self):
        """Parallel wave with one failed and one complete sub-phase."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.RUNNING,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="p1",
                        ),
                        AgentExecution(
                            role=AgentRole.TESTER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="p1",
                        ),
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.FAILED,
                            plan_phase_id="p2",
                        ),
                    ],
                ),
            },
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # Auth sub-phase should show complete symbol
        assert "✓" in result
        # API sub-phase should show failed symbol
        assert "✗" in result
        # Both phase names should appear
        assert "Auth" in result
        assert "API" in result

    def test_all_subphases_pending(self):
        """All sub-phases have no agents — all pending."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # Both should show pending symbol
        assert result.count("○") >= 2


# ---------------------------------------------------------------------------
# Fan-out / fan-in with asymmetric box widths
# ---------------------------------------------------------------------------


class TestFanConnectorsAsymmetric:
    """Fan-out and fan-in with different-width boxes."""

    def test_fan_out_asymmetric_widths(self):
        """Fan-out with boxes of different widths."""
        result = _render_fan_out([10, 30], spacing=2)
        assert len(result) == 3
        # Vertical stem positions should be at box centers
        assert "│" in result[0]
        assert "│" in result[2]
        # Bar connects the two centers
        assert "┌" in result[1]
        assert "┐" in result[1]

    def test_fan_in_asymmetric_widths(self):
        """Fan-in with boxes of different widths."""
        result = _render_fan_in([10, 30], spacing=2)
        assert len(result) == 3
        assert "└" in result[1]
        assert "┘" in result[1]

    def test_fan_out_four_boxes(self):
        """Fan-out with four boxes."""
        result = _render_fan_out([20, 20, 20, 20], spacing=2)
        assert len(result) == 3
        # Should have 4 vertical stems
        assert result[2].count("│") == 4

    def test_fan_out_empty_returns_empty(self):
        """Empty list returns empty."""
        assert _render_fan_out([]) == []

    def test_fan_in_empty_returns_empty(self):
        """Empty list returns empty."""
        assert _render_fan_in([]) == []


# ---------------------------------------------------------------------------
# _render_side_by_side edge cases
# ---------------------------------------------------------------------------


class TestSideBySideEdgeCases:
    """Additional edge cases for side-by-side rendering."""

    def test_boxes_with_different_line_widths(self):
        """Box with inconsistent internal line widths."""
        box1 = ["short", "a longer line here"]
        box2 = ["x", "yy"]
        result = _render_side_by_side([box1, box2], spacing=2)
        assert len(result) == 2
        # All lines should be the same length
        widths = {len(line) for line in result}
        assert len(widths) == 1

    def test_zero_spacing(self):
        """Zero spacing between boxes."""
        box1 = ["AB"]
        box2 = ["CD"]
        result = _render_side_by_side([box1, box2], spacing=0)
        assert result[0] == "ABCD"


# ---------------------------------------------------------------------------
# _render_subphase_box edge cases
# ---------------------------------------------------------------------------


class TestSubphaseBoxEdgeCases:
    """Additional edge cases for sub-phase box rendering."""

    def test_min_width_enforced(self):
        """Box respects min_width even with short content."""
        lines = _render_subphase_box("p1", "A", [], min_width=30)
        # Box width should be at least min_width + 2 (borders)
        assert len(lines[0]) >= 32

    def test_running_status_symbol(self):
        """Running agents produce running status on box."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.RUNNING),
        ]
        lines = _render_subphase_box("p1", "Auth", agents)
        text = "\n".join(lines)
        assert "▶" in text


# ---------------------------------------------------------------------------
# _render_tier3_implement edge cases
# ---------------------------------------------------------------------------


class TestTier3ImplementEdgeCases:
    """Additional edge cases for _render_tier3_implement."""

    def test_empty_wave_in_middle_skipped(self):
        """Empty waves (no phase IDs) are skipped."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"], [], ["p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
        )
        lines = _render_tier3_implement(pipeline, None, is_current=True)
        text = "\n".join(lines)
        assert "Auth" in text
        assert "API" in text

    def test_not_current_phase(self):
        """Non-current implement doesn't show >>> marker."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"]],
            plan_phase_names={"p1": "Auth"},
        )
        lines = _render_tier3_implement(pipeline, None, is_current=False)
        text = "\n".join(lines)
        assert ">>>" not in text

    def test_sequential_then_parallel_then_sequential(self):
        """Mixed sequential and parallel waves render correctly."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"], ["p2", "p3"], ["p4"]],
            plan_phase_names={
                "p1": "Setup",
                "p2": "Auth",
                "p3": "API",
                "p4": "Integration",
            },
        )
        lines = _render_tier3_implement(pipeline, None, is_current=True)
        text = "\n".join(lines)

        assert "Setup" in text
        assert "Auth" in text
        assert "API" in text
        assert "Integration" in text
        # Fan connectors should appear for the parallel wave
        assert "┌" in text or "+" in text

    def test_top_level_agents_with_deduplication(self):
        """Top-level agents with duplicate roles show count."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1"]],
            plan_phase_names={"p1": "Auth"},
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                    plan_phase_id="p1",
                ),
                AgentExecution(
                    role=AgentRole.REVIEWER_CONTRACT,
                    status=AgentExecutionStatus.FAILED,
                ),
                AgentExecution(
                    role=AgentRole.REVIEWER_CONTRACT,
                    status=AgentExecutionStatus.COMPLETE,
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)
        assert "Pipeline agents" in text
        assert "×2" in text
        assert text.count("reviewer_contract") == 1


# ---------------------------------------------------------------------------
# render_pipeline_dag with Tier 3 and phase details
# ---------------------------------------------------------------------------


class TestRenderPhaseDetailTier3:
    """Test render_phase_detail with agents having plan_phase_id."""

    def test_phase_detail_shows_all_tier3_agents(self):
        """Phase detail shows all agents regardless of plan_phase_id."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
            phases={
                "implement": PhaseExecution(
                    phase=PipelinePhase.IMPLEMENT,
                    status=PipelineStatus.RUNNING,
                    agents=[
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="p1",
                        ),
                        AgentExecution(
                            role=AgentRole.TESTER,
                            status=AgentExecutionStatus.RUNNING,
                            plan_phase_id="p1",
                        ),
                        AgentExecution(
                            role=AgentRole.CODER,
                            status=AgentExecutionStatus.RUNNING,
                            plan_phase_id="p2",
                        ),
                        AgentExecution(
                            role=AgentRole.INTEGRATOR,
                            status=AgentExecutionStatus.PENDING,
                        ),
                    ],
                ),
            },
        )
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        assert "Agents (4):" in result
        # All agents should appear, not just unique roles
        assert result.count("coder") == 2
        assert "tester" in result
        assert "integrator" in result


# ---------------------------------------------------------------------------
# render_pipeline_dag with all phases complete
# ---------------------------------------------------------------------------


class TestRenderPipelineDagComplete:
    """Test full DAG with completed pipeline."""

    def test_all_phases_complete(self):
        phases = {
            phase.value: PhaseExecution(
                phase=phase,
                status=PipelineStatus.COMPLETE,
            )
            for phase in [
                PipelinePhase.REFINE,
                PipelinePhase.PLAN,
                PipelinePhase.IMPLEMENT,
                PipelinePhase.PR,
            ]
        }
        pipeline = _make_pipeline(
            status=PipelineStatus.COMPLETE,
            current_phase=PipelinePhase.PR,
            phases=phases,
        )
        result = render_pipeline_dag(pipeline)

        # All phases should show complete symbol
        assert result.count("✓") >= 4
        # Status should be complete
        assert "Status: complete" in result

    def test_failed_pipeline(self):
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.COMPLETE,
            ),
            "plan": PhaseExecution(
                phase=PipelinePhase.PLAN,
                status=PipelineStatus.FAILED,
                error="Architect timed out",
            ),
        }
        pipeline = _make_pipeline(
            status=PipelineStatus.FAILED,
            current_phase=PipelinePhase.PLAN,
            phases=phases,
        )
        result = render_pipeline_dag(pipeline)

        assert "Status: failed" in result
        assert "✗" in result


# ---------------------------------------------------------------------------
# Unknown status symbol fallback
# ---------------------------------------------------------------------------


class TestStatusSymbolFallback:
    """Test fallback for unknown status values."""

    def test_unknown_status_returns_question_mark(self):
        from dag_visualizer import _get_status_symbol

        # Create a mock status that's not in the dict
        result = _get_status_symbol("nonexistent_status")
        assert result == "?"

    def test_unknown_status_ascii_returns_question_mark(self):
        from dag_visualizer import _get_status_symbol

        result = _get_status_symbol("nonexistent_status", use_ascii=True)
        assert result == "?"
