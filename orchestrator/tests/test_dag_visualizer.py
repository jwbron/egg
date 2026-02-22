"""
Tests for DAG visualizer module.
"""

from datetime import datetime, timedelta

from dag_visualizer import (
    PHASE_ORDER,
    _compute_wave_order,
    _deduplicate_agents,
    _derive_subphase_status,
    _format_duration,
    _get_agent_status_symbol,
    _get_status_symbol,
    _render_fan_in,
    _render_fan_out,
    _render_side_by_side,
    _render_subphase_box,
    _render_tier3_implement,
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
    CycleTiming,
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

        assert "2 cycles completed" in result

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
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
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
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, use_ascii=True)

        assert "+ coder" in result
        assert "> reviewer" in result

    def test_hitl_gate_phase_shows_awaiting_approval(self):
        """Test that a phase in AWAITING_HUMAN status shows 'awaiting approval'."""
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.AWAITING_HUMAN,
            ),
        }
        pipeline = create_test_pipeline(
            phases=phases,
            current_phase=PipelinePhase.REFINE,
            status=PipelineStatus.AWAITING_HUMAN,
        )
        result = render_pipeline_dag(pipeline)

        assert "⏸" in result
        assert "awaiting approval" in result
        # Should NOT show the raw enum value
        assert "awaiting_human" not in result

    def test_hitl_gate_header_shows_awaiting_approval(self):
        """Test that pipeline header shows 'awaiting approval' instead of 'awaiting_human'."""
        pipeline = create_test_pipeline(
            status=PipelineStatus.AWAITING_HUMAN,
            current_phase=PipelinePhase.REFINE,
        )
        result = render_pipeline_dag(pipeline, include_header=True)

        assert "Status: awaiting approval" in result
        assert "Status: awaiting_human" not in result


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

    def test_duration_uses_work_started_at(self):
        """Test that duration calculation prefers work_started_at over started_at."""
        now = datetime.utcnow()
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.COMPLETE,
                started_at=now - timedelta(minutes=20),
                work_started_at=now - timedelta(minutes=10),
                completed_at=now,
                cycle_timings=[
                    CycleTiming(
                        cycle=0,
                        started_at=now - timedelta(minutes=10),
                        completed_at=now,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        # Duration should be ~10m (from work_started_at), not ~20m (from started_at)
        assert "Duration: 10m" in result
        assert "20m" not in result
        # Both timestamps should appear
        assert "Started:" in result
        assert "Work started:" in result

    def test_duration_falls_back_to_started_at(self):
        """Test that duration falls back to started_at when work_started_at is None."""
        now = datetime.utcnow()
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.COMPLETE,
                started_at=now - timedelta(minutes=5),
                completed_at=now,
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        assert "Duration: 5m" in result
        assert "Work started:" not in result

    def test_dag_duration_uses_work_started_at(self):
        """Test that DAG overview duration prefers work_started_at."""
        now = datetime.utcnow()
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.COMPLETE,
                started_at=now - timedelta(minutes=15),
                work_started_at=now - timedelta(minutes=5),
                completed_at=now,
                cycle_timings=[
                    CycleTiming(
                        cycle=0,
                        started_at=now - timedelta(minutes=5),
                        completed_at=now,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_pipeline_dag(pipeline)

        # Duration should reflect work_started_at (5m), not started_at (15m)
        assert "5m" in result
        assert "15m" not in result

    def test_awaiting_human_phase_shows_awaiting_approval(self):
        """Test detail view shows 'awaiting approval' for AWAITING_HUMAN status."""
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.AWAITING_HUMAN,
            ),
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.REFINE)

        assert "⏸" in result
        assert "awaiting approval" in result
        assert "awaiting_human" not in result


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
        assert "timestamp" in report
        assert report["timestamp"] == report["updated_at"]

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


class TestWaveGrouping:
    """Tests for wave-based agent grouping in the DAG visualization."""

    def test_implement_phase_wave_order(self):
        """Agents are grouped by execution wave in implement phase."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.REVIEWER_CODE, status=AgentExecutionStatus.RUNNING
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER_CONTRACT, status=AgentExecutionStatus.PENDING
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, include_header=False)

        lines = result.split("\n")
        agent_lines = [
            line.strip()
            for line in lines
            if "coder" in line or "tester" in line or "integrator" in line or "reviewer" in line
        ]

        # Coder should be on its own line (wave 1)
        assert any(
            "coder" in line and "tester" not in line and "integrator" not in line
            for line in agent_lines
        )
        # Tester and documenter should be on the same line (wave 2)
        assert any("tester" in line and "documenter" in line for line in agent_lines)
        # Integrator should be on its own line (wave 3)
        assert any(
            "integrator" in line and "coder" not in line and "reviewer" not in line
            for line in agent_lines
        )
        # Reviewers should be after integrator (wave 4)
        assert any("reviewer_code" in line for line in agent_lines)

    def test_plan_phase_wave_order(self):
        """Agents are grouped by execution wave in plan phase."""
        phases = {
            "plan": PhaseExecution(
                phase=PipelinePhase.PLAN,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.ARCHITECT, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.TASK_PLANNER, status=AgentExecutionStatus.RUNNING
                    ),
                    AgentExecution(
                        role=AgentRole.RISK_ANALYST, status=AgentExecutionStatus.RUNNING
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER_PLAN, status=AgentExecutionStatus.PENDING
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.PLAN)
        result = render_pipeline_dag(pipeline, include_header=False)

        lines = result.split("\n")
        agent_lines = [
            line.strip()
            for line in lines
            if "architect" in line or "planner" in line or "analyst" in line or "reviewer" in line
        ]

        # Architect alone (wave 1)
        assert any("architect" in line and "planner" not in line for line in agent_lines)
        # Task planner and risk analyst together (wave 2)
        assert any("task_planner" in line and "risk_analyst" in line for line in agent_lines)
        # Reviewer plan after planner agents (wave 3)
        assert any("reviewer_plan" in line for line in agent_lines)

    def test_compute_wave_order_implement(self):
        """_compute_wave_order returns correct wave groups for implement phase."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.RUNNING),
        ]
        waves = _compute_wave_order(PipelinePhase.IMPLEMENT, agents)

        assert len(waves) == 3  # coder, tester+doc, integrator
        assert len(waves[0]) == 1  # coder
        assert waves[0][0].role == AgentRole.CODER
        assert len(waves[1]) == 2  # tester + documenter
        assert len(waves[2]) == 1  # integrator

    def test_compute_wave_order_unknown_phase_falls_back(self):
        """Phases without defined roles fall back to a single group."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.RUNNING),
        ]
        waves = _compute_wave_order(PipelinePhase.PR, agents)

        # Should return single group (fallback)
        assert len(waves) == 1
        assert len(waves[0]) == 1

    def test_unrecognized_agents_appended(self):
        """Agents not in the dependency graph are appended at the end."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REVIEWER, status=AgentExecutionStatus.RUNNING),
        ]
        waves = _compute_wave_order(PipelinePhase.IMPLEMENT, agents)

        # CODER in wave 1, generic REVIEWER not in graph → appended
        assert waves[0][0].role == AgentRole.CODER
        assert waves[-1][0].role == AgentRole.REVIEWER

    def test_refine_phase_wave_order(self):
        """Refiner is wave 1, reviewers are wave 2 in refine phase."""
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.REVIEWER_AGENT_DESIGN, status=AgentExecutionStatus.RUNNING
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER_REFINE, status=AgentExecutionStatus.RUNNING
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.REFINE)
        result = render_pipeline_dag(pipeline, include_header=False)

        lines = result.split("\n")
        agent_lines = [line.strip() for line in lines if "refiner" in line or "reviewer" in line]

        # Refiner alone (wave 1)
        assert any("refiner" in line and "reviewer" not in line for line in agent_lines)
        # Both reviewers together (wave 2)
        assert any(
            "reviewer_agent_design" in line and "reviewer_refine" in line for line in agent_lines
        )

    def test_compute_wave_order_refine(self):
        """_compute_wave_order returns correct wave groups for refine phase."""
        agents = [
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(
                role=AgentRole.REVIEWER_AGENT_DESIGN, status=AgentExecutionStatus.RUNNING
            ),
            AgentExecution(role=AgentRole.REVIEWER_REFINE, status=AgentExecutionStatus.RUNNING),
        ]
        waves = _compute_wave_order(PipelinePhase.REFINE, agents)

        assert len(waves) == 2  # refiner, reviewers
        assert len(waves[0]) == 1  # refiner
        assert waves[0][0].role == AgentRole.REFINER
        assert len(waves[1]) == 2  # both reviewers
        reviewer_roles = {a.role for a in waves[1]}
        assert reviewer_roles == {AgentRole.REVIEWER_AGENT_DESIGN, AgentRole.REVIEWER_REFINE}

    def test_reviewers_after_planners_in_dag(self):
        """Verify reviewers render after planning agents, not before."""
        phases = {
            "plan": PhaseExecution(
                phase=PipelinePhase.PLAN,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.ARCHITECT, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.TASK_PLANNER, status=AgentExecutionStatus.COMPLETE
                    ),
                    AgentExecution(
                        role=AgentRole.RISK_ANALYST, status=AgentExecutionStatus.COMPLETE
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER_PLAN, status=AgentExecutionStatus.RUNNING
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.PLAN)
        result = render_pipeline_dag(pipeline, include_header=False)

        lines = result.split("\n")
        # Find the line indices containing each agent type
        architect_line = next(i for i, line in enumerate(lines) if "architect" in line)
        planner_line = next(i for i, line in enumerate(lines) if "task_planner" in line)
        reviewer_line = next(i for i, line in enumerate(lines) if "reviewer_plan" in line)

        # Reviewer must come AFTER architect and planner
        assert reviewer_line > architect_line
        assert reviewer_line > planner_line


class TestCycleTimingDisplay:
    """Tests for per-cycle and total timing display."""

    def test_multi_cycle_dag_shows_cycle_and_total(self):
        """DAG box shows [cycle: Xm | total: Ym] when multiple cycles completed."""
        now = datetime.utcnow()
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.COMPLETE,
                started_at=now - timedelta(minutes=25),
                work_started_at=now - timedelta(minutes=5),
                completed_at=now,
                review_cycles=1,
                cycle_timings=[
                    CycleTiming(
                        cycle=0,
                        started_at=now - timedelta(minutes=20),
                        completed_at=now - timedelta(minutes=10),
                    ),
                    CycleTiming(
                        cycle=1,
                        started_at=now - timedelta(minutes=5),
                        completed_at=now,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_pipeline_dag(pipeline)

        # Should show both cycle duration and total duration
        assert "last cycle:" in result
        assert "total:" in result
        # Current cycle is 5m, total is 15m (10m + 5m)
        assert "5m0s" in result
        assert "15m0s" in result

    def test_single_cycle_dag_shows_simple_duration(self):
        """DAG box shows [Xm] with no cycle/total split for single cycle."""
        now = datetime.utcnow()
        phases = {
            "refine": PhaseExecution(
                phase=PipelinePhase.REFINE,
                status=PipelineStatus.COMPLETE,
                started_at=now - timedelta(minutes=10),
                work_started_at=now - timedelta(minutes=5),
                completed_at=now,
                review_cycles=0,
                cycle_timings=[
                    CycleTiming(
                        cycle=0,
                        started_at=now - timedelta(minutes=5),
                        completed_at=now,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_pipeline_dag(pipeline)

        # Should show simple duration, no cycle/total split
        assert "5m0s" in result
        assert "last cycle:" not in result
        assert "total:" not in result

    def test_phase_detail_shows_cycle_breakdown(self):
        """Phase detail view shows per-cycle timing breakdown."""
        now = datetime.utcnow()
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.COMPLETE,
                started_at=now - timedelta(minutes=25),
                work_started_at=now - timedelta(minutes=5),
                completed_at=now,
                review_cycles=2,
                cycle_timings=[
                    CycleTiming(
                        cycle=0,
                        started_at=now - timedelta(minutes=25),
                        completed_at=now - timedelta(minutes=15),
                    ),
                    CycleTiming(
                        cycle=1,
                        started_at=now - timedelta(minutes=12),
                        completed_at=now - timedelta(minutes=5),
                    ),
                    CycleTiming(
                        cycle=2,
                        started_at=now - timedelta(minutes=5),
                        completed_at=now,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        assert "Cycle Timings:" in result
        assert "Cycle 0:" in result
        assert "Cycle 1:" in result
        assert "Cycle 2:" in result
        assert "Total work time:" in result
        # All cycles should show "done"
        assert result.count("(done)") == 3

    def test_phase_detail_running_cycle(self):
        """Phase detail view shows 'running' for incomplete cycle."""
        now = datetime.utcnow()
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                started_at=now - timedelta(minutes=15),
                work_started_at=now - timedelta(minutes=3),
                review_cycles=1,
                cycle_timings=[
                    CycleTiming(
                        cycle=0,
                        started_at=now - timedelta(minutes=10),
                        completed_at=now - timedelta(minutes=5),
                    ),
                    CycleTiming(
                        cycle=1,
                        started_at=now - timedelta(minutes=3),
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        assert "Cycle 0:" in result
        assert "(done)" in result
        assert "Cycle 1:" in result
        assert "(running)" in result


class TestDeduplicateAgents:
    """Tests for _deduplicate_agents helper."""

    def test_no_duplicates(self):
        """Single-run agents are returned unchanged."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.RUNNING),
        ]
        deduped, counts = _deduplicate_agents(agents)

        assert len(deduped) == 2
        assert counts == {"coder": 1, "tester": 1}

    def test_duplicate_roles_collapsed(self):
        """Multiple runs of the same role are collapsed to one entry."""
        agents = [
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
        ]
        deduped, counts = _deduplicate_agents(agents)

        assert len(deduped) == 1
        assert deduped[0].role == AgentRole.REFINER
        assert counts == {"refiner": 2}

    def test_latest_status_kept(self):
        """The latest (last) execution status is used."""
        agents = [
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.FAILED),
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
        ]
        deduped, counts = _deduplicate_agents(agents)

        assert deduped[0].status == AgentExecutionStatus.COMPLETE
        assert counts["refiner"] == 2

    def test_first_seen_order_preserved(self):
        """Deduplicated list preserves first-seen ordering."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
        ]
        deduped, counts = _deduplicate_agents(agents)

        assert len(deduped) == 2
        assert deduped[0].role == AgentRole.CODER
        assert deduped[1].role == AgentRole.REFINER
        assert counts == {"coder": 2, "refiner": 2}

    def test_empty_list(self):
        """Empty input returns empty output."""
        deduped, counts = _deduplicate_agents([])
        assert deduped == []
        assert counts == {}


class TestNonGraphAgentOrdering:
    """Tests for non-graph agent placement relative to reviewers in the DAG."""

    def test_non_graph_agent_before_reviewers_in_implement(self):
        """Non-graph agents appear between integrator and reviewers."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REVIEWER_CONTRACT, status=AgentExecutionStatus.RUNNING),
            AgentExecution(role=AgentRole.REVIEWER_CODE, status=AgentExecutionStatus.RUNNING),
        ]
        waves = _compute_wave_order(PipelinePhase.IMPLEMENT, agents)

        # Find refiner and reviewer wave indices
        refiner_wave = None
        reviewer_wave = None
        for i, wave in enumerate(waves):
            for agent in wave:
                if agent.role == AgentRole.REFINER:
                    refiner_wave = i
                if agent.role.value.startswith("reviewer"):
                    reviewer_wave = i

        assert refiner_wave is not None
        assert reviewer_wave is not None
        assert refiner_wave < reviewer_wave, (
            f"Non-graph agent wave ({refiner_wave}) should precede reviewer wave ({reviewer_wave})"
        )

    def test_non_graph_agent_after_integrator_in_implement(self):
        """Non-graph agent appears after the integrator wave."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.REVIEWER_CONTRACT, status=AgentExecutionStatus.RUNNING),
        ]
        waves = _compute_wave_order(PipelinePhase.IMPLEMENT, agents)

        integrator_wave = None
        refiner_wave = None
        for i, wave in enumerate(waves):
            for agent in wave:
                if agent.role == AgentRole.INTEGRATOR:
                    integrator_wave = i
                if agent.role == AgentRole.REFINER:
                    refiner_wave = i

        assert integrator_wave is not None
        assert refiner_wave is not None
        assert refiner_wave > integrator_wave

    def test_dag_render_non_graph_agent_before_reviewers(self):
        """Full DAG render places non-graph agent line before reviewer lines."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.REVIEWER_CODE, status=AgentExecutionStatus.RUNNING
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER_CONTRACT, status=AgentExecutionStatus.RUNNING
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, include_header=False)
        lines = result.split("\n")

        refiner_line = next(i for i, line in enumerate(lines) if "refiner" in line)
        reviewer_line = next(i for i, line in enumerate(lines) if "reviewer_code" in line)

        assert refiner_line < reviewer_line


class TestRunCountDisplay:
    """Tests for run count display in DAG and phase detail views."""

    def test_duplicate_role_shows_count(self):
        """Two runs of the same role render with '×2' instead of two entries."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, include_header=False)

        # Should show single refiner entry with count
        assert "\u00d7" + "2" in result or "×2" in result
        # Should NOT show refiner twice on separate entries
        assert result.count("refiner") == 1

    def test_single_run_no_count(self):
        """Agents with a single run show no count suffix."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "coder" in result
        assert "\u00d7" not in result

    def test_ascii_count_uses_x(self):
        """ASCII mode uses 'x' instead of '×' for the count."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, use_ascii=True, include_header=False)

        assert "x2" in result
        assert "\u00d7" not in result

    def test_phase_detail_shows_all_runs(self):
        """Phase detail view shows every run without deduplication."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.REFINER,
                        status=AgentExecutionStatus.FAILED,
                        error="lint failure",
                    ),
                    AgentExecution(
                        role=AgentRole.REFINER,
                        status=AgentExecutionStatus.COMPLETE,
                        commit="abc12345",
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        # Should show total agent count (all runs), not unique roles
        assert "Agents (3):" in result
        # Both refiner runs should appear
        assert result.count("refiner") == 2
        # Commit and error from different runs are preserved
        assert "abc12345" in result
        assert "lint failure" in result
        # No dedup multiplier in the detail view
        assert "×" not in result

    def test_phase_detail_shows_all_runs_for_in_graph_agents(self):
        """Phase detail shows every run for roles in the dependency graph.

        Regression test: _compute_wave_order uses a dict keyed by role value,
        which overwrites earlier entries for the same role.  The detail view
        must not route through _compute_wave_order so that duplicate in-graph
        roles (coder, tester, etc.) are preserved.
        """
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.FAILED,
                        error="build error",
                    ),
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.COMPLETE,
                        commit="abc12345",
                    ),
                    AgentExecution(
                        role=AgentRole.TESTER,
                        status=AgentExecutionStatus.COMPLETE,
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases)
        result = render_phase_detail(pipeline, PipelinePhase.IMPLEMENT)

        # Header should report all 3 agent entries
        assert "Agents (3):" in result
        # Both coder runs must appear
        assert result.count("coder") == 2
        # Commit from second run and error from first run are preserved
        assert "abc12345" in result
        assert "build error" in result

    def test_full_scenario_from_issue(self):
        """Reproduce the exact scenario from issue #769 with valid roles."""
        phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.INTEGRATOR, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(role=AgentRole.REFINER, status=AgentExecutionStatus.COMPLETE),
                    AgentExecution(
                        role=AgentRole.REVIEWER_CODE, status=AgentExecutionStatus.RUNNING
                    ),
                    AgentExecution(
                        role=AgentRole.REVIEWER_CONTRACT, status=AgentExecutionStatus.RUNNING
                    ),
                ],
            )
        }
        pipeline = create_test_pipeline(phases=phases, current_phase=PipelinePhase.IMPLEMENT)
        result = render_pipeline_dag(pipeline, include_header=False)
        lines = result.split("\n")

        # Refiner should appear once with ×2, before reviewers
        assert result.count("refiner") == 1
        assert "×2" in result

        refiner_line = next(i for i, line in enumerate(lines) if "refiner" in line)
        reviewer_line = next(i for i, line in enumerate(lines) if "reviewer" in line)
        assert refiner_line < reviewer_line

        # Ordering should be: coder, tester+documenter, integrator, refiner, reviewers
        coder_line = next(i for i, line in enumerate(lines) if "coder" in line)
        integrator_line = next(i for i, line in enumerate(lines) if "integrator" in line)
        assert coder_line < integrator_line < refiner_line < reviewer_line


# --- Tier 3 DAG visualization tests ---


class TestDeriveSubphaseStatus:
    """Tests for _derive_subphase_status helper."""

    def test_empty_agents_returns_pending(self):
        """No agents means pending status."""
        assert _derive_subphase_status([]) == PipelineStatus.PENDING

    def test_all_complete(self):
        """All agents complete returns COMPLETE."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.COMPLETE),
        ]
        assert _derive_subphase_status(agents) == PipelineStatus.COMPLETE

    def test_any_running(self):
        """Any running agent returns RUNNING."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.RUNNING),
        ]
        assert _derive_subphase_status(agents) == PipelineStatus.RUNNING

    def test_any_failed(self):
        """Any failed agent returns FAILED (highest priority)."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.RUNNING),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.FAILED),
        ]
        assert _derive_subphase_status(agents) == PipelineStatus.FAILED

    def test_all_pending(self):
        """All pending agents returns PENDING."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.PENDING),
        ]
        assert _derive_subphase_status(agents) == PipelineStatus.PENDING

    def test_mixed_pending_and_complete(self):
        """Pending takes priority over complete."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.PENDING),
        ]
        assert _derive_subphase_status(agents) == PipelineStatus.PENDING


class TestRenderSubphaseBox:
    """Tests for _render_subphase_box function."""

    def test_basic_box_with_name(self):
        """Renders a box with the phase name and status symbol."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
        ]
        lines = _render_subphase_box("phase-1", "Auth", agents)

        text = "\n".join(lines)
        assert "Auth" in text
        assert "✓" in text  # complete symbol
        assert "coder" in text
        assert lines[0].startswith("┌")
        assert lines[-1].startswith("└")

    def test_fallback_to_phase_id(self):
        """Uses phase_id when phase_name is None."""
        lines = _render_subphase_box("phase-1", None, [])
        text = "\n".join(lines)
        assert "phase-1" in text

    def test_multiple_agents(self):
        """Shows all agents with their status symbols."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.RUNNING),
        ]
        lines = _render_subphase_box("phase-1", "Auth", agents)
        text = "\n".join(lines)
        assert "✓ coder" in text
        assert "▶ tester" in text

    def test_ascii_mode(self):
        """ASCII mode uses correct border characters."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
        ]
        lines = _render_subphase_box("phase-1", "Auth", agents, use_ascii=True)
        text = "\n".join(lines)
        assert lines[0].startswith("+")
        assert lines[-1].startswith("+")
        assert "+ coder" in text

    def test_pending_status(self):
        """Empty agents show pending symbol."""
        lines = _render_subphase_box("phase-1", "Auth", [])
        text = "\n".join(lines)
        assert "○" in text

    def test_failed_status(self):
        """Failed agent shows failed symbol on the box."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.FAILED),
        ]
        lines = _render_subphase_box("phase-1", "Auth", agents)
        text = "\n".join(lines)
        assert "✗" in text

    def test_deduplicated_agents_show_count(self):
        """Duplicate agent runs show count."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.FAILED),
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
        ]
        lines = _render_subphase_box("phase-1", "Auth", agents)
        text = "\n".join(lines)
        assert "×2" in text
        assert text.count("coder") == 1


class TestRenderSideBySide:
    """Tests for _render_side_by_side function."""

    def test_empty_input(self):
        """Empty input returns empty list."""
        assert _render_side_by_side([]) == []

    def test_single_box(self):
        """Single box returned as-is."""
        box = ["┌──┐", "│AB│", "└──┘"]
        result = _render_side_by_side([box])
        assert result == box

    def test_two_equal_height_boxes(self):
        """Two boxes of same height are concatenated."""
        box1 = ["┌──┐", "│AB│", "└──┘"]
        box2 = ["┌──┐", "│CD│", "└──┘"]
        result = _render_side_by_side([box1, box2], spacing=2)

        assert len(result) == 3
        # Each line should contain content from both boxes
        assert "AB" in result[1]
        assert "CD" in result[1]

    def test_different_height_boxes(self):
        """Shorter box is padded to match taller one."""
        box1 = ["┌──┐", "│AB│", "│CD│", "└──┘"]
        box2 = ["┌──┐", "│EF│", "└──┘"]
        result = _render_side_by_side([box1, box2], spacing=2)

        assert len(result) == 4  # Height of tallest box
        # Shorter box's extra rows should be blank padding
        assert "EF" not in result[2]

    def test_three_boxes(self):
        """Three boxes are correctly concatenated."""
        box1 = ["┌──┐", "│AB│", "└──┘"]
        box2 = ["┌──┐", "│CD│", "└──┘"]
        box3 = ["┌──┐", "│EF│", "└──┘"]
        result = _render_side_by_side([box1, box2, box3], spacing=2)

        assert len(result) == 3
        assert "AB" in result[1]
        assert "CD" in result[1]
        assert "EF" in result[1]

    def test_consistent_line_widths(self):
        """All output lines have the same width."""
        box1 = ["┌──┐", "│AB│", "└──┘"]
        box2 = ["┌────┐", "│CDEF│", "└────┘"]
        result = _render_side_by_side([box1, box2], spacing=2)

        widths = [len(line) for line in result]
        assert len(set(widths)) == 1


class TestRenderFanOut:
    """Tests for _render_fan_out function."""

    def test_single_width_returns_empty(self):
        """Single box returns empty (no fan-out needed)."""
        assert _render_fan_out([20]) == []

    def test_two_branches(self):
        """Two branches produce a 3-line fan-out."""
        result = _render_fan_out([20, 20], spacing=2)
        assert len(result) == 3
        # Line 1 is the stem
        assert "│" in result[0]
        # Line 2 is the horizontal bar
        assert "┌" in result[1]
        assert "┐" in result[1]
        assert "┴" in result[1]
        # Line 3 has vertical stems
        assert result[2].count("│") == 2

    def test_three_branches(self):
        """Three branches produce correct connector."""
        result = _render_fan_out([20, 20, 20], spacing=2)
        assert len(result) == 3
        # Should have three vertical stems in line 3
        assert result[2].count("│") == 3
        # Line 2 should have corner characters and a tee
        assert "┌" in result[1]
        assert "┐" in result[1]

    def test_ascii_mode(self):
        """ASCII mode uses correct characters."""
        result = _render_fan_out([20, 20], spacing=2, use_ascii=True)
        assert len(result) == 3
        assert "|" in result[0]
        assert "+" in result[1]
        assert result[2].count("|") == 2


class TestRenderFanIn:
    """Tests for _render_fan_in function."""

    def test_single_width_returns_empty(self):
        """Single box returns empty (no fan-in needed)."""
        assert _render_fan_in([20]) == []

    def test_two_branches(self):
        """Two branches produce a 3-line fan-in."""
        result = _render_fan_in([20, 20], spacing=2)
        assert len(result) == 3
        # Line 1 has vertical stems
        assert result[0].count("│") == 2
        # Line 2 is the horizontal bar
        assert "└" in result[1]
        assert "┘" in result[1]
        assert "┬" in result[1]
        # Line 3 is the single stem
        assert "│" in result[2]

    def test_three_branches(self):
        """Three branches produce correct connector."""
        result = _render_fan_in([20, 20, 20], spacing=2)
        assert len(result) == 3
        assert result[0].count("│") == 3

    def test_ascii_mode(self):
        """ASCII mode uses correct characters."""
        result = _render_fan_in([20, 20], spacing=2, use_ascii=True)
        assert len(result) == 3
        assert result[0].count("|") == 2
        assert "+" in result[1]
        assert "|" in result[2]

    def test_symmetric_with_fan_out(self):
        """Fan-in and fan-out for same widths produce symmetric connectors."""
        widths = [20, 20]
        out = _render_fan_out(widths, spacing=2)
        fin = _render_fan_in(widths, spacing=2)

        # Both should be 3 lines
        assert len(out) == len(fin) == 3
        # Stem positions should match
        assert out[2] == fin[0]  # vertical stems match


class TestRenderTier3Implement:
    """Tests for _render_tier3_implement orchestrator function."""

    def test_sequential_wave(self):
        """Single-phase waves render as centered boxes."""
        pipeline = Pipeline(
            id="t3-test",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1"], ["phase-2"]],
            plan_phase_names={"phase-1": "Auth", "phase-2": "API"},
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                    plan_phase_id="phase-1",
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        assert "Implement (Tier 3)" in text
        assert "Auth" in text
        assert "API" in text
        assert ">>>" in text

    def test_parallel_wave_has_fan_connectors(self):
        """Parallel waves include fan-out and fan-in connectors."""
        pipeline = Pipeline(
            id="t3-test",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1", "phase-2"]],
            plan_phase_names={"phase-1": "Auth", "phase-2": "API"},
        )
        lines = _render_tier3_implement(pipeline, None, is_current=True)
        text = "\n".join(lines)

        # Should have fan-out and fan-in connectors
        assert "┌" in text or "+" in text
        assert "┘" in text or "+" in text
        # Both phases should appear
        assert "Auth" in text
        assert "API" in text

    def test_top_level_agents_box(self):
        """Top-level agents render in a separate box."""
        pipeline = Pipeline(
            id="t3-test",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1"]],
            plan_phase_names={"phase-1": "Auth"},
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                    plan_phase_id="phase-1",
                ),
                AgentExecution(
                    role=AgentRole.INTEGRATOR,
                    status=AgentExecutionStatus.PENDING,
                ),
                AgentExecution(
                    role=AgentRole.REVIEWER_CONTRACT,
                    status=AgentExecutionStatus.PENDING,
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        assert "Pipeline agents" in text
        assert "integrator" in text
        assert "reviewer_contract" in text

    def test_no_top_level_agents(self):
        """No top-level agents means no Pipeline agents box."""
        pipeline = Pipeline(
            id="t3-test",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1"]],
            plan_phase_names={"phase-1": "Auth"},
        )
        phase_exec = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.COMPLETE,
                    plan_phase_id="phase-1",
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        assert "Pipeline agents" not in text

    def test_ascii_mode(self):
        """ASCII mode uses correct characters throughout."""
        pipeline = Pipeline(
            id="t3-test",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1", "phase-2"]],
            plan_phase_names={"phase-1": "Auth", "phase-2": "API"},
        )
        lines = _render_tier3_implement(pipeline, None, is_current=True, use_ascii=True)
        text = "\n".join(lines)

        # ASCII borders
        assert "+" in text
        assert "===" in text
        # No Unicode
        assert "┌" not in text
        assert "└" not in text
        assert "═" not in text


class TestRenderPipelineDagTier3:
    """Integration tests for render_pipeline_dag with Tier 3 pipelines."""

    def _make_tier3_pipeline(
        self,
        waves=None,
        names=None,
        agents=None,
        current_phase=PipelinePhase.IMPLEMENT,
    ):
        """Helper to create a Tier 3 pipeline for testing."""
        if waves is None:
            waves = [["phase-1"], ["phase-2", "phase-3"], ["phase-4"]]
        if names is None:
            names = {
                "phase-1": "Auth",
                "phase-2": "API",
                "phase-3": "UI",
                "phase-4": "Integration",
            }
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
        if agents is not None:
            phases["implement"] = PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=agents,
            )
        return Pipeline(
            id="tier3-test",
            status=PipelineStatus.RUNNING,
            current_phase=current_phase,
            plan_phase_waves=waves,
            plan_phase_names=names,
            phases=phases,
        )

    def test_full_dag_has_all_phases(self):
        """Full DAG includes Refine, Plan, expanded Implement, and PR."""
        pipeline = self._make_tier3_pipeline()
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "Refine" in result
        assert "Plan" in result
        assert "Implement (Tier 3)" in result
        assert "PR" in result

    def test_subphase_boxes_in_wave_order(self):
        """Sub-phase boxes appear in wave order."""
        pipeline = self._make_tier3_pipeline()
        result = render_pipeline_dag(pipeline, include_header=False)
        lines = result.split("\n")

        auth_line = next(i for i, line in enumerate(lines) if "Auth" in line)
        api_line = next(i for i, line in enumerate(lines) if "API" in line)
        integration_line = next(i for i, line in enumerate(lines) if "Integration" in line)

        # Wave 1 (Auth) before Wave 2 (API, UI) before Wave 3 (Integration)
        assert auth_line < api_line < integration_line

    def test_parallel_wave_has_connectors(self):
        """Parallel waves have fan-out and fan-in connectors."""
        pipeline = self._make_tier3_pipeline()
        result = render_pipeline_dag(pipeline, include_header=False)

        # Fan-out/fan-in connectors for the parallel wave
        assert "┌" in result
        assert "┘" in result

    def test_agents_in_correct_subphase(self):
        """Agents appear in their correct sub-phase boxes."""
        agents = [
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                plan_phase_id="phase-1",
            ),
            AgentExecution(
                role=AgentRole.TESTER,
                status=AgentExecutionStatus.COMPLETE,
                plan_phase_id="phase-1",
            ),
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                plan_phase_id="phase-2",
            ),
        ]
        pipeline = self._make_tier3_pipeline(agents=agents)
        result = render_pipeline_dag(pipeline, include_header=False)

        # Auth box should contain coder and tester
        lines = result.split("\n")
        auth_line = next(i for i, line in enumerate(lines) if "Auth" in line)
        api_line = next(i for i, line in enumerate(lines) if "API" in line)

        # Between Auth and API, we should find coder and tester
        auth_section = "\n".join(lines[auth_line:api_line])
        assert "coder" in auth_section
        assert "tester" in auth_section

    def test_top_level_agents_after_subphases(self):
        """Top-level agents appear after all sub-phase waves."""
        agents = [
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                plan_phase_id="phase-1",
            ),
            AgentExecution(
                role=AgentRole.INTEGRATOR,
                status=AgentExecutionStatus.PENDING,
            ),
        ]
        pipeline = self._make_tier3_pipeline(agents=agents)
        result = render_pipeline_dag(pipeline, include_header=False)
        lines = result.split("\n")

        auth_line = next(i for i, line in enumerate(lines) if "Auth" in line)
        integrator_line = next(i for i, line in enumerate(lines) if "integrator" in line)

        assert integrator_line > auth_line

    def test_ascii_mode_full_dag(self):
        """Full DAG renders correctly in ASCII mode."""
        pipeline = self._make_tier3_pipeline()
        result = render_pipeline_dag(pipeline, use_ascii=True, include_header=False)

        # ASCII characters only
        assert "+" in result
        assert "|" in result
        # No Unicode
        assert "│" not in result
        assert "╔" not in result
        assert "┌" not in result

    def test_header_present_with_tier3(self):
        """Header is included when include_header=True."""
        pipeline = self._make_tier3_pipeline()
        pipeline.repo = "test/repo"
        pipeline.branch = "egg/test"
        result = render_pipeline_dag(pipeline, include_header=True)

        assert "Pipeline: tier3-test" in result
        assert "Repository: test/repo" in result
        assert "Branch: egg/test" in result


