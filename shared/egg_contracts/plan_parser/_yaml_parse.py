"""YAML extraction layer for the plan parser.

Parses the ``# yaml-tasks`` code fence (preferred), legacy YAML front
matter, and the per-task / per-slice / ``pr`` blocks into the
:class:`~egg_contracts.plan_parser._models.ParsedPhase` /
:class:`~egg_contracts.plan_parser._models.ParsedTask` model layer.
Extracted verbatim from the pre-split ``plan_parser.py`` (#3312 slice-7);
every function is AST-identical and re-exports through the package barrel.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from ..agent_roles import EXECUTION_ROLE_VALUES
from ._models import (
    _JIRA_KEY_PATTERN,
    _KNOWN_SLICE_KEYS,
    JIRA_ACTION_STATUS_VALUES,
    JIRA_ACTION_VALUES,
    YAML_FENCE_PATTERN,
    ParsedPhase,
    ParsedTask,
    ParseWarning,
)


def _extract_jira_task_fields(
    task_data: dict[str, Any],
    task_id: str,
    warnings: list[ParseWarning],
) -> tuple[str | None, str | None, str | None]:
    """Extract ``jira_key``, ``jira_action``, and ``jira_action_status``
    from a parsed-YAML task dict (issue #1557).

    Unknown ``jira_action`` / ``jira_action_status`` values surface as
    ParseWarnings and resolve to ``None`` rather than being silently
    dropped — matches the contract task-1-3 acceptance:
    "Non-literal ``jira_action`` or ``jira_action_status`` produces a
    warning, not a silent drop."

    A ``jira_key`` whose shape doesn't match the canonical pattern
    surfaces as a ParseWarning and resolves to ``None`` for the same
    reason.

    Returns a (jira_key, jira_action, jira_action_status) tuple where
    each element is either a validated string or ``None``.
    """
    raw_key = task_data.get("jira_key")
    jira_key: str | None = None
    if raw_key is not None:
        if isinstance(raw_key, str):
            trimmed = raw_key.strip()
            if not trimmed:
                jira_key = None
            elif _JIRA_KEY_PATTERN.match(trimmed):
                jira_key = trimmed
            else:
                warnings.append(
                    ParseWarning(
                        line_number=None,
                        message=(
                            f"Task {task_id} has invalid jira_key "
                            f"'{trimmed}' (expected <PROJECT>-<number> "
                            "shape); ignoring"
                        ),
                    )
                )
        else:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Task {task_id} jira_key must be a string; "
                        f"got {type(raw_key).__name__}, ignoring"
                    ),
                )
            )

    raw_action = task_data.get("jira_action")
    jira_action: str | None = None
    if raw_action is not None:
        if isinstance(raw_action, str) and raw_action in JIRA_ACTION_VALUES:
            jira_action = raw_action
        else:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Task {task_id} has invalid jira_action "
                        f"'{raw_action}' (valid: "
                        f"{', '.join(sorted(JIRA_ACTION_VALUES))}); "
                        "ignoring"
                    ),
                )
            )

    raw_status = task_data.get("jira_action_status")
    jira_action_status: str | None = None
    if raw_status is not None:
        if isinstance(raw_status, str) and raw_status in JIRA_ACTION_STATUS_VALUES:
            jira_action_status = raw_status
        else:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Task {task_id} has invalid jira_action_status "
                        f"'{raw_status}' (valid: "
                        f"{', '.join(sorted(JIRA_ACTION_STATUS_VALUES))}); "
                        "ignoring"
                    ),
                )
            )

    return jira_key, jira_action, jira_action_status


def parse_yaml_code_fence(content: str) -> tuple[dict[str, Any] | None, str, list[ParseWarning]]:
    """
    Extract YAML data from a code fence with the yaml-tasks marker.

    The code fence must be formatted as:
    ```yaml
    # yaml-tasks
    slices:
      - id: 1
        name: Slice Name
        ...
    ```

    The legacy ``phases:`` key is also accepted for backward compatibility.

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

            # Issue #1557: per-task Jira mapping (epic-mode only — fields
            # are ``None`` on ticket / github_issue mode plans).
            jira_key, jira_action, jira_action_status = _extract_jira_task_fields(
                task_data, task_id, warnings
            )

            tasks.append(
                ParsedTask(
                    id=task_id,
                    phase_number=phase_num,
                    task_number=task_num,
                    description=task_data.get("description", ""),
                    acceptance_criteria=task_data.get("acceptance", ""),
                    files_affected=files,
                    jira_key=jira_key,
                    jira_action=jira_action,
                    jira_action_status=jira_action_status,
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

    Expected format (post-#2137):
    ```yaml
    # yaml-tasks
    slices:
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

    The legacy ``phases:`` key is accepted as an alias for ``slices:``;
    when both are present, ``slices:`` wins.

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

    # Reject ad-hoc multi-PR `pr_plan` format. Slice packaging is owned by
    # the `slices:` DAG (one slice = one stacked PR, post-#2137); `pr_plan`
    # is not a supported decomposition format regardless of whether the
    # plan ships as one or many PRs.
    if "pr_plan" in yaml_data:
        if not slices_list and not legacy_phases_list:
            # pr_plan without slices/phases means the LLM put the task
            # breakdown under the wrong key — treat as a parse error.
            return [], [
                ParseWarning(
                    line_number=None,
                    message="'pr_plan' key found without 'slices' or 'phases' — "
                    "'pr_plan' is not a supported decomposition format. Use the "
                    "'slices' (canonical, post-#2137) or 'phases' (legacy) key "
                    "to express the slice DAG; the implement-phase pipeline "
                    "ships each slice as its own stacked PR.",
                    context="The 'pr_plan' format is not supported; use 'slices'",
                )
            ]
        warnings.append(
            ParseWarning(
                line_number=None,
                message="'pr_plan' key is not supported — use the 'slices' key "
                "to express the slice DAG, and the singular 'pr' key for the "
                "per-PR metadata block (title, description, test_plan, "
                "manual_steps).",
                context="The 'pr_plan' format will be ignored; use 'slices' + 'pr'",
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
        except ValueError, TypeError:
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

        # #2870 — flag keys the parser doesn't consume. The yaml-tasks
        # schema is ``additionalProperties: false`` but is never validated
        # at parse time, so a stray key (e.g. ``parent_slice_id``) is
        # otherwise dropped silently — taking its data with it. ``id`` is
        # always present; report the rest sorted for a stable message.
        unknown_keys = sorted(set(phase_data) - _KNOWN_SLICE_KEYS)
        if unknown_keys:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Slice {phase_num} has unrecognized key(s) "
                        f"{unknown_keys} — ignored by the parser, so any "
                        "data they carry (e.g. slice ordering) is silently "
                        "dropped. Slice ordering must use 'dependencies' "
                        "(canonical) or 'depends_on' (see #2870)."
                    ),
                    context="Allowed slice keys: " + ", ".join(sorted(_KNOWN_SLICE_KEYS)),
                )
            )

        phase_name = phase_data.get("name", f"Slice {phase_num}")
        phase_goal = phase_data.get("goal", "")
        # #2743 — accept ``depends_on`` as an alias for ``dependencies``.
        # Pipeline-8b81ed32 produced a plan that used ``depends_on: <int>``
        # on every phase and the contract came back with empty deps because
        # the parser only consulted ``dependencies``. ``dependencies`` is
        # the schema-canonical key (.egg/schemas/yaml-tasks.schema.json);
        # when both are present it wins and a warning is recorded.
        if "dependencies" in phase_data and "depends_on" in phase_data:
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Slice {phase_num} declares both 'dependencies' "
                        "and 'depends_on' — preferring 'dependencies' "
                        "(canonical key per yaml-tasks.schema.json). "
                        "Remove 'depends_on' to silence this warning."
                    ),
                )
            )
            phase_dependencies = phase_data["dependencies"]
            dep_source_key = "dependencies"
        elif "depends_on" in phase_data:
            phase_dependencies = phase_data["depends_on"]
            dep_source_key = "depends_on"
        else:
            phase_dependencies = phase_data.get("dependencies", "")
            dep_source_key = "dependencies"
        # #2743 — surface a parse-time warning for ``bool`` values so a
        # ``parse_plan`` consumer sees that the dep was discarded. The
        # ``to_contract_slice`` branch drops bools to ``[]`` (since
        # ``bool`` is an ``int`` subclass and we don't want ``True`` to
        # become ``slice-1``); this warning records the silent drop.
        if isinstance(phase_dependencies, bool):
            warnings.append(
                ParseWarning(
                    line_number=None,
                    message=(
                        f"Slice {phase_num} '{dep_source_key}' is a bool "
                        f"({phase_dependencies!r}); dependencies dropped. "
                        "Use an int, 'slice-N', or a list."
                    ),
                )
            )
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

            # Issue #1557: per-task Jira mapping (epic-mode only).
            jira_key, jira_action, jira_action_status = _extract_jira_task_fields(
                task_data, task_id, warnings
            )

            parsed_tasks.append(
                ParsedTask(
                    id=task_id.upper(),
                    phase_number=task_phase,
                    task_number=task_num,
                    description=description,
                    acceptance_criteria=acceptance,
                    files_affected=files,
                    role=role,
                    jira_key=jira_key,
                    jira_action=jira_action,
                    jira_action_status=jira_action_status,
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
    slices:
      ...
    ```

    The legacy ``phases:`` key is accepted as an alias for ``slices:``.

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
