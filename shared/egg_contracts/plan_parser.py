"""
Plan document parser for extracting tasks into contract format.

This module parses plan documents (markdown) and extracts structured task
information that can be written to the contract JSON.

Task ID Format:
    Tasks must be marked with explicit IDs: [TASK-{phase}-{number}]
    Example: [TASK-1-1] Create contract JSON schema — Acceptance: schema validates

Parse Failure Handling:
    - If a phase contains no parseable tasks, a placeholder task is created
    - If the plan document is missing or malformed, parsing fails with a structured error
    - Parse results include a warnings[] array for human review
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Phase, PhaseStatus, Task, TaskStatus


@dataclass
class ParsedTask:
    """A task extracted from a plan document."""

    id: str
    phase_number: int
    task_number: int
    description: str
    acceptance_criteria: str
    files_affected: list[str] = field(default_factory=list)

    def to_contract_task(self) -> Task:
        """Convert to a contract Task model."""
        return Task(
            id=f"task-{self.task_number}",
            description=self.description,
            status=TaskStatus.PENDING,
            acceptance_criteria=self.acceptance_criteria,
            files_affected=self.files_affected,
        )


@dataclass
class ParsedPhase:
    """A phase extracted from a plan document."""

    number: int
    name: str
    goal: str
    tasks: list[ParsedTask] = field(default_factory=list)
    dependencies: str = ""
    exit_criteria: str = ""

    def to_contract_phase(self) -> Phase:
        """Convert to a contract Phase model."""
        return Phase(
            id=f"phase-{self.number}",
            name=self.name,
            status=PhaseStatus.PENDING,
            tasks=[task.to_contract_task() for task in self.tasks],
        )


@dataclass
class ParseWarning:
    """A warning generated during parsing."""

    line_number: int | None
    message: str
    context: str = ""


@dataclass
class ParseResult:
    """Result of parsing a plan document."""

    success: bool
    phases: list[ParsedPhase] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    error: str | None = None
    raw_yaml: dict[str, Any] | None = None

    def to_contract_phases(self) -> list[Phase]:
        """Convert all parsed phases to contract Phase models."""
        return [phase.to_contract_phase() for phase in self.phases]


# Regex pattern for task IDs in markdown
# Matches: [TASK-{phase}-{number}] description — Acceptance: criteria
TASK_PATTERN = re.compile(
    r"\[TASK-(\d+)-(\d+)\]\s*(.+?)\s*(?:—|--|-)\s*Acceptance:\s*(.+)",
    re.IGNORECASE,
)

# Pattern for phase headers
# Matches: ### Phase N: Name or ## Phase N: Name
PHASE_HEADER_PATTERN = re.compile(
    r"^#{2,3}\s*Phase\s+(\d+):\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern for goal lines within a phase section
GOAL_PATTERN = re.compile(r"\*\*Goal\*\*:\s*(.+)", re.IGNORECASE)

# Pattern for files in brackets
FILES_PATTERN = re.compile(r"\[([^\]]+)\]")


def parse_yaml_frontmatter(content: str) -> tuple[dict[str, Any] | None, str]:
    """
    Extract YAML front matter from document if present.

    Args:
        content: The document content

    Returns:
        Tuple of (yaml_data, remaining_content)
    """
    if not content.startswith("---"):
        return None, content

    # Find the closing ---
    lines = content.split("\n")
    end_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = i
            break

    if end_index is None:
        return None, content

    yaml_block = "\n".join(lines[1:end_index])
    remaining = "\n".join(lines[end_index + 1 :])

    try:
        yaml_data = yaml.safe_load(yaml_block)
        return yaml_data, remaining
    except yaml.YAMLError:
        return None, content


def parse_tasks_from_yaml(yaml_data: dict[str, Any]) -> list[ParsedTask]:
    """
    Parse tasks from YAML front matter.

    Args:
        yaml_data: Parsed YAML data

    Returns:
        List of ParsedTask objects
    """
    tasks = []
    task_list = yaml_data.get("tasks", [])

    for task_data in task_list:
        task_id = task_data.get("id", "")
        # Parse task ID: TASK-{phase}-{number}
        match = re.match(r"TASK-(\d+)-(\d+)", task_id, re.IGNORECASE)
        if match:
            phase_num = int(match.group(1))
            task_num = int(match.group(2))
            tasks.append(
                ParsedTask(
                    id=task_id,
                    phase_number=phase_num,
                    task_number=task_num,
                    description=task_data.get("description", ""),
                    acceptance_criteria=task_data.get("acceptance", ""),
                    files_affected=task_data.get("files", []),
                )
            )

    return tasks


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


def parse_plan(content: str) -> ParseResult:
    """
    Parse a plan document and extract tasks and phases.

    Args:
        content: The plan document content (markdown with optional YAML front matter)

    Returns:
        ParseResult with extracted phases, tasks, and any warnings
    """
    if not content or not content.strip():
        return ParseResult(
            success=False,
            error="Plan document is empty",
        )

    warnings: list[ParseWarning] = []

    # Try YAML front matter first
    yaml_data, markdown_content = parse_yaml_frontmatter(content)

    tasks: list[ParsedTask] = []
    if yaml_data and "tasks" in yaml_data:
        # Parse from YAML
        tasks = parse_tasks_from_yaml(yaml_data)
    else:
        # Parse from markdown
        tasks, md_warnings = parse_tasks_from_markdown(markdown_content)
        warnings.extend(md_warnings)

    # Parse phases from markdown
    phases = parse_phases_from_markdown(markdown_content)

    # Assign tasks to phases
    for task in tasks:
        for phase in phases:
            if phase.number == task.phase_number:
                phase.tasks.append(task)
                break
        else:
            # Task references a phase that doesn't exist in headers
            # Create the phase if needed
            matching_phases = [p for p in phases if p.number == task.phase_number]
            if not matching_phases:
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
                    id=f"TASK-{phase.number}-0",
                    phase_number=phase.number,
                    task_number=0,
                    description=f"Review phase '{phase.name}' manually",
                    acceptance_criteria="Human verification",
                )
            )

    # Warn if no tasks at all were found
    if not tasks and not phases:
        return ParseResult(
            success=False,
            error="No tasks or phases found in plan document. "
            "Tasks must use format: [TASK-{phase}-{number}] description — Acceptance: criteria",
        )

    return ParseResult(
        success=True,
        phases=phases,
        warnings=warnings,
        raw_yaml=yaml_data,
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
