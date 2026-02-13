"""
Tests for DAG visualizer module.
"""

from datetime import datetime, timedelta

from dag_visualizer import (
    PHASE_ORDER,
    _format_duration,
    _get_agent_status_symbol,
    _get_status_symbol,
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
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)


def create_test_pipeline(
    pipeline_id: str = "test-123",
    status: PipelineStatus = PipelineStatus.RUNNING,
    current_phase: PipelinePhase = PipelinePhase.IMPLEMENT,
    phases: dict | None = None,
) -> Pipeline:
    """Create a test pipeline with optional phase data."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=123,
        repo="test/repo",
        branch="egg/test-feature",
        status=status,
        current_phase=current_phase,
    )
    if phases:
        pipeline.phases = phases
    return pipeline


class TestFormatDuration:
    """Tests for _format_duration helper."""

    def test_seconds_only(self):
        """Test duration under a minute."""
        now = datetime.utcnow()
        start = now - timedelta(seconds=45)
        result = _format_duration(start, now)
        assert result == "45s"

    def test_minutes_and_seconds(self):
        """Test duration under an hour."""
        now = datetime.utcnow()
        start = now - timedelta(minutes=5, seconds=30)
        result = _format_duration(start, now)
        assert result == "5m30s"

    def test_hours_and_minutes(self):
        """Test duration over an hour."""
        now = datetime.utcnow()
        start = now - timedelta(hours=2, minutes=15)
        result = _format_duration(start, now)
        assert result == "2h15m"

    def test_no_start_time(self):
        """Test with no start time."""
        result = _format_duration(None)
        assert result == ""

    def test_no_end_time_uses_now(self):
        """Test with no end time defaults to now."""
        start = datetime.utcnow() - timedelta(seconds=10)
        result = _format_duration(start)
        # Should be approximately 10 seconds
        assert "s" in result


class TestStatusSymbol:
    """Tests for _get_status_symbol helper."""

    def test_unicode_symbols(self):
        """Test Unicode status symbols."""
        assert _get_status_symbol(PipelineStatus.PENDING) == "○"
        assert _get_status_symbol(PipelineStatus.RUNNING) == "▶"
        assert _get_status_symbol(PipelineStatus.COMPLETE) == "✓"
        assert _get_status_symbol(PipelineStatus.FAILED) == "✗"
        assert _get_status_symbol(PipelineStatus.AWAITING_HUMAN) == "⏸"

    def test_ascii_symbols(self):
        """Test ASCII-only status symbols."""
        assert _get_status_symbol(PipelineStatus.PENDING, use_ascii=True) == "o"
        assert _get_status_symbol(PipelineStatus.RUNNING, use_ascii=True) == ">"
        assert _get_status_symbol(PipelineStatus.COMPLETE, use_ascii=True) == "+"
        assert _get_status_symbol(PipelineStatus.FAILED, use_ascii=True) == "x"


class TestAgentStatusSymbol:
    """Tests for _get_agent_status_symbol helper."""

    def test_unicode_agent_symbols(self):
        """Test Unicode symbols for all agent execution statuses."""
        assert _get_agent_status_symbol(AgentExecutionStatus.PENDING) == "○"
        assert _get_agent_status_symbol(AgentExecutionStatus.RUNNING) == "▶"
        assert _get_agent_status_symbol(AgentExecutionStatus.COMPLETE) == "✓"
        assert _get_agent_status_symbol(AgentExecutionStatus.FAILED) == "✗"

    def test_ascii_agent_symbols(self):
        """Test ASCII symbols for all agent execution statuses."""
        assert _get_agent_status_symbol(AgentExecutionStatus.PENDING, use_ascii=True) == "o"
        assert _get_agent_status_symbol(AgentExecutionStatus.RUNNING, use_ascii=True) == ">"
        assert _get_agent_status_symbol(AgentExecutionStatus.COMPLETE, use_ascii=True) == "+"
        assert _get_agent_status_symbol(AgentExecutionStatus.FAILED, use_ascii=True) == "x"


class TestRenderPipelineDag:
    """Tests for render_pipeline_dag function."""

    def test_basic_dag_structure(self):
        """Test that DAG contains all phases."""
        pipeline = create_test_pipeline()
        result = render_pipeline_dag(pipeline)

        # Check all phases are present
        assert "Refine" in result
        assert "Plan" in result
        assert "Implement" in result
        assert "PR" in result

    def test_current_phase_marker(self):
        """Test that current phase is marked."""
        pipeline = create_test_pipeline(current_phase=PipelinePhase.PLAN)
        result = render_pipeline_dag(pipeline)

        # Current phase should have >>> marker
        assert ">>>" in result

    def test_header_included(self):
        """Test that header information is included."""
        pipeline = create_test_pipeline()
        result = render_pipeline_dag(pipeline, include_header=True)

        assert f"Pipeline: {pipeline.id}" in result
        assert f"Status: {pipeline.status.value}" in result
        assert f"Repository: {pipeline.repo}" in result

    def test_header_excluded(self):
        """Test that header can be excluded."""
        pipeline = create_test_pipeline()
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "Pipeline:" not in result
        assert "Repository:" not in result

    def test_ascii_mode(self):
        """Test ASCII-only mode uses correct characters."""
        pipeline = create_test_pipeline()
        result = render_pipeline_dag(pipeline, use_ascii=True)

        # Should use ASCII borders
        assert "+" in result or "=" in result
        assert "|" in result

    def test_phase_with_review_cycles(self):
        """Test phase showing review cycles."""
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.COMPLETE,
                review_cycles=2,
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_pipeline_dag(pipeline)

        assert "cycle 2" in result

    def test_phase_with_agents(self):
        """Test phase showing per-agent role and status."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.COMPLETE,
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER,
                        status=AgentExecutionStatus.RUNNING,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline)

        assert "coder" in result
        assert "reviewer" in result
        # Completed coder should have checkmark, running reviewer should have play symbol
        assert "✓ coder" in result
        assert "▶ reviewer" in result
        # Old container count should not appear
        assert "container(s)" not in result

    def test_phase_with_mixed_agent_statuses(self):
        """Test phase with agents in different states renders each with correct symbol."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.COMPLETE,
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER,
                        status=AgentExecutionStatus.RUNNING,
                    ),
                    AgentExecution(
                        role=AgentRole.TESTER,
                        status=AgentExecutionStatus.FAILED,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(
            phases=phases, current_phase=PipelinePhase.IMPLEMENT
        )
        result = render_pipeline_dag(pipeline)

        assert "✓ coder" in result
        assert "▶ reviewer" in result
        assert "✗ tester" in result

    def test_phase_with_no_agents(self):
        """Test that a pending phase with no agents shows no agent info line."""
        pipeline = create_test_pipeline()
        result = render_pipeline_dag(pipeline, include_header=False)

        # Pending phases should not have agent info lines
        assert "coder" not in result
        assert "reviewer" not in result
        assert "agent" not in result

    def test_phase_agents_ascii_mode(self):
        """Test agent symbols use ASCII equivalents when use_ascii=True."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.COMPLETE,
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER,
                        status=AgentExecutionStatus.RUNNING,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(
            phases=phases, current_phase=PipelinePhase.IMPLEMENT
        )
        result = render_pipeline_dag(pipeline, use_ascii=True)

        assert "+ coder" in result
        assert "> reviewer" in result


