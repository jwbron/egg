"""Top-level parse orchestration for the plan parser.

Drives the three-tier parsing strategy (YAML code fence -> YAML front
matter -> markdown regex) and exposes the public ``parse_plan`` /
``parse_plan_file`` entry points plus ``format_warnings_for_comment``.
Extracted verbatim from the pre-split ``plan_parser.py`` (#3312 slice-7);
every function is AST-identical and re-exports through the package barrel,
so ``patch("egg_contracts.plan_parser.parse_plan")`` keeps resolving.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._markdown_parse import parse_phases_from_markdown, parse_tasks_from_markdown
from ._models import (
    PLACEHOLDER_ACCEPTANCE_CRITERIA,
    ParsedPhase,
    ParsedTask,
    ParseResult,
    ParseWarning,
)
from ._yaml_parse import (
    extract_pr_metadata_from_yaml,
    parse_phases_from_yaml,
    parse_tasks_from_yaml,
    parse_yaml_code_fence,
    parse_yaml_frontmatter,
)


def parse_plan(content: str) -> ParseResult:
    """
    Parse a plan document and extract tasks and phases.

    Parsing priority (Option C two-pass approach):
    1. YAML code fence with `# yaml-tasks` marker (preferred, structured)
    2. YAML front matter with `tasks:` key (legacy)
    3. Markdown regex extraction (fallback, fragile)

    Args:
        content: The plan document content (markdown with optional structured YAML)

    Returns:
        ParseResult with extracted phases, tasks, and any warnings
    """
    if not content or not content.strip():
        return ParseResult(
            success=False,
            error="Plan document is empty",
        )

    warnings: list[ParseWarning] = []
    phases: list[ParsedPhase] = []
    yaml_data: dict[str, Any] | None = None

    # === Priority 1: YAML code fence with yaml-tasks marker ===
    fence_yaml, remaining_content, fence_warnings = parse_yaml_code_fence(content)
    warnings.extend(fence_warnings)

    if fence_yaml is not None:
        yaml_data = fence_yaml
        # Try structured phases format first
        fence_phases, phase_warnings = parse_phases_from_yaml(fence_yaml)
        warnings.extend(phase_warnings)

        if fence_phases:
            phases = fence_phases
            # Still parse markdown phases for any additional metadata
            md_phases = parse_phases_from_markdown(remaining_content)
            # Merge goal/dependencies from markdown if missing in YAML
            for md_phase in md_phases:
                for phase in phases:
                    if phase.number == md_phase.number:
                        if not phase.goal and md_phase.goal:
                            phase.goal = md_phase.goal
                        break

    # === Priority 2: YAML front matter (legacy) ===
    if not phases:
        frontmatter_yaml, markdown_content = parse_yaml_frontmatter(content)
        if frontmatter_yaml and "tasks" in frontmatter_yaml:
            yaml_data = frontmatter_yaml
            tasks, yaml_warnings = parse_tasks_from_yaml(frontmatter_yaml)
            warnings.extend(yaml_warnings)

            # Parse phases from markdown
            phases = parse_phases_from_markdown(markdown_content)

            # Assign tasks to phases
            for task in tasks:
                for phase in phases:
                    if phase.number == task.phase_number:
                        phase.tasks.append(task)
                        break
                else:
                    # Create phase for orphan task
                    matching = [p for p in phases if p.number == task.phase_number]
                    if not matching:
                        phases.append(
                            ParsedPhase(
                                number=task.phase_number,
                                name=f"Phase {task.phase_number}",
                                goal="",
                                tasks=[task],
                            )
                        )
        else:
            markdown_content = content

    # === Priority 3: Markdown regex extraction (fallback) ===
    if not phases:
        tasks, md_warnings = parse_tasks_from_markdown(markdown_content)
        warnings.extend(md_warnings)

        # Parse phases from markdown headers
        phases = parse_phases_from_markdown(markdown_content)

        # Assign tasks to phases
        for task in tasks:
            assigned = False
            for phase in phases:
                if phase.number == task.phase_number:
                    phase.tasks.append(task)
                    assigned = True
                    break
            if not assigned:
                # Create phase for orphan task
                matching = [p for p in phases if p.number == task.phase_number]
                if not matching:
                    phases.append(
                        ParsedPhase(
                            number=task.phase_number,
                            name=f"Phase {task.phase_number}",
                            goal="",
                            tasks=[task],
                        )
                    )

    # Sort phases by number
    phases.sort(key=lambda p: p.number)

    # Check for phases without tasks and add placeholders
    for phase in phases:
        if not phase.tasks:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=f"Phase {phase.number} '{phase.name}' contains no parseable tasks",
                    context="A placeholder task will be created",
                )
            )
            phase.tasks.append(
                ParsedTask(
                    id=f"TASK-{phase.number}-1",
                    phase_number=phase.number,
                    task_number=1,
                    description=f"Review phase '{phase.name}' manually",
                    acceptance_criteria=PLACEHOLDER_ACCEPTANCE_CRITERIA,
                )
            )

    # Warn if no tasks at all were found
    if not phases:
        return ParseResult(
            success=False,
            error="No tasks or phases found in plan document. "
            "Use a yaml-tasks code fence or format tasks as: "
            "[TASK-{phase}-{number}] description — Acceptance: criteria",
            warnings=warnings,
        )

    # Extract PR metadata from YAML data
    pr_title, pr_description, pr_test_plan, pr_manual_steps, pr_warnings = (
        extract_pr_metadata_from_yaml(yaml_data)
    )
    warnings.extend(pr_warnings)

    return ParseResult(
        success=True,
        phases=phases,
        warnings=warnings,
        raw_yaml=yaml_data,
        pr_title=pr_title,
        pr_description=pr_description,
        pr_test_plan=pr_test_plan,
        pr_manual_steps=pr_manual_steps,
    )


def parse_plan_file(path: Path) -> ParseResult:
    """
    Parse a plan document from a file.

    Args:
        path: Path to the plan document

    Returns:
        ParseResult with extracted phases, tasks, and any warnings
    """
    if not path.exists():
        return ParseResult(
            success=False,
            error=f"Plan file not found: {path}",
        )

    try:
        content = path.read_text(encoding="utf-8")
        return parse_plan(content)
    except Exception as e:
        return ParseResult(
            success=False,
            error=f"Failed to read plan file: {e}",
        )


def format_warnings_for_comment(warnings: list[ParseWarning]) -> str:
    """
    Format warnings for display in a GitHub comment.

    Args:
        warnings: List of parse warnings

    Returns:
        Formatted markdown string
    """
    if not warnings:
        return ""

    lines = ["### Parse Warnings", ""]
    for warning in warnings:
        loc = f"Line {warning.line_number}: " if warning.line_number else ""
        lines.append(f"- {loc}{warning.message}")
        if warning.context:
            lines.append(f"  - {warning.context}")

    return "\n".join(lines)
