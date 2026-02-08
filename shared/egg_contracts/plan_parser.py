"""
Plan document parser for extracting tasks into contract format.

This module parses plan documents (markdown) and extracts structured task
information that can be written to the contract JSON.

Parsing Strategy (Option C - Two-Pass Approach):
    The parser supports three extraction modes, in order of preference:

    1. YAML Code Fence (preferred): A ```yaml block marked with `# yaml-tasks`
       header containing structured task data. This is machine-parseable and
       type-checked, while allowing humans to review the prose plan above it.

    2. YAML Front Matter (legacy): A ---delimited YAML block at the start of
       the document. Still supported for backwards compatibility.

    3. Markdown Regex (fallback): Extract tasks from markdown list items using
       the [TASK-{phase}-{number}] pattern. This is fragile and may miss tasks
       if the LLM's output format drifts.

Task ID Format:
    Tasks must use the format: TASK-{phase}-{number}
    Example: TASK-1-1, TASK-2-3

YAML Code Fence Format:
    The structured appendix should be a YAML code block with the marker comment:

    ```yaml
    # yaml-tasks
    phases:
      - id: 1
        name: Setup
        goal: Initialize the project
        tasks:
          - id: TASK-1-1
            description: Create contract JSON schema
            acceptance: Schema validates sample contracts
            files:
              - .egg/schemas/contract.schema.json
    ```

Parse Failure Handling:
    - If a phase contains no parseable tasks, a placeholder task is created
    - If the plan document is missing or malformed, parsing fails with a structured error
    - Parse results include a warnings[] array for human review
    - If YAML code fence parsing fails, falls back to markdown regex with a warning
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Phase, PhaseStatus, Task, TaskStatus

# Placeholder acceptance criteria for tasks that couldn't be parsed.
# Used as a sentinel value to filter out non-real criteria during aggregation.
PLACEHOLDER_ACCEPTANCE_CRITERIA = "Human verification"


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
            id=f"task-{self.phase_number}-{self.task_number}",
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
    pr_title: str | None = None
    pr_description: str | None = None

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

# Pattern for YAML code fence with yaml-tasks marker
# Matches: ```yaml\n# yaml-tasks\n...\n```
YAML_FENCE_PATTERN = re.compile(
    r"```(?:yaml|yml)\s*\n\s*#\s*yaml-tasks\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def parse_yaml_code_fence(content: str) -> tuple[dict[str, Any] | None, str, list[ParseWarning]]:
    """
    Extract YAML data from a code fence with the yaml-tasks marker.

    The code fence must be formatted as:
    ```yaml
    # yaml-tasks
    phases:
      - id: 1
        name: Phase Name
        ...
    ```

    Args:
        content: The document content

    Returns:
        Tuple of (yaml_data, remaining_content, warnings)
    """
    warnings: list[ParseWarning] = []
    match = YAML_FENCE_PATTERN.search(content)

    if not match:
        return None, content, warnings

    yaml_block = match.group(1)

    try:
        yaml_data = yaml.safe_load(yaml_block)
        if yaml_data is None:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message="yaml-tasks code fence is empty",
                    context="Falling back to markdown parsing",
                )
            )
            return None, content, warnings

        # Validate required structure
        if not isinstance(yaml_data, dict):
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message="yaml-tasks must contain a YAML mapping (dict)",
                    context="Falling back to markdown parsing",
                )
            )
            return None, content, warnings

        # Remove the YAML fence from content for markdown fallback parsing
        remaining = content[: match.start()] + content[match.end() :]
        return yaml_data, remaining.strip(), warnings

    except yaml.YAMLError as e:
        warnings.append(
            ParseWarning(
                line_number=None,
                message=f"Invalid YAML in yaml-tasks code fence: {e}",
                context="Falling back to markdown parsing",
            )
        )
        return None, content, warnings


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
    Parse tasks from YAML front matter (legacy flat task list format).

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

            # Normalize files field to list
            files = task_data.get("files", [])
            if isinstance(files, str):
                files = [files]
            elif not isinstance(files, list):
                files = []

            tasks.append(
                ParsedTask(
                    id=task_id,
                    phase_number=phase_num,
                    task_number=task_num,
                    description=task_data.get("description", ""),
                    acceptance_criteria=task_data.get("acceptance", ""),
                    files_affected=files,
                )
            )

    return tasks


def parse_phases_from_yaml(
    yaml_data: dict[str, Any],
) -> tuple[list[ParsedPhase], list[ParseWarning]]:
    """
    Parse phases and tasks from structured YAML (yaml-tasks code fence format).

    Expected format:
    ```yaml
    # yaml-tasks
    phases:
      - id: 1
        name: Setup
        goal: Initialize the project
        tasks:
          - id: TASK-1-1
            description: Create contract JSON schema
            acceptance: Schema validates sample contracts
            files:
              - schema.json
    ```

    Args:
        yaml_data: Parsed YAML data from code fence

    Returns:
        Tuple of (phases, warnings)
    """
    phases: list[ParsedPhase] = []
    warnings: list[ParseWarning] = []
    seen_phase_ids: set[int] = set()

    phase_list = yaml_data.get("phases", [])

    if not phase_list:
        # Check for legacy flat task list format
        if "tasks" in yaml_data:
            return [], warnings  # Let caller fall back to legacy parsing
        warnings.append(
            ParseWarning(
                line_number=None,
                message="yaml-tasks block has no 'phases' key",
                context="Expected format: phases: [...]",
            )
        )
        return phases, warnings

    for phase_data in phase_list:
        if not isinstance(phase_data, dict):
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=f"Invalid phase entry (expected dict, got {type(phase_data).__name__})",
                )
            )
            continue

        # Extract phase info
        phase_id = phase_data.get("id")
        if phase_id is None:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message="Phase missing 'id' field",
                    context=f"Phase data: {phase_data}",
                )
            )
            continue

        # Handle both numeric and string IDs
        try:
            phase_num = int(phase_id)
        except (ValueError, TypeError):
            # Try extracting number from string like "phase-1"
            id_match = re.search(r"(\d+)", str(phase_id))
            if id_match:
                phase_num = int(id_match.group(1))
            else:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Cannot parse phase ID: {phase_id}",
                    )
                )
                continue

        # Check for duplicate phase IDs
        if phase_num in seen_phase_ids:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=f"Duplicate phase ID: {phase_num}",
                    context="Skipping duplicate phase",
                )
            )
            continue
        seen_phase_ids.add(phase_num)

        phase_name = phase_data.get("name", f"Phase {phase_num}")
        phase_goal = phase_data.get("goal", "")
        phase_dependencies = phase_data.get("dependencies", "")
        phase_exit_criteria = phase_data.get("exit_criteria", "")

        # Parse tasks for this phase
        parsed_tasks: list[ParsedTask] = []
        task_list = phase_data.get("tasks", [])

        for task_data in task_list:
            if not isinstance(task_data, dict):
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Invalid task entry in phase {phase_num}",
                    )
                )
                continue

            task_id = task_data.get("id", "")
            description = task_data.get("description", "")
            acceptance = task_data.get("acceptance", "")
            files = task_data.get("files", [])

            # Ensure files is a list
            if isinstance(files, str):
                files = [files]
            elif not isinstance(files, list):
                files = []

            # Parse task ID: TASK-{phase}-{number}
            id_match = re.match(r"TASK-(\d+)-(\d+)", str(task_id), re.IGNORECASE)
            if id_match:
                task_phase = int(id_match.group(1))
                task_num = int(id_match.group(2))
            else:
                # Try to use sequence number if ID doesn't match pattern
                task_num = len(parsed_tasks) + 1
                task_phase = phase_num
                task_id = f"TASK-{phase_num}-{task_num}"
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Task ID '{task_data.get('id', 'missing')}' doesn't match pattern, "
                        f"assigned {task_id}",
                    )
                )

            # Validate task phase matches container phase
            if task_phase != phase_num:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Task {task_id} is in phase {phase_num} but ID suggests phase {task_phase}",
                        context="Task will be assigned to its container phase",
                    )
                )
                task_phase = phase_num

            if not description:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Task {task_id} has empty description",
                    )
                )

            parsed_tasks.append(
                ParsedTask(
                    id=task_id.upper(),
                    phase_number=task_phase,
                    task_number=task_num,
                    description=description,
                    acceptance_criteria=acceptance,
                    files_affected=files,
                )
            )

        phases.append(
            ParsedPhase(
                number=phase_num,
                name=phase_name,
                goal=phase_goal,
                tasks=parsed_tasks,
                dependencies=phase_dependencies,
                exit_criteria=phase_exit_criteria,
            )
        )

    # Sort phases by number
    phases.sort(key=lambda p: p.number)

    return phases, warnings


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


def extract_pr_metadata_from_yaml(
    yaml_data: dict[str, Any] | None,
) -> tuple[str | None, str | None, list[ParseWarning]]:
    """
    Extract PR metadata (title and description) from YAML data.

    Expected format in yaml-tasks block:
    ```yaml
    # yaml-tasks
    pr:
      title: "PR title here"
      description: |
        PR description here.
    phases:
      ...
    ```

    Args:
        yaml_data: Parsed YAML data from code fence or frontmatter

    Returns:
        Tuple of (pr_title, pr_description, warnings)
    """
    warnings: list[ParseWarning] = []

    if yaml_data is None:
        return None, None, warnings

    pr_data = yaml_data.get("pr")
    if pr_data is None:
        return None, None, warnings

    if not isinstance(pr_data, dict):
        warnings.append(
            ParseWarning(
                line_number=None,
                message=f"'pr' field must be an object, got {type(pr_data).__name__}",
                context="PR metadata will be ignored",
            )
        )
        return None, None, warnings

    pr_title = pr_data.get("title")
    pr_description = pr_data.get("description", "")

    if pr_title is None:
        warnings.append(
            ParseWarning(
                line_number=None,
                message="'pr' object is missing required 'title' field",
                context="PR metadata will be ignored",
            )
        )
        return None, None, warnings

    if not isinstance(pr_title, str):
        warnings.append(
            ParseWarning(
                line_number=None,
                message=f"'pr.title' must be a string, got {type(pr_title).__name__}",
                context="PR metadata will be ignored",
            )
        )
        return None, None, warnings

    pr_title = pr_title.strip()
    if not pr_title:
        warnings.append(
            ParseWarning(
                line_number=None,
                message="'pr.title' cannot be empty",
                context="PR metadata will be ignored",
            )
        )
        return None, None, warnings

    # Normalize description to string
    if pr_description is None:
        pr_description = ""
    elif not isinstance(pr_description, str):
        pr_description = str(pr_description)
    pr_description = pr_description.strip()

    return pr_title, pr_description, warnings


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
            tasks = parse_tasks_from_yaml(frontmatter_yaml)

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
        )

    # Extract PR metadata from YAML data
    pr_title, pr_description, pr_warnings = extract_pr_metadata_from_yaml(yaml_data)
    warnings.extend(pr_warnings)

    return ParseResult(
        success=True,
        phases=phases,
        warnings=warnings,
        raw_yaml=yaml_data,
        pr_title=pr_title,
        pr_description=pr_description,
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