class TestRenderCompactStatus:
    """Tests for render_compact_status function."""

    def test_all_phases_shown(self):
        """Test that all phases are shown in compact view."""
        pipeline = create_test_pipeline()
        result = render_compact_status(pipeline)

        assert "Refine" in result
        assert "Plan" in result
        assert "Implement" in result
        assert "PR" in result

    def test_current_phase_bracketed(self):
        """Test that current phase is bracketed."""
        pipeline = create_test_pipeline(current_phase=PipelinePhase.PLAN)
        result = render_compact_status(pipeline)

        # Current phase should be in brackets
        assert "[" in result
        assert "Plan" in result

    def test_ascii_arrows(self):
        """Test ASCII mode uses text arrows."""
        pipeline = create_test_pipeline()
        result = render_compact_status(pipeline, use_ascii=True)

        assert "-->" in result

    def test_unicode_arrows(self):
        """Test Unicode mode uses arrow symbols."""
        pipeline = create_test_pipeline()
        result = render_compact_status(pipeline, use_ascii=False)

        assert "→" in result


class TestRenderProgressBar:
    """Tests for render_progress_bar function."""

    def test_empty_progress(self):
        """Test progress bar with no completed phases."""
        pipeline = create_test_pipeline(
            current_phase=PipelinePhase.REFINE,
            status=PipelineStatus.PENDING,
        )
        result = render_progress_bar(pipeline, width=20)

        # Should show 0% or very low percentage
        assert "0%" in result or "12%" in result

    def test_partial_progress(self):
        """Test progress bar with some completed phases."""
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.COMPLETE,
            ),
            "plan": PhaseExecution(
                phase=PipelinePhase.PLAN,
                status=PipelineStatus.COMPLETE,
            ),
        }
        pipeline = create_test_pipeline(
            phases=phases,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        result = render_progress_bar(pipeline, width=20)

        # Should show approximately 50% (2/4 complete + half for current)
        assert "62%" in result or "50%" in result

    def test_complete_progress(self):
        """Test progress bar when all phases complete."""
        phases = {
            phase.value: PhaseExecution(
                phase=phase,
                status=PipelineStatus.COMPLETE,
            )
            for phase in PHASE_ORDER
        }
        pipeline = create_test_pipeline(
            phases=phases,
            current_phase=PipelinePhase.PR,
            status=PipelineStatus.COMPLETE,
        )
        result = render_progress_bar(pipeline, width=20)

        assert "100%" in result

    def test_ascii_mode_characters(self):
        """Test ASCII mode uses correct fill characters."""
        pipeline = create_test_pipeline()
        result = render_progress_bar(pipeline, use_ascii=True)

        # Should use # and - for fill
        assert "#" in result or "-" in result


class TestRenderPhaseDetail:
    """Tests for render_phase_detail function."""

    def test_not_started_phase(self):
        """Test detail view for phase not yet started."""
        pipeline = create_test_pipeline()
        result = render_phase_detail(pipeline, PipelinePhase.PR)

        assert "Phase: PR" in result
        assert "Not started" in result

    def test_phase_with_all_details(self):
        """Test detail view with full phase execution data."""
        now = datetime.utcnow()
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                started_at=now - timedelta(minutes=10),
                review_cycles=1,
                containers=[
                    ContainerInfo(
                        container_id="abc123",
                        container_name="egg-sandbox-coder",
                        status=ContainerStatus.RUNNING,
                        agent_role=AgentRole.CODER,
                    )
                ],
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.RUNNING,
                    )
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        assert "Phase: Implement" in result
        assert "running" in result
        assert "Review Cycles: 1" in result
        assert "Containers (1):" in result
        assert "Agents (1):" in result
        assert "coder" in result

    def test_failed_phase_shows_error(self):
        """Test detail view shows error for failed phase."""
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.FAILED,
                error="Container failed to start",
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.REFINE)

        assert "failed" in result
        assert "Error: Container failed to start" in result


