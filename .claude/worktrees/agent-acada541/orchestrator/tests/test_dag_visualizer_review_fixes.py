"""
Tests verifying the three code review fixes for Tier 3 DAG visualization.

Review issue 1: Orphaned agents (plan_phase_id not in any wave) were silently
    dropped from visualization. Fix: move them to top_level_agents.

Review issue 2: Fan-out/fan-in stem was at total_width // 2 instead of
    bar_center, causing misalignment with asymmetric box widths.
    Fix: use bar_center for both stem and junction.

Review issue 3: Tee character overwrite with 3+ equal-width boxes when
    bar_center coincides with an intermediate branch center.
    Fix: use cross junction (┼ / +) when center equals a branch.

Also covers the minor fix: plan_phase_names moved inside the phase_waves guard.
"""

from dag_visualizer import (
    _render_fan_in,
    _render_fan_out,
    _render_tier3_implement,
    render_pipeline_dag,
)
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
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
        "id": "review-fix-test",
        "issue_number": 1,
        "repo": "test/repo",
        "branch": "egg/test",
        "status": PipelineStatus.RUNNING,
        "current_phase": PipelinePhase.IMPLEMENT,
    }
    defaults.update(kwargs)
    return Pipeline(**defaults)


# ===========================================================================
# Review Issue 1: Orphaned agents silently dropped
# ===========================================================================


