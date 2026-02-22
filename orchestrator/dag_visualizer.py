"""
ASCII DAG visualization for pipeline execution status.

Generates visual representations of the SDLC pipeline DAG showing
phases, their status, review cycles, and agent execution state.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from models import (
    AgentExecution,
    AgentExecutionStatus,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# Status symbols for phase visualization
STATUS_SYMBOLS = {
    PipelineStatus.PENDING: "○",  # Empty circle - not started
    PipelineStatus.RUNNING: "▶",  # Play symbol - running
    PipelineStatus.AWAITING_HUMAN: "⏸",  # Pause - waiting for human
    PipelineStatus.COMPLETE: "✓",  # Checkmark - done
    PipelineStatus.FAILED: "✗",  # X - failed
    PipelineStatus.CANCELLED: "⊘",  # Circle with slash - cancelled
}

# Alternative ASCII-only symbols (for terminals without Unicode)
ASCII_STATUS_SYMBOLS = {
    PipelineStatus.PENDING: "o",
    PipelineStatus.RUNNING: ">",
    PipelineStatus.AWAITING_HUMAN: "||",
    PipelineStatus.COMPLETE: "+",
    PipelineStatus.FAILED: "x",
    PipelineStatus.CANCELLED: "-",
}

# Phase order for linear DAG
PHASE_ORDER = [
    PipelinePhase.REFINE,
    PipelinePhase.PLAN,
    PipelinePhase.IMPLEMENT,
    PipelinePhase.PR,
]

# Phase display names
PHASE_NAMES = {
    PipelinePhase.REFINE: "Refine",
    PipelinePhase.PLAN: "Plan",
    PipelinePhase.IMPLEMENT: "Implement",
    PipelinePhase.PR: "PR",
}


def _get_status_symbol(status: PipelineStatus, use_ascii: bool = False) -> str:
    """Get display symbol for a status."""
    symbols = ASCII_STATUS_SYMBOLS if use_ascii else STATUS_SYMBOLS
    return symbols.get(status, "?")


# Mapping from AgentExecutionStatus to PipelineStatus for symbol lookup
_AGENT_STATUS_TO_PIPELINE_STATUS = {
    AgentExecutionStatus.COMPLETE: PipelineStatus.COMPLETE,
    AgentExecutionStatus.RUNNING: PipelineStatus.RUNNING,
    AgentExecutionStatus.FAILED: PipelineStatus.FAILED,
    AgentExecutionStatus.PENDING: PipelineStatus.PENDING,
}


def _get_agent_status_symbol(status: AgentExecutionStatus, use_ascii: bool = False) -> str:
    """Get display symbol for an agent execution status."""
    pipeline_status = _AGENT_STATUS_TO_PIPELINE_STATUS.get(status, PipelineStatus.PENDING)
    return _get_status_symbol(pipeline_status, use_ascii)


def _format_seconds(total_seconds: int) -> str:
    """Format a number of seconds as a human-readable duration string."""
    if total_seconds < 60:
        return f"{total_seconds}s"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}m{seconds}s"
    else:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours}h{minutes}m"


def _format_duration(started_at: datetime | None, ended_at: datetime | None = None) -> str:
    """Format duration between two timestamps."""
    if not started_at:
        return ""

    end = ended_at or datetime.utcnow()
    total_seconds = int((end - started_at).total_seconds())
    return _format_seconds(total_seconds)


def _total_work_seconds(phase_exec: PhaseExecution) -> int:
    """Sum of all completed cycle durations in seconds."""
    total = 0
    for ct in phase_exec.cycle_timings:
        end = ct.completed_at or datetime.utcnow()
        total += int((end - ct.started_at).total_seconds())
    return total


def _deduplicate_agents(
    agents: list[AgentExecution],
) -> tuple[list[AgentExecution], dict[str, int]]:
    """Collapse multiple runs of the same role into a single entry.

    When an agent role runs multiple times (e.g., checker retries,
    coder re-runs across review cycles), keep only the latest
    execution per role and track the run count.

    Returns:
        Tuple of (deduplicated agent list in first-seen order,
                  dict mapping role value to run count).
    """
    seen: dict[str, AgentExecution] = {}
    counts: dict[str, int] = {}
    order: list[str] = []

    for agent in agents:
        role_val = agent.role.value
        if role_val not in seen:
            order.append(role_val)
            seen[role_val] = agent
            counts[role_val] = 1
        else:
            seen[role_val] = agent
            counts[role_val] += 1

    deduped = [seen[rv] for rv in order]
    return deduped, counts


def _compute_wave_order(
    phase: PipelinePhase,
    agents: list[AgentExecution],
) -> list[list[AgentExecution]]:
    """Group agents by execution wave for display.

    Uses the dependency graph to determine which agents run in parallel
    (same wave) and which run sequentially (different waves).

    Falls back to a single group (flat list) for phases without
    defined agent roles (e.g. refine, pr).
    """
    try:
        from egg_contracts.agent_roles import get_roles_for_phase
        from egg_contracts.dependency_graph import build_dependency_graph
    except ImportError:
        return [agents]

    try:
        roles = get_roles_for_phase(phase.value, include_reviewers=True)
    except ValueError:
        return [agents]

    graph = build_dependency_graph(roles)
    waves = graph.compute_waves()

    # Build role value -> agent lookup
    agent_by_role_value: dict[str, AgentExecution] = {}
    for agent in agents:
        agent_by_role_value[agent.role.value] = agent

    # Map agents to their wave groups
    wave_groups: list[list[AgentExecution]] = []
    assigned: set[str] = set()

    for wave_roles in waves:
        group = []
        for role in wave_roles:
            if role.value in agent_by_role_value:
                group.append(agent_by_role_value[role.value])
                assigned.add(role.value)
        if group:
            wave_groups.append(group)

    # Place agents not in the dependency graph (e.g. CHECKER) in the
    # correct visual position.  Non-reviewer agents are inserted before
    # the first reviewer wave so that the display matches execution order
    # (workers → checker → reviewers).  Reviewer-type agents that aren't
    # in the graph are appended at the end.
    remaining = [a for a in agents if a.role.value not in assigned]
    if remaining:
        non_reviewer = [a for a in remaining if not a.role.value.startswith("reviewer")]
        reviewer_rem = [a for a in remaining if a.role.value.startswith("reviewer")]

        if non_reviewer:
            # Find the first wave that is entirely reviewer agents
            reviewer_start = None
            for idx, group in enumerate(wave_groups):
                if all(a.role.value.startswith("reviewer") for a in group):
                    reviewer_start = idx
                    break
            if reviewer_start is not None:
                wave_groups.insert(reviewer_start, non_reviewer)
            else:
                wave_groups.append(non_reviewer)

        if reviewer_rem:
            wave_groups.append(reviewer_rem)

    return wave_groups if wave_groups else [agents]


def _render_phase_box(
    phase: PipelinePhase,
    status: PipelineStatus,
    review_cycles: int,
    is_current: bool,
    agents: list[AgentExecution] | None = None,
    duration: str = "",
    total_duration: str = "",
    use_ascii: bool = False,
) -> list[str]:
    """Render a single phase box.

    Returns a list of lines for the box.
    """
    symbol = _get_status_symbol(status, use_ascii)
    name = PHASE_NAMES.get(phase, phase.value)

    # Build status line
    if status == PipelineStatus.AWAITING_HUMAN:
        status_text = "awaiting approval"
    else:
        status_text = status.value
    if review_cycles > 0:
        cycle_word = "cycle" if review_cycles == 1 else "cycles"
        status_text += f" ({review_cycles} {cycle_word} completed)"

    # Current phase indicator
    current_marker = ">>>" if is_current else "   "

    # Build optional agent info lines, grouped by execution wave.
    # Agents that ran multiple times are collapsed into a single entry
    # with a run count (e.g. "✓ checker ×2").
    # For Tier 3, agents with a plan_phase_id are grouped by sub-phase
    # first, then by wave within each sub-phase.  Agents without a
    # plan_phase_id (e.g. reviewer_contract, integrator) are rendered
    # after all sub-phase groups.
    agent_lines: list[str] = []
    if agents:
        mult = "x" if use_ascii else "\u00d7"

        def _render_wave_group(group_agents: list) -> None:
            deduped, run_counts = _deduplicate_agents(group_agents)
            wave_groups = _compute_wave_order(phase, deduped)
            for wave in wave_groups:
                entries = []
                for agent in wave:
                    agent_symbol = _get_agent_status_symbol(agent.status, use_ascii)
                    count = run_counts.get(agent.role.value, 1)
                    if count > 1:
                        entries.append(f"{agent_symbol} {agent.role.value} {mult}{count}")
                    else:
                        entries.append(f"{agent_symbol} {agent.role.value}")
                # Wrap within wave if 4+ agents (3 per line)
                if len(entries) <= 3:
                    agent_lines.append("   " + "  ".join(entries))
                else:
                    per_line = 3
                    for i in range(0, len(entries), per_line):
                        chunk = entries[i : i + per_line]
                        agent_lines.append("   " + "  ".join(chunk))

        # Partition into sub-phase buckets (preserving insertion order)
        sub_phase_buckets: dict[str, list] = {}
        top_level_agents: list = []
        for agent in agents:
            pid = getattr(agent, "plan_phase_id", None)
            if pid:
                sub_phase_buckets.setdefault(pid, []).append(agent)
            else:
                top_level_agents.append(agent)

        if sub_phase_buckets:
            for pid, bucket in sub_phase_buckets.items():
                agent_lines.append(f"   {pid}:")
                _render_wave_group(bucket)
            if top_level_agents:
                _render_wave_group(top_level_agents)
        else:
            _render_wave_group(agents)

    # Build duration line
    if duration and total_duration and total_duration != duration:
        dur_line = f"   [last cycle: {duration} | total: {total_duration}]"
    elif duration:
        dur_line = f"   [{duration}]"
    else:
        dur_line = ""

    # Width calculation - consider ALL potential content lines upfront
    # Name line has format " {symbol} {name}", so length is 3 + len(name)
    name_line_content = f" {symbol} {name}"
    status_line_content = f"   {status_text}"
    content_widths = [len(name_line_content), len(status_line_content), 12]
    for al in agent_lines:
        content_widths.append(len(al))
    if dur_line:
        content_widths.append(len(dur_line))
    content_width = max(content_widths)
    box_width = content_width + 2  # borders only (content is already padded)

    # Build box lines
    lines = []
    border_h = "=" if use_ascii else "═"
    border_v = "|" if use_ascii else "│"
    corner_tl = "+" if use_ascii else "╔"
    corner_tr = "+" if use_ascii else "╗"
    corner_bl = "+" if use_ascii else "╚"
    corner_br = "+" if use_ascii else "╝"

    # Top border
    lines.append(f"{current_marker} {corner_tl}{border_h * (box_width - 2)}{corner_tr}")

    # Phase name with symbol
    lines.append(f"    {border_v}{name_line_content:<{box_width - 2}}{border_v}")

    # Status line
    lines.append(f"    {border_v}{status_line_content:<{box_width - 2}}{border_v}")

    # Optional agent info lines
    for al in agent_lines:
        lines.append(f"    {border_v}{al:<{box_width - 2}}{border_v}")

    # Duration if available
    if dur_line:
        lines.append(f"    {border_v}{dur_line:<{box_width - 2}}{border_v}")

    # Bottom border
    lines.append(f"    {corner_bl}{border_h * (box_width - 2)}{corner_br}")

    return lines


def _render_arrow(use_ascii: bool = False) -> list[str]:
    """Render a vertical arrow between phases."""
    if use_ascii:
        return ["        |", "        |", "        v"]
    else:
        return ["        │", "        │", "        ▼"]


# --- Tier 3 sub-phase rendering ---


def _derive_subphase_status(
    agents: list[AgentExecution],
) -> PipelineStatus:
    """Derive aggregate status for a sub-phase from its agents.

    Priority: FAILED > RUNNING > PENDING > COMPLETE.
    Empty agent list returns PENDING.
    """
    if not agents:
        return PipelineStatus.PENDING

    statuses = {a.status for a in agents}
    if AgentExecutionStatus.FAILED in statuses:
        return PipelineStatus.FAILED
    if AgentExecutionStatus.RUNNING in statuses:
        return PipelineStatus.RUNNING
    if AgentExecutionStatus.PENDING in statuses:
        return PipelineStatus.PENDING
    return PipelineStatus.COMPLETE


def _render_subphase_box(
    phase_id: str,
    phase_name: str | None,
    agents: list[AgentExecution],
    use_ascii: bool = False,
    min_width: int = 20,
) -> list[str]:
    """Render a compact box for a single Tier 3 sub-phase.

    Shows the phase name, aggregate status, and agent sequence
    with status symbols.

    Args:
        phase_id: Plan phase identifier (e.g. 'phase-1')
        phase_name: Human-readable name (fallback to phase_id)
        agents: Agent executions belonging to this sub-phase
        use_ascii: Use ASCII-only characters
        min_width: Minimum box content width

    Returns:
        List of lines for the sub-phase box.
    """
    display_name = phase_name or phase_id
    status = _derive_subphase_status(agents)
    symbol = _get_status_symbol(status, use_ascii)

    # Build agent entries
    mult = "x" if use_ascii else "\u00d7"
    agent_entries: list[str] = []
    if agents:
        deduped, run_counts = _deduplicate_agents(agents)
        for agent in deduped:
            agent_symbol = _get_agent_status_symbol(agent.status, use_ascii)
            count = run_counts.get(agent.role.value, 1)
            if count > 1:
                agent_entries.append(f" {agent_symbol} {agent.role.value} {mult}{count}")
            else:
                agent_entries.append(f" {agent_symbol} {agent.role.value}")

    # Compute content width
    name_line = f" {symbol} {display_name}"
    content_widths = [len(name_line), min_width]
    for entry in agent_entries:
        content_widths.append(len(entry))
    content_width = max(content_widths)
    box_width = content_width + 2  # borders

    # Build box
    border_h = "-" if use_ascii else "─"
    border_v = "|" if use_ascii else "│"
    corner_tl = "+" if use_ascii else "┌"
    corner_tr = "+" if use_ascii else "┐"
    corner_bl = "+" if use_ascii else "└"
    corner_br = "+" if use_ascii else "┘"

    lines = []
    lines.append(f"{corner_tl}{border_h * (box_width - 2)}{corner_tr}")
    lines.append(f"{border_v}{name_line:<{box_width - 2}}{border_v}")
    for entry in agent_entries:
        lines.append(f"{border_v}{entry:<{box_width - 2}}{border_v}")
    lines.append(f"{corner_bl}{border_h * (box_width - 2)}{corner_br}")

    return lines


def _render_side_by_side(
    boxes: list[list[str]],
    spacing: int = 2,
) -> list[str]:
    """Concatenate multiple box line-lists horizontally.

    Normalizes heights by padding shorter boxes with blank lines
    and concatenates them with horizontal spacing.

    Args:
        boxes: List of box line-lists to join side-by-side
        spacing: Number of spaces between boxes

    Returns:
        Combined lines with all boxes side-by-side.
    """
    if not boxes:
        return []
    if len(boxes) == 1:
        return list(boxes[0])

    # Normalize heights
    max_height = max(len(box) for box in boxes)
    widths = [max(len(line) for line in box) if box else 0 for box in boxes]

    normalized: list[list[str]] = []
    for box, width in zip(boxes, widths, strict=True):
        padded = [line.ljust(width) for line in box]
        while len(padded) < max_height:
            padded.append(" " * width)
        normalized.append(padded)

    # Concatenate line-by-line
    spacer = " " * spacing
    result = []
    for row_idx in range(max_height):
        parts = [normalized[box_idx][row_idx] for box_idx in range(len(normalized))]
        result.append(spacer.join(parts))

    return result


def _render_fan_out(
    box_widths: list[int],
    spacing: int = 2,
    use_ascii: bool = False,
) -> list[str]:
    """Render a fan-out connector from a single stem to multiple branches.

    Produces a visual like:
          │
       ┌──┴──┐
       │     │

    Args:
        box_widths: Widths of each box being fanned out to
        spacing: Spacing between boxes
        use_ascii: Use ASCII-only characters

    Returns:
        List of lines for the fan-out connector.
    """
    if len(box_widths) < 2:
        return []

    # Characters
    v_line = "|" if use_ascii else "│"
    h_line = "-" if use_ascii else "─"
    tee_down = "+" if use_ascii else "┴"
    corner_l = "+" if use_ascii else "┌"
    corner_r = "+" if use_ascii else "┐"
    tee_up = "+" if use_ascii else "┬"

    # Total width of all boxes + spacing
    total_width = sum(box_widths) + spacing * (len(box_widths) - 1)

    # Centers of each box within the total width
    centers = []
    offset = 0
    for w in box_widths:
        centers.append(offset + w // 2)
        offset += w + spacing

    # Line 1: single vertical stem centered
    center = total_width // 2
    line1 = " " * center + v_line

    # Line 2: horizontal bar with tee-down at center, corners at edges
    left_edge = centers[0]
    right_edge = centers[-1]
    bar = [" "] * total_width
    for i in range(left_edge, right_edge + 1):
        bar[i] = h_line
    bar[left_edge] = corner_l
    bar[right_edge] = corner_r
    # Place tee-down at center of bar
    bar_center = (left_edge + right_edge) // 2
    bar[bar_center] = tee_down
    # Place tee-up at intermediate branch points
    for c in centers[1:-1]:
        bar[c] = tee_up

    line2 = "".join(bar)

    # Line 3: vertical stems at each branch center
    stems = [" "] * total_width
    for c in centers:
        stems[c] = v_line
    line3 = "".join(stems)

    return [line1, line2, line3]


def _render_fan_in(
    box_widths: list[int],
    spacing: int = 2,
    use_ascii: bool = False,
) -> list[str]:
    """Render a fan-in connector from multiple branches to a single stem.

    Produces a visual like:
       │     │
       └──┬──┘
          │

    Args:
        box_widths: Widths of each box being fanned in from
        spacing: Spacing between boxes
        use_ascii: Use ASCII-only characters

    Returns:
        List of lines for the fan-in connector.
    """
    if len(box_widths) < 2:
        return []

    # Characters
    v_line = "|" if use_ascii else "│"
    h_line = "-" if use_ascii else "─"
    tee_up = "+" if use_ascii else "┬"
    corner_l = "+" if use_ascii else "└"
    corner_r = "+" if use_ascii else "┘"
    tee_down = "+" if use_ascii else "┴"

    # Total width of all boxes + spacing
    total_width = sum(box_widths) + spacing * (len(box_widths) - 1)

    # Centers of each box within the total width
    centers = []
    offset = 0
    for w in box_widths:
        centers.append(offset + w // 2)
        offset += w + spacing

    # Line 1: vertical stems at each branch center
    stems = [" "] * total_width
    for c in centers:
        stems[c] = v_line
    line1 = "".join(stems)

    # Line 2: horizontal bar with tee-up at center, corners at edges
    left_edge = centers[0]
    right_edge = centers[-1]
    bar = [" "] * total_width
    for i in range(left_edge, right_edge + 1):
        bar[i] = h_line
    bar[left_edge] = corner_l
    bar[right_edge] = corner_r
    # Place tee-up at center of bar
    bar_center = (left_edge + right_edge) // 2
    bar[bar_center] = tee_up
    # Place tee-down at intermediate branch points
    for c in centers[1:-1]:
        bar[c] = tee_down

    line2 = "".join(bar)

    # Line 3: single vertical stem centered
    center = total_width // 2
    line3 = " " * center + v_line

    return [line1, line2, line3]


def _render_tier3_implement(
    pipeline: Pipeline,
    phase_exec: PhaseExecution | None,
    is_current: bool,
    use_ascii: bool = False,
) -> list[str]:
    """Render the expanded Tier 3 Implement section with sub-phase boxes.

    Replaces the single Implement box with individual sub-phase boxes
    arranged by dependency wave, connected with fan-out/fan-in connectors.

    Top-level agents (those without plan_phase_id, e.g. integrator,
    reviewer_contract) are rendered in a separate box after all sub-phases.

    Args:
        pipeline: Pipeline with plan_phase_waves data
        phase_exec: Phase execution data for implement phase
        is_current: Whether implement is the current phase
        use_ascii: Use ASCII-only characters

    Returns:
        List of lines for the full expanded Implement section.
    """
    waves = pipeline.plan_phase_waves or []
    phase_names = pipeline.plan_phase_names or {}

    # Partition agents by plan_phase_id
    agents_by_phase: dict[str, list[AgentExecution]] = {}
    top_level_agents: list[AgentExecution] = []

    if phase_exec and phase_exec.agents:
        for agent in phase_exec.agents:
            pid = agent.plan_phase_id
            if pid:
                agents_by_phase.setdefault(pid, []).append(agent)
            else:
                top_level_agents.append(agent)

    lines: list[str] = []

    # Current phase indicator for the section header
    current_marker = ">>>" if is_current else "   "
    border_h = "=" if use_ascii else "═"
    lines.append(f"{current_marker} {border_h * 3} Implement (Tier 3) {border_h * 3}")

    spacing = 2
    max_side_by_side = 4

    for wave_idx, wave_phase_ids in enumerate(waves):
        if not wave_phase_ids:
            continue

        # Build sub-phase boxes for this wave
        wave_boxes: list[list[str]] = []
        for pid in wave_phase_ids:
            phase_agents = agents_by_phase.get(pid, [])
            box = _render_subphase_box(
                phase_id=pid,
                phase_name=phase_names.get(pid),
                agents=phase_agents,
                use_ascii=use_ascii,
            )
            wave_boxes.append(box)

        if len(wave_phase_ids) == 1:
            # Single phase in wave — render centered with indent
            for line in wave_boxes[0]:
                lines.append(f"    {line}")
        else:
            # Multiple phases — render side-by-side with connectors
            # Handle wrapping if too many boxes
            for chunk_start in range(0, len(wave_boxes), max_side_by_side):
                chunk = wave_boxes[chunk_start : chunk_start + max_side_by_side]
                chunk_widths = [
                    max(len(line) for line in box) if box else 0 for box in chunk
                ]

                if chunk_start > 0:
                    # Arrow between wrapped rows
                    v_line = "|" if use_ascii else "│"
                    lines.append(f"    {v_line}")

                # Fan-out connector
                fan_out = _render_fan_out(chunk_widths, spacing, use_ascii)
                for line in fan_out:
                    lines.append(f"    {line}")

                # Side-by-side boxes
                side_by_side = _render_side_by_side(chunk, spacing)
                for line in side_by_side:
                    lines.append(f"    {line}")

                # Fan-in connector
                fan_in = _render_fan_in(chunk_widths, spacing, use_ascii)
                for line in fan_in:
                    lines.append(f"    {line}")

        # Arrow between waves (except after last wave)
        if wave_idx < len(waves) - 1:
            v_line = "|" if use_ascii else "│"
            arrow = "v" if use_ascii else "▼"
            lines.append(f"        {v_line}")
            lines.append(f"        {arrow}")

    # Render top-level agents (integrator, reviewer_contract, etc.) in a separate box
    if top_level_agents:
        v_line = "|" if use_ascii else "│"
        arrow = "v" if use_ascii else "▼"
        lines.append(f"        {v_line}")
        lines.append(f"        {arrow}")

        # Build a compact box for top-level agents
        status = _derive_subphase_status(top_level_agents)
        symbol = _get_status_symbol(status, use_ascii)
        mult = "x" if use_ascii else "\u00d7"

        deduped, run_counts = _deduplicate_agents(top_level_agents)
        agent_entries: list[str] = []
        for agent in deduped:
            agent_symbol = _get_agent_status_symbol(agent.status, use_ascii)
            count = run_counts.get(agent.role.value, 1)
            if count > 1:
                agent_entries.append(f" {agent_symbol} {agent.role.value} {mult}{count}")
            else:
                agent_entries.append(f" {agent_symbol} {agent.role.value}")

        name_line = f" {symbol} Pipeline agents"
        content_widths = [len(name_line), 20]
        for entry in agent_entries:
            content_widths.append(len(entry))
        content_width = max(content_widths)
        box_width = content_width + 2

        border_h_box = "-" if use_ascii else "─"
        border_v = "|" if use_ascii else "│"
        corner_tl = "+" if use_ascii else "┌"
        corner_tr = "+" if use_ascii else "┐"
        corner_bl = "+" if use_ascii else "└"
        corner_br = "+" if use_ascii else "┘"

        lines.append(f"    {corner_tl}{border_h_box * (box_width - 2)}{corner_tr}")
        lines.append(f"    {border_v}{name_line:<{box_width - 2}}{border_v}")
        for entry in agent_entries:
            lines.append(f"    {border_v}{entry:<{box_width - 2}}{border_v}")
        lines.append(f"    {corner_bl}{border_h_box * (box_width - 2)}{corner_br}")

    return lines


def render_pipeline_dag(
    pipeline: Pipeline,
    use_ascii: bool = False,
    include_header: bool = True,
) -> str:
    """Render full pipeline DAG visualization.

    Args:
        pipeline: Pipeline to visualize
        use_ascii: Use ASCII-only characters (for limited terminals)
        include_header: Include header with pipeline info

    Returns:
        Multi-line string visualization
    """
    lines = []

    # Header
    if include_header:
        lines.append(f"Pipeline: {pipeline.id}")
        header_status = pipeline.status.value
        if pipeline.status == PipelineStatus.AWAITING_HUMAN:
            header_status = "awaiting approval"
        lines.append(f"Status: {header_status}")
        if pipeline.repo:
            lines.append(f"Repository: {pipeline.repo}")
        if pipeline.branch:
            lines.append(f"Branch: {pipeline.branch}")
        lines.append("")
        lines.append("DAG Visualization:")
        lines.append("")

    # Render each phase
    for i, phase in enumerate(PHASE_ORDER):
        # Get phase execution data
        phase_exec = pipeline.phases.get(phase.value)

        if phase_exec:
            status = phase_exec.status
            review_cycles = phase_exec.review_cycles
            agents = phase_exec.agents
            work_start = phase_exec.work_started_at or phase_exec.started_at
            duration = _format_duration(work_start, phase_exec.completed_at)
            # Compute total duration from cycle timings when multi-cycle
            if phase_exec.cycle_timings and phase_exec.review_cycles > 0:
                total_duration = _format_seconds(_total_work_seconds(phase_exec))
            else:
                total_duration = ""
        else:
            status = PipelineStatus.PENDING
            review_cycles = 0
            agents = []
            duration = ""
            total_duration = ""

        is_current = pipeline.current_phase == phase

        # Use Tier 3 expanded rendering for Implement phase when wave data exists
        if phase == PipelinePhase.IMPLEMENT and pipeline.plan_phase_waves:
            box_lines = _render_tier3_implement(
                pipeline=pipeline,
                phase_exec=phase_exec,
                is_current=is_current,
                use_ascii=use_ascii,
            )
        else:
            # Render standard phase box
            box_lines = _render_phase_box(
                phase=phase,
                status=status,
                review_cycles=review_cycles,
                is_current=is_current,
                agents=agents,
                duration=duration,
                total_duration=total_duration,
                use_ascii=use_ascii,
            )
        lines.extend(box_lines)

        # Arrow between phases (except after last)
        if i < len(PHASE_ORDER) - 1:
            lines.extend(_render_arrow(use_ascii))

    return "\n".join(lines)


def render_phase_detail(
    pipeline: Pipeline,
    phase: PipelinePhase,
    use_ascii: bool = False,
) -> str:
    """Render detailed view of a single phase.

    Args:
        pipeline: Pipeline containing phase
        phase: Phase to render details for
        use_ascii: Use ASCII-only characters

    Returns:
        Multi-line string with phase details
    """
    lines = []
    phase_exec = pipeline.phases.get(phase.value)

    name = PHASE_NAMES.get(phase, phase.value)
    lines.append(f"Phase: {name}")
    lines.append("=" * 40)

    if not phase_exec:
        lines.append("Status: Not started")
        return "\n".join(lines)

    symbol = _get_status_symbol(phase_exec.status, use_ascii)
    if phase_exec.status == PipelineStatus.AWAITING_HUMAN:
        status_text = "awaiting approval"
    else:
        status_text = phase_exec.status.value
    lines.append(f"Status: {symbol} {status_text}")
    lines.append(f"Review Cycles: {phase_exec.review_cycles}")

    # Per-cycle timing breakdown
    if phase_exec.cycle_timings:
        lines.append("")
        lines.append("Cycle Timings:")
        for ct in phase_exec.cycle_timings:
            dur = _format_duration(ct.started_at, ct.completed_at)
            status = "done" if ct.completed_at else "running"
            lines.append(f"  Cycle {ct.cycle}: {dur} ({status})")
        if len(phase_exec.cycle_timings) > 1:
            total = _format_seconds(_total_work_seconds(phase_exec))
            lines.append(f"  Total work time: {total}")

    if phase_exec.started_at:
        lines.append(f"Started: {phase_exec.started_at.isoformat()}")
    if phase_exec.work_started_at:
        lines.append(f"Work started: {phase_exec.work_started_at.isoformat()}")
    if phase_exec.completed_at:
        lines.append(f"Completed: {phase_exec.completed_at.isoformat()}")
        work_start = phase_exec.work_started_at or phase_exec.started_at
        duration = _format_duration(work_start, phase_exec.completed_at)
        lines.append(f"Duration: {duration}")

    if phase_exec.error:
        lines.append(f"Error: {phase_exec.error}")

    # Container details
    if phase_exec.containers:
        lines.append("")
        lines.append(f"Containers ({len(phase_exec.containers)}):")
        for container in phase_exec.containers:
            c_status = _get_status_symbol(
                PipelineStatus.COMPLETE
                if container.status == ContainerStatus.EXITED
                else PipelineStatus.RUNNING
                if container.status == ContainerStatus.RUNNING
                else PipelineStatus.PENDING,
                use_ascii,
            )
            role = container.agent_role.value if container.agent_role else "worker"
            lines.append(f"  {c_status} {container.container_name[:20]} ({role})")

    # Agent details — show ALL runs (no deduplication, no wave grouping).
    # The detail view intentionally preserves every execution so that
    # commit SHAs and error messages from earlier runs are not lost.
    # Deduplication is only applied in the DAG overview (_render_phase_box)
    # where space is limited.
    #
    # We iterate phase_exec.agents directly rather than routing through
    # _compute_wave_order, because that function deduplicates in-graph
    # roles via a dict keyed by role value (only the last execution per
    # role survives).  The detail view must show every run.
    if phase_exec.agents:
        lines.append("")
        lines.append(f"Agents ({len(phase_exec.agents)}):")
        for agent in phase_exec.agents:
            a_status = _get_agent_status_symbol(agent.status, use_ascii)
            lines.append(f"  {a_status} {agent.role.value}")
            if agent.commit:
                lines.append(f"      Commit: {agent.commit[:8]}")
            if agent.error:
                lines.append(f"      Error: {agent.error[:50]}")

    return "\n".join(lines)


def render_compact_status(
    pipeline: Pipeline,
    use_ascii: bool = False,
) -> str:
    """Render a single-line compact status.

    Args:
        pipeline: Pipeline to visualize
        use_ascii: Use ASCII-only characters

    Returns:
        Single-line status string
    """
    parts = []

    for phase in PHASE_ORDER:
        phase_exec = pipeline.phases.get(phase.value)
        status = phase_exec.status if phase_exec else PipelineStatus.PENDING
        symbol = _get_status_symbol(status, use_ascii)

        # Current phase indicator
        if pipeline.current_phase == phase:
            parts.append(f"[{symbol}{PHASE_NAMES[phase]}]")
        else:
            parts.append(f"{symbol}{PHASE_NAMES[phase]}")

    arrow = "-->" if use_ascii else "→"
    return f" {arrow} ".join(parts)


def render_progress_bar(
    pipeline: Pipeline,
    width: int = 40,
    use_ascii: bool = False,
) -> str:
    """Render a progress bar for pipeline completion.

    Args:
        pipeline: Pipeline to visualize
        width: Width of progress bar
        use_ascii: Use ASCII-only characters

    Returns:
        Progress bar string
    """
    # Calculate completion percentage
    completed = 0
    total = len(PHASE_ORDER)

    for phase in PHASE_ORDER:
        phase_exec = pipeline.phases.get(phase.value)
        if phase_exec and phase_exec.status == PipelineStatus.COMPLETE:
            completed += 1
        elif pipeline.current_phase == phase:
            # Current phase counts as half
            completed += 0.5

    percentage = completed / total
    filled_width = int(width * percentage)

    fill_char = "#" if use_ascii else "█"
    empty_char = "-" if use_ascii else "░"

    bar = fill_char * filled_width + empty_char * (width - filled_width)
    pct_text = f"{int(percentage * 100)}%"

    return f"[{bar}] {pct_text}"


def generate_status_report(
    pipeline: Pipeline,
    use_ascii: bool = False,
) -> dict[str, Any]:
    """Generate a complete status report for API response.

    Args:
        pipeline: Pipeline to report on
        use_ascii: Use ASCII-only characters

    Returns:
        Dictionary with visualization data
    """
    return {
        "pipeline_id": pipeline.id,
        "status": pipeline.status.value,
        "current_phase": pipeline.current_phase.value,
        "visualization": {
            "dag": render_pipeline_dag(pipeline, use_ascii=use_ascii),
            "compact": render_compact_status(pipeline, use_ascii=use_ascii),
            "progress": render_progress_bar(pipeline, use_ascii=use_ascii),
        },
        "phases": {
            phase.value: {
                "status": (
                    pipeline.phases[phase.value].status.value
                    if phase.value in pipeline.phases
                    else PipelineStatus.PENDING.value
                ),
                "review_cycles": (
                    pipeline.phases[phase.value].review_cycles
                    if phase.value in pipeline.phases
                    else 0
                ),
                "containers": (
                    len(pipeline.phases[phase.value].containers)
                    if phase.value in pipeline.phases
                    else 0
                ),
                # Agents are intentionally NOT deduplicated here.
                # The API exposes raw execution data so clients can
                # see every run (with commit/error per run).  Visual
                # deduplication is only applied in _render_phase_box.
                "agents": (
                    [
                        {"role": a.role.value, "status": a.status.value}
                        for a in pipeline.phases[phase.value].agents
                    ]
                    if phase.value in pipeline.phases
                    else []
                ),
            }
            for phase in PHASE_ORDER
        },
        "pending_decisions": len(pipeline.get_pending_decisions()),
        "updated_at": pipeline.updated_at.isoformat() + "Z",
        "timestamp": pipeline.updated_at.isoformat() + "Z",
    }