class TestGenerateStatusReport:
    """Tests for generate_status_report function."""

    def test_report_structure(self):
        """Test that report contains all expected fields."""
        pipeline = create_test_pipeline()
        report = generate_status_report(pipeline)

        assert "pipeline_id" in report
        assert "status" in report
        assert "current_phase" in report
        assert "visualization" in report
        assert "phases" in report
        assert "pending_decisions" in report
        assert "updated_at" in report

    def test_visualization_contains_all_formats(self):
        """Test that visualization includes all format types."""
        pipeline = create_test_pipeline()
        report = generate_status_report(pipeline)

        assert "dag" in report["visualization"]
        assert "compact" in report["visualization"]
        assert "progress" in report["visualization"]

    def test_phases_contains_all_phases(self):
        """Test that phases dict includes all pipeline phases with correct data types."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.COMPLETE,
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER,
                        status=AgentExecutionStatus.RUNNING,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        report = generate_status_report(pipeline)

        for phase in PHASE_ORDER:
            assert phase.value in report["phases"]
            phase_data = report["phases"][phase.value]
            assert "status" in phase_data
            assert "review_cycles" in phase_data
            assert "containers" in phase_data
            assert "agents" in phase_data
            # Agents should be a list
            assert isinstance(phase_data["agents"], list)

        # Verify agent details for the phase with agents
        impl_agents = report["phases"]["implement"]["agents"]
        assert len(impl_agents) == 2
        assert impl_agents[0]["role"] == "coder"
        assert impl_agents[0]["status"] == "complete"
        assert impl_agents[1]["role"] == "reviewer"
        assert impl_agents[1]["status"] == "running"

        # Phases without agents should have empty list
        assert report["phases"]["refine"]["agents"] == []

    def test_ascii_mode_propagates(self):
        """Test that ASCII mode affects visualizations."""
        pipeline = create_test_pipeline()
        report = generate_status_report(pipeline, use_ascii=True)

        # Compact should use ASCII arrows
        assert "-->" in report["visualization"]["compact"]

    def test_pending_decisions_count(self):
        """Test that pending decisions are counted."""
        pipeline = create_test_pipeline()
        pipeline.add_decision("Test question?", ["yes", "no"])

        report = generate_status_report(pipeline)
        assert report["pending_decisions"] == 1
