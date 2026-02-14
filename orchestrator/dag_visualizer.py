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


def _format_duration(started_at: datetime | None, ended_at: datetime | None = None) -> str:
    """Format duration between two timestamps."""
    if not started_at:
        return ""

    end = ended_at or datetime.utcnow()
    delta = end - started_at
    total_seconds = int(delta.total_seconds())

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

    # Append any agents not in the dependency graph (e.g. generic REVIEWER)
    remaining = [a for a in agents if a.role.value not in assigned]
    if remaining:
        wave_groups.append(remaining)

    return wave_groups if wave_groups else [agents]


def _render_phase_box(
    phase: PipelinePhase,
    status: PipelineStatus,
    review_cycles: int,
    is_current: bool,
    agents: list[AgentExecution] | None = None,
    duration: str = "",
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
        status_text += f" (cycle {review_cycles})"

    # Current phase indicator
    current_marker = ">>>" if is_current else "   "

    # Build optional agent info lines, grouped by execution wave
    agent_lines: list[str] = []
    if agents:
        wave_groups = _compute_wave_order(phase, agents)

        for wave in wave_groups:
            entries = []
            for agent in wave:
                agent_symbol = _get_agent_status_symbol(agent.status, use_ascii)
                entries.append(f"{agent_symbol} {agent.role.value}")

            # Wrap within wave if 4+ agents (3 per line)
            if len(entries) <= 3:
                agent_lines.append("   " + "  ".join(entries))
            else:
                per_line = 3
                for i in range(0, len(entries), per_line):
                    chunk = entries[i : i + per_line]
                    agent_lines.append("   " + "  ".join(chunk))

    # Build duration line
    dur_line = f"   [{duration}]" if duration else ""

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
            duration = _format_duration(phase_exec.started_at, phase_exec.completed_at)
        else:
            status = PipelineStatus.PENDING
            review_cycles = 0
            agents = []
            duration = ""

        is_current = pipeline.current_phase == phase

        # Render phase box
        box_lines = _render_phase_box(
            phase=phase,
            status=status,
            review_cycles=review_cycles,
            is_current=is_current,
            agents=agents,
            duration=duration,
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

    if phase_exec.started_at:
        lines.append(f"Started: {phase_exec.started_at.isoformat()}")
    if phase_exec.completed_at:
        lines.append(f"Completed: {phase_exec.completed_at.isoformat()}")
        duration = _format_duration(phase_exec.started_at, phase_exec.completed_at)
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

    # Agent details, grouped by wave
    if phase_exec.agents:
        lines.append("")
        lines.append(f"Agents ({len(phase_exec.agents)}):")
        wave_groups = _compute_wave_order(phase, phase_exec.agents)
        for wave_idx, wave in enumerate(wave_groups):
            if wave_idx > 0:
                lines.append("")  # Blank line between waves
            for agent in wave:
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