class TestOrphanedAgentsVisibility:
    """Verify agents with plan_phase_id not in any wave are rendered."""

    def test_orphaned_agent_appears_in_output(self):
        """Orphaned agent must appear in the rendered DAG — not silently dropped."""
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

        # The orphaned tester MUST appear in output
        assert "tester" in result
        # It should be in the Pipeline agents box (top-level)
        assert "Pipeline agents" in result
        # The valid agent should be in its correct sub-phase
        assert "Auth" in result
        assert "coder" in result

    def test_orphaned_agent_in_tier3_implement_directly(self):
        """Test _render_tier3_implement partitions orphans correctly."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2"]],
            plan_phase_names={"p1": "Auth", "p2": "API"},
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
                    role=AgentRole.TESTER,
                    status=AgentExecutionStatus.RUNNING,
                    plan_phase_id="p2",
                ),
                # Orphaned: plan_phase_id doesn't match p1 or p2
                AgentExecution(
                    role=AgentRole.DOCUMENTER,
                    status=AgentExecutionStatus.COMPLETE,
                    plan_phase_id="deleted-phase",
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        # Orphaned documenter should be in Pipeline agents box
        assert "documenter" in text
        assert "Pipeline agents" in text
        # Regular agents should be in their sub-phase boxes
        assert "coder" in text
        assert "tester" in text

    def test_multiple_orphaned_agents(self):
        """Multiple orphaned agents all appear in Pipeline agents box."""
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
                    role=AgentRole.TESTER,
                    status=AgentExecutionStatus.FAILED,
                    plan_phase_id="orphan-1",
                ),
                AgentExecution(
                    role=AgentRole.DOCUMENTER,
                    status=AgentExecutionStatus.PENDING,
                    plan_phase_id="orphan-2",
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        assert "tester" in text
        assert "documenter" in text
        assert "Pipeline agents" in text

    def test_orphaned_and_null_phase_id_both_in_top_level(self):
        """Both orphaned (bad phase_id) and null (no phase_id) go to top-level."""
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
                # Orphaned: bad phase_id
                AgentExecution(
                    role=AgentRole.TESTER,
                    status=AgentExecutionStatus.COMPLETE,
                    plan_phase_id="nonexistent",
                ),
                # Top-level: no phase_id
                AgentExecution(
                    role=AgentRole.INTEGRATOR,
                    status=AgentExecutionStatus.PENDING,
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        # Both should be in Pipeline agents box
        assert "tester" in text
        assert "integrator" in text
        assert "Pipeline agents" in text

    def test_all_agents_orphaned_renders_only_top_level(self):
        """When ALL agents are orphaned, they all go to Pipeline agents box."""
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
                    plan_phase_id="wrong-phase",
                ),
            ],
        )
        lines = _render_tier3_implement(pipeline, phase_exec, is_current=True)
        text = "\n".join(lines)

        # The Auth sub-phase box should exist but have no agents
        assert "Auth" in text
        # Orphaned coder should be in Pipeline agents
        assert "coder" in text
        assert "Pipeline agents" in text


# ===========================================================================
# Review Issue 2: Fan-out/fan-in stem misalignment
# ===========================================================================


class TestStemAlignment:
    """Verify stem position uses bar_center, not total_width // 2."""

    def test_fan_out_stem_at_bar_center_asymmetric(self):
        """With asymmetric widths, stem must be at bar_center, not total_width // 2."""
        # box_widths = [10, 30], spacing = 2
        # total_width = 10 + 2 + 30 = 42
        # centers = [5, 42 - 30//2] = [5, 27]  (offset: 0+10//2=5, 12+30//2=27)
        # bar_center = (5 + 27) // 2 = 16
        # total_width // 2 = 21 (the old, wrong value)
        result = _render_fan_out([10, 30], spacing=2)

        stem_line = result[0]
        bar_line = result[1]

        # Find stem position in line 1
        stem_pos = stem_line.index("│")

        # Find junction position in bar (line 2) — should be ┴
        junction_pos = bar_line.index("┴")

        # Stem and junction MUST be at the same column
        assert stem_pos == junction_pos, (
            f"Stem at column {stem_pos} but junction at column {junction_pos}"
        )

        # Verify it's NOT at total_width // 2 = 21
        total_width = 10 + 2 + 30
        assert stem_pos != total_width // 2, (
            "Stem should NOT be at total_width // 2 (the old broken behavior)"
        )

    def test_fan_in_stem_at_bar_center_asymmetric(self):
        """Fan-in stem must also use bar_center, not total_width // 2."""
        result = _render_fan_in([10, 30], spacing=2)

        stem_line = result[2]  # Fan-in: stem is line 3
        bar_line = result[1]

        stem_pos = stem_line.index("│")
        junction_pos = bar_line.index("┬")

        assert stem_pos == junction_pos, (
            f"Stem at column {stem_pos} but junction at column {junction_pos}"
        )

    def test_fan_out_stem_matches_bar_center_exact(self):
        """Verify exact bar_center calculation for known widths."""
        # widths = [10, 30], spacing = 2
        # centers = [5, 27]
        # bar_center = (5 + 27) // 2 = 16
        result = _render_fan_out([10, 30], spacing=2)

        stem_pos = result[0].index("│")
        assert stem_pos == 16

    def test_fan_in_stem_matches_bar_center_exact(self):
        """Verify exact bar_center calculation for fan-in."""
        result = _render_fan_in([10, 30], spacing=2)

        stem_pos = result[2].index("│")
        assert stem_pos == 16

    def test_symmetric_widths_stem_at_center(self):
        """With symmetric widths, bar_center and total_width // 2 agree."""
        result = _render_fan_out([20, 20], spacing=2)

        stem_pos = result[0].index("│")
        total_width = 20 + 2 + 20
        # For symmetric widths, both calculations should agree
        assert stem_pos == total_width // 2

    def test_fan_out_highly_asymmetric(self):
        """Extreme asymmetry: [5, 50] — stem must track bar_center."""
        result = _render_fan_out([5, 50], spacing=2)

        stem_pos = result[0].index("│")
        bar_line = result[1]

        # Find the junction
        junction_pos = bar_line.index("┴")
        assert stem_pos == junction_pos

        # Verify stem is NOT at total_width // 2
        total_width = 5 + 2 + 50
        assert stem_pos != total_width // 2

    def test_fan_in_three_boxes_asymmetric_alignment(self):
        """Three boxes with different widths — stem and junction aligned."""
        result = _render_fan_in([10, 20, 40], spacing=2)

        stem_pos = result[2].index("│")

        # Compute centers: [0+5, 12+10, 34+20] = [5, 22, 54]
        centers = [5, 22, 54]
        bar_center = (centers[0] + centers[-1]) // 2  # (5 + 54) // 2 = 29
        assert stem_pos == bar_center

    def test_fan_out_ascii_stem_aligned(self):
        """ASCII mode also aligns stem with junction."""
        result = _render_fan_out([10, 30], spacing=2, use_ascii=True)

        stem_pos = result[0].index("|")
        bar_line = result[1]
        # In ASCII mode, junction is "+"
        # Find the + that's at bar_center (not at edges)
        # Edges are also +, so we need to find the one at the expected position
        bar_center = (5 + 27) // 2  # = 16
        assert stem_pos == bar_center
        assert bar_line[bar_center] == "+"


# ===========================================================================
# Review Issue 3: Tee character overwrite with 3+ equal-width boxes
# ===========================================================================