class TestTier3BackwardCompatibility:
    """Regression tests ensuring Tier 1/2 pipelines render identically."""

    def _make_tier1_pipeline(self, agents=None):
        """Create a standard Tier 1/2 pipeline without plan_phase_waves."""
        phases = {}
        if agents is not None:
            phases["implement"] = PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                agents=agents,
            )
        return Pipeline(
            id="tier1-test",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            phases=phases,
        )

    def test_no_waves_renders_standard_box(self):
        """Pipeline without plan_phase_waves renders standard Implement box."""
        pipeline = self._make_tier1_pipeline()
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "Implement" in result
        assert "Tier 3" not in result
        assert "╔" in result  # Standard double-border box

    def test_with_agents_renders_standard(self):
        """Pipeline with agents but no waves renders standard agent list."""
        agents = [
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.COMPLETE),
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.RUNNING),
        ]
        pipeline = self._make_tier1_pipeline(agents=agents)
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "✓ coder" in result
        assert "▶ tester" in result
        assert "Tier 3" not in result

    def test_subphase_agents_in_standard_mode(self):
        """Agents with plan_phase_id but no waves still render in standard box."""
        agents = [
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                plan_phase_id="phase-1",
            ),
        ]
        pipeline = self._make_tier1_pipeline(agents=agents)
        result = render_pipeline_dag(pipeline, include_header=False)

        # Should use standard rendering with sub-phase grouping
        assert "Tier 3" not in result
        assert "phase-1:" in result  # Standard sub-phase grouping
        assert "coder" in result


