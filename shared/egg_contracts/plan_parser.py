"""
Plan document parser for extracting tasks into contract JSON.

Parses plan documents (docs/issues/{number}-plan.md) and extracts:
- Implementation phases
- Tasks with IDs, descriptions, acceptance criteria, and files
- Phase goals and exit criteria
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Contract, Phase, PhaseStatus, Task, TaskStatus


@dataclass
class ParsedTask:
    """A task extracted from the plan document."""

    id: str
    description: str
    acceptance_criteria: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class ParsedPhase:
    """A phase extracted from the plan document."""

    id: str
    name: str
    goal: str = ""
    tasks: list[ParsedTask] = field(default_factory=list)
    dependencies: str = ""
    exit_criteria: str = ""


@dataclass
class ParsedPlan:
    """Complete parsed plan document."""

    summary: str = ""
    phases: list[ParsedPhase] = field(default_factory=list)
    test_strategy: str = ""
    rollback: str = ""
    migration: str = ""


def parse_plan_document(content: str) -> ParsedPlan:
    """
    Parse a plan document and extract phases and tasks.

    Args:
        content: Markdown content of the plan document

    Returns:
        ParsedPlan with extracted data
    """
    plan = ParsedPlan()
    lines = content.split("\n")

    current_section = None
    current_phase: ParsedPhase | None = None
    phase_counter = 0

    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect main sections
        if line.startswith("## Summary"):
            current_section = "summary"
            i += 1
            continue
        elif line.startswith("## Implementation Phases"):
            current_section = "phases"
            i += 1
            continue
        elif line.startswith("## Test Strategy"):
            current_section = "test_strategy"
            i += 1
            continue
        elif line.startswith("## Rollback"):
            current_section = "rollback"
            i += 1
            continue
        elif line.startswith("## Migration"):
            current_section = "migration"
            i += 1
            continue
        elif line.startswith("## "):
            current_section = None
            i += 1
            continue

        # Parse phase headers (### Phase N: Name)
        phase_match = re.match(r"^###\s+Phase\s+(\d+):\s+(.+)$", line)
        if phase_match:
            phase_counter += 1
            phase_num = phase_match.group(1)
            phase_name = phase_match.group(2).strip()

            # Save previous phase
            if current_phase:
                plan.phases.append(current_phase)

            current_phase = ParsedPhase(
                id=f"phase-{phase_num}",
                name=phase_name,
            )
            i += 1
            continue

        # Parse phase subsections
        if current_phase:
            if line.startswith("**Goal**:"):
                current_phase.goal = line.replace("**Goal**:", "").strip()
                i += 1
                continue
            elif line.startswith("**Dependencies**:"):
                current_phase.dependencies = line.replace("**Dependencies**:", "").strip()
                i += 1
                continue
            elif line.startswith("**Exit criteria**:"):
                current_phase.exit_criteria = line.replace("**Exit criteria**:", "").strip()
                i += 1
                continue
            elif line.startswith("**Tasks**:"):
                # Parse task table
                i += 1
                tasks = parse_task_table(lines, i)
                current_phase.tasks.extend(tasks)
                # Skip past table
                while i < len(lines) and (lines[i].startswith("|") or lines[i].strip() == ""):
                    i += 1
                continue

        # Collect content for current section
        if current_section == "summary" and line.strip():
            plan.summary += line + "\n"
        elif current_section == "test_strategy" and line.strip():
            plan.test_strategy += line + "\n"
        elif current_section == "rollback" and line.strip():
            plan.rollback += line + "\n"
        elif current_section == "migration" and line.strip():
            plan.migration += line + "\n"

        i += 1

    # Save last phase
    if current_phase:
        plan.phases.append(current_phase)

    # Clean up collected text
    plan.summary = plan.summary.strip()
    plan.test_strategy = plan.test_strategy.strip()
    plan.rollback = plan.rollback.strip()
    plan.migration = plan.migration.strip()

    return plan


def parse_task_table(lines: list[str], start_idx: int) -> list[ParsedTask]:
    """
    Parse a markdown table of tasks.

    Expected format:
    | ID | Description | Acceptance Criteria | Files |
    |----|-------------|---------------------|-------|
    | task-1 | ... | ... | `file.py` |

    Args:
        lines: All lines of the document
        start_idx: Index where table starts

    Returns:
        List of parsed tasks
    """
    tasks = []

    # Skip header and separator rows
    i = start_idx
    while i < len(lines) and not lines[i].startswith("|"):
        i += 1

    if i >= len(lines):
        return tasks

    # Skip header row
    i += 1
    # Skip separator row
    if i < len(lines) and re.match(r"^\|[-\s|]+\|$", lines[i]):
        i += 1

    # Parse data rows
    while i < len(lines) and lines[i].startswith("|"):
        row = lines[i]
        cells = [c.strip() for c in row.split("|")[1:-1]]  # Remove empty first/last

        if len(cells) >= 2:
            task_id = cells[0].strip()
            description = cells[1].strip() if len(cells) > 1 else ""
            acceptance = cells[2].strip() if len(cells) > 2 else ""
            files_str = cells[3].strip() if len(cells) > 3 else ""

            # Extract file paths from backticks
            files = re.findall(r"`([^`]+)`", files_str)

            # Clean up task ID (remove underscores in case of markdown formatting)
            task_id = task_id.replace("_", "")

            # Only add if it looks like a valid task
            if task_id.startswith("task-") and description:
                tasks.append(
                    ParsedTask(
                        id=task_id,
                        description=description.replace("_", " ").strip(),
                        acceptance_criteria=acceptance.replace("_", " ").strip(),
                        files=files,
                    )
                )

        i += 1

    return tasks


def load_plan_from_file(repo_root: Path | str, issue_number: int) -> ParsedPlan | None:
    """
    Load and parse a plan document for an issue.

    Args:
        repo_root: Path to repository root
        issue_number: GitHub issue number

    Returns:
        ParsedPlan or None if file not found
    """
    repo_root = Path(repo_root)
    plan_path = repo_root / "docs" / "issues" / f"{issue_number}-plan.md"

    if not plan_path.exists():
        return None

    with open(plan_path) as f:
        content = f.read()

    return parse_plan_document(content)


def extract_tasks_to_contract(
    contract: Contract,
    plan: ParsedPlan,
) -> Contract:
    """
    Extract tasks from a parsed plan into a contract.

    Args:
        contract: Contract to update
        plan: Parsed plan document

    Returns:
        Updated contract with phases and tasks
    """
    # Clear existing phases (they'll be replaced)
    contract.phases = []

    for parsed_phase in plan.phases:
        phase = Phase(
            id=parsed_phase.id,
            name=parsed_phase.name,
            status=PhaseStatus.PENDING,
            tasks=[],
        )

        for parsed_task in parsed_phase.tasks:
            task = Task(
                id=parsed_task.id,
                description=parsed_task.description,
                acceptance_criteria=parsed_task.acceptance_criteria,
                files=parsed_task.files if parsed_task.files else None,
                status=TaskStatus.PENDING,
            )
            phase.tasks.append(task)

        contract.phases.append(phase)

    return contract


def sync_contract_from_plan(
    repo_root: Path | str,
    issue_number: int,
) -> bool:
    """
    Sync a contract's tasks from its plan document.

    Args:
        repo_root: Path to repository root
        issue_number: GitHub issue number

    Returns:
        True if sync successful
    """
    from .loader import load_contract, save_contract

    contract = load_contract(repo_root, issue_number)
    if not contract:
        return False

    plan = load_plan_from_file(repo_root, issue_number)
    if not plan:
        return False

    contract = extract_tasks_to_contract(contract, plan)
    save_contract(contract, repo_root)

    return True