class TestTeeOverwriteCrossJunction:
    """Verify cross junction (┼) is used when bar_center coincides with a branch."""

    def test_fan_out_three_equal_boxes_uses_cross(self):
        """3 equal-width boxes: middle branch center = bar_center → ┼."""
        result = _render_fan_out([20, 20, 20], spacing=2)
        bar_line = result[1]

        # With 3 equal boxes of width 20, spacing 2:
        # centers = [10, 32, 54]
        # bar_center = (10 + 54) // 2 = 32
        # centers[1] = 32 = bar_center → cross junction
        assert "┼" in bar_line

    def test_fan_in_three_equal_boxes_uses_cross(self):
        """3 equal-width boxes: middle branch center = bar_center → ┼."""
        result = _render_fan_in([20, 20, 20], spacing=2)
        bar_line = result[1]

        assert "┼" in bar_line

    def test_fan_out_three_equal_no_overwrite(self):
        """Cross junction should NOT be overwritten by branch tee."""
        result = _render_fan_out([20, 20, 20], spacing=2)
        bar_line = result[1]

        # The bar_center position should have ┼, not ┬ (branch tee)
        centers = [10, 32, 54]
        bar_center = (centers[0] + centers[-1]) // 2  # 32
        assert bar_line[bar_center] == "┼"
        # ┬ should NOT appear at bar_center
        assert bar_line[bar_center] != "┬"

    def test_fan_in_three_equal_no_overwrite(self):
        """Cross junction should NOT be overwritten by branch tee in fan-in."""
        result = _render_fan_in([20, 20, 20], spacing=2)
        bar_line = result[1]

        centers = [10, 32, 54]
        bar_center = (centers[0] + centers[-1]) // 2  # 32
        assert bar_line[bar_center] == "┼"
        # ┴ (branch tee) should NOT appear at bar_center
        assert bar_line[bar_center] != "┴"

    def test_fan_out_two_equal_no_cross_needed(self):
        """2 equal boxes: bar_center is NOT an intermediate branch → ┴ is correct."""
        result = _render_fan_out([20, 20], spacing=2)
        bar_line = result[1]

        # No ┼ should appear — the center is between the two branches, not on one
        assert "┼" not in bar_line
        # Normal stem junction ┴ should be used
        assert "┴" in bar_line

    def test_fan_out_four_equal_boxes_center_not_on_branch(self):
        """4 equal boxes: bar_center falls between branches 2 and 3 → no ┼."""
        result = _render_fan_out([20, 20, 20, 20], spacing=2)
        bar_line = result[1]

        # With 4 equal boxes of width 20, spacing 2:
        # centers = [10, 32, 54, 76]
        # bar_center = (10 + 76) // 2 = 43
        # 43 is not in centers[1:-1] = {32, 54}
        # So no cross junction needed
        assert "┼" not in bar_line
        assert "┴" in bar_line

    def test_fan_out_ascii_cross_junction(self):
        """ASCII mode: 3 equal boxes uses + for cross junction."""
        result = _render_fan_out([20, 20, 20], spacing=2, use_ascii=True)
        bar_line = result[1]

        # In ASCII mode, all junction chars are "+", so we verify by position
        centers = [10, 32, 54]
        bar_center = (centers[0] + centers[-1]) // 2  # 32
        assert bar_line[bar_center] == "+"

    def test_fan_out_five_equal_boxes_center_on_middle_branch(self):
        """5 equal boxes: bar_center = center of middle (3rd) box → ┼."""
        result = _render_fan_out([20, 20, 20, 20, 20], spacing=2)
        bar_line = result[1]

        # centers = [10, 32, 54, 76, 98]
        # bar_center = (10 + 98) // 2 = 54
        # centers[1:-1] = {32, 54, 76}
        # 54 is in intermediate centers → cross junction
        assert "┼" in bar_line

    def test_fan_in_five_equal_boxes_center_on_middle_branch(self):
        """5 equal boxes fan-in: bar_center on middle branch → ┼."""
        result = _render_fan_in([20, 20, 20, 20, 20], spacing=2)
        bar_line = result[1]

        assert "┼" in bar_line


# ===========================================================================
# Additional connector correctness tests
# ===========================================================================


