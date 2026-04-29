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
    The structured appendix should be a YAML code block with the marker comment.
    Use block scalars (``|-``) for ``name``, ``goal``, ``description``, and
    ``acceptance`` — plain unquoted scalars break when the value contains a
    ``: `` sequence (e.g. a backticked ``code: type`` snippet), because PyYAML
    interprets it as a nested mapping and raises ``ScannerError``. See #1974.

    ```yaml
    # yaml-tasks
    phases:
      - id: 1
        name: |-
          Setup
        goal: |-
          Initialize the project
        tasks:
          - id: TASK-1-1
            description: |-
              Create contract JSON schema
            acceptance: |-
              Schema validates sample contracts
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

from .agent_roles import EXECUTION_ROLE_VALUES
from .models import Slice, SliceStatus, Task, TaskStatus

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
    role: str | None = None

    def to_contract_task(self) -> Task:
        """Convert to a contract Task model."""
        return Task(
            id=f"task-{self.phase_number}-{self.task_number}",
            description=self.description,
            status=TaskStatus.PENDING,
            acceptance_criteria=self.acceptance_criteria,
            files_affected=self.files_affected,
            role=self.role,
        )


@dataclass
class ParsedPhase:
    """A slice (legacy: phase) extracted from a plan document.

    The class name is preserved for backward compat but post-#2137 the
    canonical SDLC term is "slice". The plan parser accepts either
    ``slices:`` (canonical) or ``phases:`` (legacy alias) at the
    ``# yaml-tasks`` level — see ``parse_phases_from_yaml``.
    """

    number: int
    name: str
    goal: str
    tasks: list[ParsedTask] = field(default_factory=list)
    dependencies: str = ""
    exit_criteria: str = ""
    serialized_chain_order: list[str] = field(default_factory=list)

    def to_contract_phase(self) -> Slice:
        """Convert to a contract Slice model (legacy alias name)."""
        return self.to_contract_slice()

    def to_contract_slice(self) -> Slice:
        """Convert to a contract Slice model.

        Renamed from ``to_contract_phase`` in #2137. The output uses
        the canonical ``slice-<N>`` ID shape; legacy ``phase-<N>``
        dependency strings emitted by older planners are translated
        to ``slice-<N>`` so post-rename consumers see a uniform DAG.
        """
        # Normalize dependencies to slice-N format
        normalized_deps: list[str] = []
        if self.dependencies:
            raw_deps: str | list[str] = self.dependencies
            # Handle both list and string formats
            dep_list: list[str]
            if isinstance(raw_deps, str):
                dep_list = [d.strip() for d in raw_deps.split(",") if d.strip()]
            else:
                dep_list = raw_deps
            if isinstance(dep_list, list):
                for dep in dep_list:
                    dep_str = str(dep).strip()
                    if dep_str.startswith("slice-"):
                        normalized_deps.append(dep_str)
                    elif dep_str.startswith("phase-"):
                        # Legacy planner output — rewrite the prefix.
                        normalized_deps.append("slice-" + dep_str[len("phase-") :])
                    else:
                        # Try to extract slice/phase number — prefer
                        # explicit "slice N" / "phase N" patterns to
                        # avoid extracting unrelated numbers from prose.
                        m = re.search(r"(?:slice|phase)\s*(\d+)", dep_str, re.IGNORECASE)
                        if not m:
                            # Fall back to bare number only if the string is
                            # short (likely just "1" or "2", not prose).
                            if len(dep_str) <= 10:
                                m = re.search(r"(\d+)", dep_str)
                        if m:
                            normalized_deps.append(f"slice-{m.group(1)}")

        # Normalise serialized_chain_order entries the same way so the
        # planner can emit either ``slice-N`` or ``phase-N`` and the
        # contract always sees the canonical form.
        normalised_chain: list[str] = []
        for entry in self.serialized_chain_order:
            entry_str = str(entry).strip()
            if entry_str.startswith("slice-"):
                normalised_chain.append(entry_str)
            elif entry_str.startswith("phase-"):
                normalised_chain.append("slice-" + entry_str[len("phase-") :])
            else:
                m = re.search(r"(?:slice|phase)\s*(\d+)", entry_str, re.IGNORECASE)
                if not m and len(entry_str) <= 10:
                    m = re.search(r"(\d+)", entry_str)
                if m:
                    normalised_chain.append(f"slice-{m.group(1)}")

        return Slice(
            id=f"slice-{self.number}",
            name=self.name,
            status=SliceStatus.PENDING,
            tasks=[task.to_contract_task() for task in self.tasks],
            dependencies=normalized_deps,
            serialized_chain_order=normalised_chain,
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
    pr_test_plan: str | None = None
    pr_manual_steps: str | None = None

    def to_contract_phases(self) -> list[Slice]:
        """Backward-compat alias for ``to_contract_slices`` (#2137).

        The canonical name is now ``to_contract_slices`` since the
        contract field is ``slices``; this alias keeps existing
        callers working during the transition window.
        """
        return self.to_contract_slices()

    def to_contract_slices(self) -> list[Slice]:
        """Convert all parsed slices to contract Slice models."""
        return [phase.to_contract_slice() for phase in self.phases]


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


def parse_tasks_from_yaml(
    yaml_data: dict[str, Any],
) -> tuple[list[ParsedTask], list[ParseWarning]]:
    """
    Parse tasks from YAML front matter (legacy flat task list format).

    Args:
        yaml_data: Parsed YAML data

    Returns:
        Tuple of (list of ParsedTask objects, list of ParseWarning objects)
    """
    tasks = []
    warnings: list[ParseWarning] = []
    task_list = yaml_data.get("tasks", [])

    for task_data in task_list:
        task_id = task_data.get("id", "")
        # Parse task ID: TASK-{phase}-{number}
        # Anchored to end-of-string — see #1988.
        match = re.match(r"TASK-(\d+)-(\d+)\Z", task_id, re.IGNORECASE)
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
        else:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=f"Task ID '{task_id}' doesn't match pattern TASK-N-N, skipping",
                    context=f"Description: {task_data.get('description', 'none')}",
                )
            )

    return tasks, warnings


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

    # Accept either ``slices:`` (canonical post-#2137) or ``phases:``
    # (legacy alias). When both keys are present ``slices`` wins and a
    # warning surfaces; when neither is present we fall through to the
    # ``tasks:`` flat-list legacy path below.
    slices_list = yaml_data.get("slices", [])
    legacy_phases_list = yaml_data.get("phases", [])

    # Reject multi-PR format: pr_plan key indicates the LLM proposed
    # multiple PRs, which violates the one-issue-one-PR constraint.
    if "pr_plan" in yaml_data:
        if not slices_list and not legacy_phases_list:
            # pr_plan without slices/phases means the LLM put the task
            # breakdown under the wrong key — treat as a parse error.
            return [], [
                ParseWarning(
                    line_number=None,
                    message="'pr_plan' key found without 'slices' or 'phases' — "
                    "the plan uses the unsupported multi-PR format. Each issue must "
                    "produce exactly one PR using the 'slices' (canonical) or "
                    "'phases' (legacy) key.",
                    context="The 'pr_plan' multi-PR format is not supported",
                )
            ]
        warnings.append(
            ParseWarning(
                line_number=None,
                message="'pr_plan' key is not supported — use 'pr' (singular) instead. "
                "Each issue must produce exactly one PR.",
                context="The 'pr_plan' multi-PR format will be ignored",
            )
        )

    if slices_list and legacy_phases_list:
        warnings.append(
            ParseWarning(
                line_number=None,
                message=(
                    "yaml-tasks contains both 'slices:' and 'phases:' keys — "
                    "'slices' wins. Remove 'phases:' to silence this warning."
                ),
                context="Canonical key is 'slices' post-#2137",
            )
        )
        phase_list: list[Any] = slices_list
    elif slices_list:
        phase_list = slices_list
    else:
        phase_list = legacy_phases_list

    if not phase_list:
        # Check for legacy flat task list format
        if "tasks" in yaml_data:
            return [], warnings  # Let caller fall back to legacy parsing
        warnings.append(
            ParseWarning(
                line_number=None,
                message="yaml-tasks block has no 'slices' or 'phases' key",
                context="Expected format: slices: [...] (or legacy phases: [...])",
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
            # Try extracting number from string like "phase-1" or "slice-1"
            id_match = re.search(r"(\d+)", str(phase_id))
            if id_match:
                phase_num = int(id_match.group(1))
            else:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Cannot parse slice/phase ID: {phase_id}",
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

        phase_name = phase_data.get("name", f"Slice {phase_num}")
        phase_goal = phase_data.get("goal", "")
        phase_dependencies = phase_data.get("dependencies", "")
        phase_exit_criteria = phase_data.get("exit_criteria", "")
        # ``serialized_chain_order`` is a planner-emitted field added in
        # #2137. The planner uses it to record the deliberate ordering
        # of would-be multi-parent slices when it serialises the
        # upstream cluster into a chain. Validation that each entry
        # references a real sibling slice id happens at the parser
        # level (warning) and at ingestion (forest validation).
        phase_serialized_chain_order_raw = phase_data.get("serialized_chain_order", [])
        if isinstance(phase_serialized_chain_order_raw, str):
            phase_serialized_chain_order = [
                e.strip() for e in phase_serialized_chain_order_raw.split(",") if e.strip()
            ]
        elif isinstance(phase_serialized_chain_order_raw, list):
            phase_serialized_chain_order = [
                str(e).strip() for e in phase_serialized_chain_order_raw if str(e).strip()
            ]
        else:
            phase_serialized_chain_order = []
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Slice {phase_num} 'serialized_chain_order' must be a "
                        f"list or comma-separated string; got "
                        f"{type(phase_serialized_chain_order_raw).__name__} — "
                        "ignoring"
                    ),
                )
            )

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
            role = task_data.get("role")

            # Ensure files is a list
            if isinstance(files, str):
                files = [files]
            elif not isinstance(files, list):
                files = []

            # Parse task ID: TASK-{phase}-{number}
            # Anchor to end-of-string so IDs like TASK-1-3A don't silently
            # match as (1, 3) and collide with TASK-1-3B.  See #1988.
            id_match = re.match(r"TASK-(\d+)-(\d+)\Z", str(task_id), re.IGNORECASE)
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

            # Validate role if provided
            if role is not None and role not in EXECUTION_ROLE_VALUES:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Task {task_id} has invalid role '{role}', ignoring",
                        context=f"Valid roles: {', '.join(sorted(EXECUTION_ROLE_VALUES))}",
                    )
                )
                role = None

            parsed_tasks.append(
                ParsedTask(
                    id=task_id.upper(),
                    phase_number=task_phase,
                    task_number=task_num,
                    description=description,
                    acceptance_criteria=acceptance,
                    files_affected=files,
                    role=role,
                )
            )

        # Sanity check: flag duplicate contract ids within this phase so a
        # silent collision can't ship (see #1988).  Task.id pattern accepts
        # duplicates, so Pydantic won't catch this on its own.
        seen_contract_ids: set[str] = set()
        for pt in parsed_tasks:
            contract_id = f"task-{pt.phase_number}-{pt.task_number}"
            if contract_id in seen_contract_ids:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=f"Duplicate task id '{contract_id}' in phase {phase_num}",
                        context=f"Source task ID: {pt.id}",
                    )
                )
            seen_contract_ids.add(contract_id)

        phases.append(
            ParsedPhase(
                number=phase_num,
                name=phase_name,
                goal=phase_goal,
                tasks=parsed_tasks,
                dependencies=phase_dependencies,
                exit_criteria=phase_exit_criteria,
                serialized_chain_order=phase_serialized_chain_order,
            )
        )

    # Sort phases by number
    phases.sort(key=lambda p: p.number)

    # Validate ``serialized_chain_order`` references — entries must
    # name real sibling slice IDs (otherwise the chain can't be
    # honoured at ingestion). Surfaces as a warning per TASK-2-1
    # acceptance.
    known_slice_ids = {f"slice-{p.number}" for p in phases}
    known_phase_ids = {f"phase-{p.number}" for p in phases}
    for parsed in phases:
        for entry in parsed.serialized_chain_order:
            normalised = entry
            if entry.startswith("phase-"):
                normalised = "slice-" + entry[len("phase-") :]
            if (
                normalised not in known_slice_ids
                and entry not in known_phase_ids
                and entry not in known_slice_ids
            ):
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=(
                            f"Slice {parsed.number} 'serialized_chain_order' "
                            f"references unknown sibling '{entry}'"
                        ),
                        context=("serialized_chain_order entries must name real sibling slice IDs"),
                    )
                )

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


def _normalize_optional_string(value: Any) -> str:
    """Normalize an optional YAML value to a stripped string.

    Treats the literal string 'None' (case-insensitive) as empty, since prompt
    examples tell agents to write 'None' when there are no steps.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value_str: str = str(value).strip()
    else:
        value_str = value.strip()
    if value_str.lower() == "none":
        return ""
    # Handle multi-line fields where every line's value is 'None', e.g.:
    #   Pre-merge: None
    #   Post-merge: None
    lines = value_str.splitlines()
    if lines and all(_line_value_is_none(line) for line in lines if line.strip()):
        return ""
    return value_str


def _line_value_is_none(line: str) -> bool:
    """Check if a 'Label: Value' line has 'None' as its value."""
    if ":" in line:
        _, _, val = line.partition(":")
        return val.strip().lower() == "none"
    return line.strip().lower() == "none"


def extract_pr_metadata_from_yaml(
    yaml_data: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None, str | None, list[ParseWarning]]:
    """
    Extract PR metadata (title, description, test_plan, manual_steps) from YAML data.

    Expected format in yaml-tasks block:
    ```yaml
    # yaml-tasks
    pr:
      title: "PR title here"
      description: |
        PR description here.
      test_plan: |
        - Automated: tests that cover the changes
        - Manual: steps for reviewers to verify
      manual_steps: |
        Pre-merge: any steps before merging
        Post-merge: any steps after merging
    phases:
      ...
    ```

    Args:
        yaml_data: Parsed YAML data from code fence or frontmatter

    Returns:
        Tuple of (pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings)
    """
    warnings: list[ParseWarning] = []

    if yaml_data is None:
        return None, None, None, None, warnings

    pr_data = yaml_data.get("pr")
    if pr_data is None:
        return None, None, None, None, warnings

    if not isinstance(pr_data, dict):
        warnings.append(
            ParseWarning(
                line_number=None,
                message=f"'pr' field must be an object, got {type(pr_data).__name__}",
                context="PR metadata will be ignored",
            )
        )
        return None, None, None, None, warnings

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
        return None, None, None, None, warnings

    if not isinstance(pr_title, str):
        warnings.append(
            ParseWarning(
                line_number=None,
                message=f"'pr.title' must be a string, got {type(pr_title).__name__}",
                context="PR metadata will be ignored",
            )
        )
        return None, None, None, None, warnings

    pr_title = pr_title.strip()
    if not pr_title:
        warnings.append(
            ParseWarning(
                line_number=None,
                message="'pr.title' cannot be empty",
                context="PR metadata will be ignored",
            )
        )
        return None, None, None, None, warnings

    # Normalize description to string
    pr_description = _normalize_optional_string(pr_description)

    # Extract test_plan and manual_steps (optional fields)
    pr_test_plan = _normalize_optional_string(pr_data.get("test_plan"))
    pr_manual_steps = _normalize_optional_string(pr_data.get("manual_steps"))

    # Warn if title exceeds recommended length (70 chars for GitHub readability)
    if len(pr_title) > 70:
        warnings.append(
            ParseWarning(
                line_number=None,
                message=f"PR title exceeds recommended length of 70 characters ({len(pr_title)} chars)",
                context="Consider shortening for better readability in GitHub UI",
            )
        )

    # Warn if test_plan is missing (it's strongly recommended)
    if not pr_test_plan:
        warnings.append(
            ParseWarning(
                line_number=None,
                message="'pr.test_plan' is missing — PRs should include a test plan",
                context="Consider adding automated and manual verification steps",
            )
        )

    return pr_title, pr_description, pr_test_plan, pr_manual_steps, warnings


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


# ---------------------------------------------------------------------------
# #2137 — slice DAG forest validation
# ---------------------------------------------------------------------------


def validate_forest(slices: list[Slice]) -> list[str]:
    """Walk the slice DAG and reject any slice with >1 parent.

    Added in #2137 (TASK-2-2). The slice scheduler / stacked-PR
    machinery requires the implement-phase slice DAG to be a forest:
    each slice has at most one DAG parent. Multi-parent slices break
    the stacking invariant (a child PR has exactly one base) and are
    rejected at plan ingestion so the plan reviewer NACKs the planner.

    Args:
        slices: The slice list extracted from the contract / plan.

    Returns:
        A list of structured-error strings — one entry per offending
        slice. An empty list means the DAG is a valid forest. Each
        entry is a human-readable, reviewer-NACK-able message that
        explicitly names the offender, its parents, and the
        ``serialized_chain_order`` remediation (per refine-phase
        decision-17).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    for slice_ in slices:
        if slice_.id in seen_ids:
            errors.append(
                f"Duplicate slice id '{slice_.id}' — every slice must have a "
                "unique identifier within the contract"
            )
        seen_ids.add(slice_.id)

    for slice_ in slices:
        deps = slice_.dependencies or []
        # Filter out unknown dependency targets — those are a separate
        # ingestion error and would otherwise drown the forest signal.
        real_parents = [d for d in deps if d in seen_ids]
        if len(real_parents) > 1:
            errors.append(
                f"Slice '{slice_.id}' has {len(real_parents)} DAG parents "
                f"({sorted(real_parents)!r}); the implement-phase slice DAG "
                "must be a forest (≤1 parent per slice). Serialise the "
                "upstream cluster into a chain and record the chosen order "
                "on this slice's 'serialized_chain_order' field — see "
                "issue #2137 plan TASK-2-3 for the auto-serialization rule."
            )

    # Cycle detection — a forest is by definition acyclic. A cyclic
    # ``slice-1 → slice-2 → slice-1`` chain has every slice with
    # exactly one parent, so the parent-count check above lets it
    # through; without this DFS the run loop's
    # ``while not scheduler.all_done():`` would spin forever.
    cycle_offenders = _detect_cycles(slices, seen_ids)
    for cycle in cycle_offenders:
        errors.append(
            f"Slice DAG contains a cycle: {' → '.join(cycle + [cycle[0]])}. "
            "Slices form an acyclic forest — break the cycle by removing "
            "or re-pointing one of the offending dependencies."
        )

    return errors


def _detect_cycles(slices: list[Slice], known_ids: set[str]) -> list[list[str]]:
    """Return a list of one slice-id chain per cycle in the slice DAG.

    DFS-based cycle detection. Returns one representative chain per
    cycle (so a 3-node cycle reports once, not three times). Unknown
    ids in ``dependencies`` are silently skipped here — they're
    reported by other validators.
    """
    adj: dict[str, list[str]] = {}
    for slice_ in slices:
        deps = [d for d in (slice_.dependencies or []) if d in known_ids]
        adj[slice_.id] = deps

    visited: set[str] = set()
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    seen_cycles: set[frozenset[str]] = set()

    def dfs(node: str, path: list[str]) -> None:
        visited.add(node)
        on_stack.add(node)
        path.append(node)
        for nxt in adj.get(node, []):
            if nxt in on_stack:
                # Found a cycle — slice the path from where ``nxt``
                # was first seen to ``node`` inclusive.
                if nxt in path:
                    cycle = path[path.index(nxt) :]
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(list(cycle))
            elif nxt not in visited:
                dfs(nxt, path)
        on_stack.discard(node)
        path.pop()

    for node in adj:
        if node not in visited:
            dfs(node, [])

    return cycles


__all__ = (
    "ParsedPhase",
    "ParsedTask",
    "ParseResult",
    "ParseWarning",
    "format_warnings_for_comment",
    "parse_plan",
    "parse_plan_file",
    "validate_forest",
)
