"""Markdown fallback extraction layer for the plan parser.

The fragile third-tier parser: pulls tasks and phase sections out of raw
markdown via the compiled patterns in
:mod:`~egg_contracts.plan_parser._models` when no structured YAML is
present. Extracted verbatim from the pre-split ``plan_parser.py`` (#3312
slice-7); both functions are AST-identical and re-export through the
package barrel.
"""

from __future__ import annotations

from ._models import (
    FILES_PATTERN,
    GOAL_PATTERN,
    PHASE_HEADER_PATTERN,
    TASK_PATTERN,
    ParsedPhase,
    ParsedTask,
    ParseWarning,
)


def parse_tasks_from_markdown(content: str) -> tuple[list[ParsedTask], list[ParseWarning]]:
    """
    Parse tasks from markdown content.

    Args:
        content: Markdown content (without YAML front matter)

    Returns:
        Tuple of (tasks, warnings)
    """
    tasks: list[ParsedTask] = []
    warnings: list[ParseWarning] = []

    for line in content.split("\n"):
        # Look for task patterns in list items
        if not line.strip().startswith("-"):
            continue

        match = TASK_PATTERN.search(line)
        if match:
            phase_num = int(match.group(1))
            task_num = int(match.group(2))
            description = match.group(3).strip()
            acceptance = match.group(4).strip()

            # Extract files from description if present
            files = []
            files_match = FILES_PATTERN.search(description)
            if files_match:
                files = [f.strip() for f in files_match.group(1).split(",")]

            tasks.append(
                ParsedTask(
                    id=f"TASK-{phase_num}-{task_num}",
                    phase_number=phase_num,
                    task_number=task_num,
                    description=description,
                    acceptance_criteria=acceptance,
                    files_affected=files,
                )
            )

    return tasks, warnings


def parse_phases_from_markdown(content: str) -> list[ParsedPhase]:
    """
    Parse phase sections from markdown content.

    Args:
        content: Markdown content

    Returns:
        List of ParsedPhase objects (without tasks filled in)
    """
    phases = []
    matches = list(PHASE_HEADER_PATTERN.finditer(content))

    for i, match in enumerate(matches):
        phase_num = int(match.group(1))
        phase_name = match.group(2).strip()

        # Extract section content until next phase or end
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section = content[start:end]

        # Extract goal
        goal = ""
        goal_match = GOAL_PATTERN.search(section)
        if goal_match:
            goal = goal_match.group(1).strip()

        phases.append(
            ParsedPhase(
                number=phase_num,
                name=phase_name,
                goal=goal,
            )
        )

    return phases