class TestConnectorCorrectnessDetailed:
    """Detailed structural correctness tests for fan connectors."""

    def test_fan_out_left_corner_at_first_center(self):
        """Fan-out: left corner ┌ must be at the first box center."""
        result = _render_fan_out([10, 30], spacing=2)
        bar_line = result[1]

        # First center is 10 // 2 = 5
        assert bar_line[5] == "┌"

    def test_fan_out_right_corner_at_last_center(self):
        """Fan-out: right corner ┐ must be at the last box center."""
        result = _render_fan_out([10, 30], spacing=2)
        bar_line = result[1]

        # Last center: offset=12, width=30, center=12+15=27
        assert bar_line[27] == "┐"

    def test_fan_in_left_corner_at_first_center(self):
        """Fan-in: left corner └ must be at the first box center."""
        result = _render_fan_in([10, 30], spacing=2)
        bar_line = result[1]

        assert bar_line[5] == "└"

    def test_fan_in_right_corner_at_last_center(self):
        """Fan-in: right corner ┘ must be at the last box center."""
        result = _render_fan_in([10, 30], spacing=2)
        bar_line = result[1]

        assert bar_line[27] == "┘"

    def test_fan_out_branch_stems_at_box_centers(self):
        """Fan-out line 3: vertical stems at each box center."""
        result = _render_fan_out([10, 20, 30], spacing=2)
        stems_line = result[2]

        # centers = [5, 22, 49]  (0+5, 12+10, 34+15)
        centers = [5, 22, 49]
        for c in centers:
            assert stems_line[c] == "│", f"Expected │ at position {c}, got {stems_line[c]!r}"

    def test_fan_in_branch_stems_at_box_centers(self):
        """Fan-in line 1: vertical stems at each box center."""
        result = _render_fan_in([10, 20, 30], spacing=2)
        stems_line = result[0]

        # centers = [5, 22, 49]  (0+5, 12+10, 34+15)
        centers = [5, 22, 49]
        for c in centers:
            assert stems_line[c] == "│", f"Expected │ at position {c}, got {stems_line[c]!r}"

    def test_fan_out_horizontal_bar_spans_between_edges(self):
        """Horizontal bar should span from first center to last center."""
        result = _render_fan_out([10, 30], spacing=2)
        bar_line = result[1]

        # Between centers[0]=5 and centers[-1]=27, all positions should be non-space
        for i in range(5, 28):
            assert bar_line[i] != " ", f"Position {i} should not be blank in bar"

    def test_fan_out_no_bar_outside_edges(self):
        """No bar characters should appear before first center or after last center."""
        result = _render_fan_out([10, 30], spacing=2)
        bar_line = result[1]

        # Before first center (0-4): should be spaces
        for i in range(5):
            assert bar_line[i] == " ", f"Position {i} should be blank before bar"
        # After last center (28+): should be spaces
        for i in range(28, len(bar_line)):
            assert bar_line[i] == " ", f"Position {i} should be blank after bar"


# ===========================================================================
# Integration: full DAG render with review-fix scenarios
# ===========================================================================


class TestFullDagWithReviewFixes:
    """End-to-end tests combining review fix scenarios."""

    def test_orphaned_agents_visible_in_full_dag(self):
        """Full render_pipeline_dag shows orphaned agents."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2"]],
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
                            plan_phase_id="p2",
                        ),
                        # Orphaned agent
                        AgentExecution(
                            role=AgentRole.DOCUMENTER,
                            status=AgentExecutionStatus.COMPLETE,
                            plan_phase_id="removed-phase",
                        ),
                        # Top-level agent
                        AgentExecution(
                            role=AgentRole.INTEGRATOR,
                            status=AgentExecutionStatus.PENDING,
                        ),
                    ],
                ),
            },
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # All four agents must appear
        assert "coder" in result
        assert "tester" in result
        assert "documenter" in result
        assert "integrator" in result
        # Orphaned + top-level go to Pipeline agents
        assert "Pipeline agents" in result

    def test_asymmetric_subphases_have_aligned_connectors(self):
        """Sub-phases with very different name lengths produce aligned connectors."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2"]],
            plan_phase_names={
                "p1": "A",  # Very short name → narrow box
                "p2": "A Very Long Phase Name",  # Long name → wide box
            },
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # Both phases should render
        assert "A Very Long Phase Name" in result
        # Fan-out and fan-in connectors should appear
        lines = result.split("\n")
        impl_start = next(i for i, line in enumerate(lines) if "Tier 3" in line)
        impl_section = lines[impl_start:]

        # Find fan-out bar line (has ┌ and ┐)
        bar_lines = [ln for ln in impl_section if "┌" in ln and "┐" in ln]
        assert len(bar_lines) >= 1, "Fan-out bar not found"

        # Find fan-out stem line (line before bar) — should have │
        stem_lines = [ln for ln in impl_section if ln.strip() == "│"]
        assert len(stem_lines) >= 1, "Stem line not found"

    def test_three_parallel_phases_cross_junction_in_dag(self):
        """Three parallel sub-phases produce cross junction in full DAG."""
        pipeline = _make_pipeline(
            plan_phase_waves=[["p1", "p2", "p3"]],
            plan_phase_names={"p1": "Auth", "p2": "API", "p3": "UI"},
        )
        result = render_pipeline_dag(pipeline, include_header=False)

        # Cross junction should appear somewhere in the implement section
        assert "┼" in result