class TestTier3EdgeCases:
    """Edge case tests for Tier 3 DAG visualization."""

    def test_single_wave_no_connectors(self):
        """Single-wave Tier 3 pipeline has no fan-out/fan-in."""
        pipeline = Pipeline(
            id="t3-single",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1"]],
            plan_phase_names={"phase-1": "Auth"},
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "Auth" in result
        assert "Tier 3" in result
        # No fan-out/fan-in connectors (no ┴ or ┬ characters used by connectors)
        impl_section = result.split("Tier 3")[1].split("PR")[0]
        assert "┴" not in impl_section
        assert "┬" not in impl_section

    def test_empty_plan_phase_waves(self):
        """Empty plan_phase_waves list falls back to standard rendering."""
        pipeline = Pipeline(
            id="t3-empty",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[],
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # Empty list is falsy, should use standard rendering
        assert "Tier 3" not in result

    def test_missing_plan_phase_names(self):
        """Missing plan_phase_names uses phase IDs as fallback."""
        pipeline = Pipeline(
            id="t3-nonames",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1", "phase-2"]],
            # plan_phase_names is None
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "phase-1" in result
        assert "phase-2" in result

    def test_four_parallel_phases(self):
        """Four parallel phases render side-by-side within max limit."""
        pipeline = Pipeline(
            id="t3-4way",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1", "phase-2", "phase-3", "phase-4"]],
            plan_phase_names={
                "phase-1": "A",
                "phase-2": "B",
                "phase-3": "C",
                "phase-4": "D",
            },
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "A" in result
        assert "B" in result
        assert "C" in result
        assert "D" in result

    def test_five_parallel_phases_wraps(self):
        """Five parallel phases wrap into multiple rows."""
        pipeline = Pipeline(
            id="t3-5way",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1", "phase-2", "phase-3", "phase-4", "phase-5"]],
            plan_phase_names={
                "phase-1": "A",
                "phase-2": "B",
                "phase-3": "C",
                "phase-4": "D",
                "phase-5": "E",
            },
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # All phases should appear
        for name in "ABCDE":
            assert name in result

    def test_long_phase_name(self):
        """Long phase names are handled without error."""
        pipeline = Pipeline(
            id="t3-long",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1"]],
            plan_phase_names={"phase-1": "A very long phase name that exceeds typical widths"},
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "A very long phase name" in result

    def test_none_phase_exec(self):
        """Tier 3 rendering handles None phase_exec gracefully."""
        pipeline = Pipeline(
            id="t3-none",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            plan_phase_waves=[["phase-1"]],
            plan_phase_names={"phase-1": "Auth"},
            # No implement phase in phases dict
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        assert "Auth" in result
        assert "Tier 3" in result
