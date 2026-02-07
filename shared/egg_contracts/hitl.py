"""
HITL (Human-in-the-Loop) checkbox handling for SDLC pipeline.

Provides functionality for:
- Generating markdown checkbox blocks for human decisions
- Parsing checkbox state from GitHub issue/PR comments
- Handling debounce timing to prevent accidental double-clicks
- Updating comments to show countdown status

Debounce mechanism:
- When a checkbox is checked, a 30-second countdown starts
- During the countdown, additional changes reset the timer
- After the countdown, the decision is finalized and processed
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class HitlDecisionCategory(StrEnum):
    """Categories of HITL decisions."""

    GUIDANCE = "guidance"  # Provide additional context
    OVERRIDE = "override"  # Override current state
    MANUAL = "manual"  # Take manual action


class HitlOptionId(StrEnum):
    """Standard HITL option identifiers."""

    # Guidance options
    PROVIDE_CONTEXT = "provide_context"
    ADJUST_CRITERIA = "adjust_criteria"
    BREAK_INTO_SUBTASKS = "break_into_subtasks"

    # Override options
    MARK_COMPLETE = "mark_complete"
    SKIP_TASKS = "skip_tasks"
    CANCEL_PIPELINE = "cancel_pipeline"

    # Manual options
    COMPLETE_MANUALLY = "complete_manually"
    REASSIGN = "reassign"


@dataclass
class HitlOption:
    """A single HITL checkbox option."""

    id: str
    label: str
    category: HitlDecisionCategory
    checked: bool = False
    description: str | None = None


@dataclass
class HitlCheckboxState:
    """Parsed state of HITL checkboxes from a comment."""

    options: list[HitlOption]
    has_changes: bool
    debounce_until: datetime | None
    raw_comment: str

    def get_checked_options(self) -> list[HitlOption]:
        """Get all checked options."""
        return [opt for opt in self.options if opt.checked]

    def get_checked_by_category(self, category: HitlDecisionCategory) -> list[HitlOption]:
        """Get checked options in a specific category."""
        return [opt for opt in self.options if opt.checked and opt.category == category]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "options": [
                {
                    "id": opt.id,
                    "label": opt.label,
                    "category": opt.category,
                    "checked": opt.checked,
                }
                for opt in self.options
            ],
            "has_changes": self.has_changes,
            "debounce_until": self.debounce_until.isoformat() if self.debounce_until else None,
        }


# Default debounce period in seconds
DEFAULT_DEBOUNCE_SECONDS = 30


def generate_checkbox_block(
    options: list[HitlOption],
    category: HitlDecisionCategory,
    title: str | None = None,
) -> str:
    """
    Generate a markdown checkbox block for a category of options.

    Args:
        options: List of options to include
        category: Category for this block
        title: Optional title for the block

    Returns:
        Markdown string with checkboxes
    """
    lines = []

    if title:
        lines.append(f"### {title}")

    lines.append(f"<!-- HITL-DECISION: {category} -->")

    for opt in options:
        checkbox = "[x]" if opt.checked else "[ ]"
        line = f"- {checkbox} {opt.label}"
        if opt.description:
            line += f" — _{opt.description}_"
        lines.append(line)

    return "\n".join(lines)


def generate_debounce_notice(
    seconds_remaining: int,
    decision_summary: str | None = None,
) -> str:
    """
    Generate a debounce countdown notice.

    Args:
        seconds_remaining: Seconds until decision is finalized
        decision_summary: Optional summary of pending decision

    Returns:
        Markdown string with countdown notice
    """
    if seconds_remaining <= 0:
        return "> ✅ **Decision finalized.** Processing now..."

    lines = [
        f"> ⏳ **Debounce active:** Decision will be processed in **{seconds_remaining} seconds**.",
        "> ",
        "> _Making additional changes will reset the timer._",
    ]

    if decision_summary:
        lines.append(f"> \n> **Pending decision:** {decision_summary}")

    return "\n".join(lines)


def generate_full_hitl_block(
    issue_number: int,
    stuck_task_id: str | None = None,
    include_debounce_notice: bool = True,
) -> str:
    """
    Generate a complete HITL decision block for escalation.

    Args:
        issue_number: The issue number for context
        stuck_task_id: Optional ID of the stuck task
        include_debounce_notice: Whether to include debounce notice

    Returns:
        Full markdown block with all option categories
    """
    context = f" for task `{stuck_task_id}`" if stuck_task_id else ""

    blocks = [
        f"## Human Decision Required{context}",
        "",
        "Please select one or more options below to help the pipeline proceed.",
        "",
    ]

    # Guidance options
    guidance_options = [
        HitlOption(
            id=HitlOptionId.PROVIDE_CONTEXT,
            label="I will provide additional context or requirements below",
            category=HitlDecisionCategory.GUIDANCE,
        ),
        HitlOption(
            id=HitlOptionId.ADJUST_CRITERIA,
            label="The acceptance criteria should be adjusted",
            category=HitlDecisionCategory.GUIDANCE,
        ),
        HitlOption(
            id=HitlOptionId.BREAK_INTO_SUBTASKS,
            label="Break this task into smaller sub-tasks",
            category=HitlDecisionCategory.GUIDANCE,
        ),
    ]
    blocks.append(generate_checkbox_block(guidance_options, HitlDecisionCategory.GUIDANCE, "Option 1: Provide Guidance"))
    blocks.append("")

    # Override options
    override_options = [
        HitlOption(
            id=HitlOptionId.MARK_COMPLETE,
            label="Mark current tasks as complete (override review)",
            category=HitlDecisionCategory.OVERRIDE,
        ),
        HitlOption(
            id=HitlOptionId.SKIP_TASKS,
            label="Skip remaining tasks in this phase",
            category=HitlDecisionCategory.OVERRIDE,
        ),
        HitlOption(
            id=HitlOptionId.CANCEL_PIPELINE,
            label="Cancel the pipeline for this issue",
            category=HitlDecisionCategory.OVERRIDE,
        ),
    ]
    blocks.append(generate_checkbox_block(override_options, HitlDecisionCategory.OVERRIDE, "Option 2: Override"))
    blocks.append("")

    # Manual options
    manual_options = [
        HitlOption(
            id=HitlOptionId.COMPLETE_MANUALLY,
            label="I will complete the remaining work manually",
            category=HitlDecisionCategory.MANUAL,
        ),
        HitlOption(
            id=HitlOptionId.REASSIGN,
            label="Assign to a different agent/person",
            category=HitlDecisionCategory.MANUAL,
        ),
    ]
    blocks.append(generate_checkbox_block(manual_options, HitlDecisionCategory.MANUAL, "Option 3: Manual Intervention"))
    blocks.append("")

    if include_debounce_notice:
        blocks.append("---")
        blocks.append("")
        blocks.append(generate_debounce_notice(DEFAULT_DEBOUNCE_SECONDS))
        blocks.append("")

    blocks.append("---")
    blocks.append("")
    blocks.append("_Check your selection(s) above, then add context in a reply if needed._")

    return "\n".join(blocks)


# Regex patterns for parsing checkboxes
CHECKBOX_PATTERN = re.compile(r"^-\s*\[([ xX])\]\s*(.+?)(?:\s*—\s*_.+_)?$", re.MULTILINE)
HITL_MARKER_PATTERN = re.compile(r"<!--\s*HITL-DECISION:\s*(\w+)\s*-->")
DEBOUNCE_PATTERN = re.compile(r"in \*\*(\d+) seconds\*\*")


def parse_checkbox_state(
    comment_body: str,
    previous_state: HitlCheckboxState | None = None,
) -> HitlCheckboxState:
    """
    Parse checkbox state from a GitHub comment.

    Args:
        comment_body: The raw comment body
        previous_state: Previous state to compare for changes

    Returns:
        HitlCheckboxState with parsed options
    """
    options: list[HitlOption] = []
    current_category: HitlDecisionCategory | None = None

    # Split by lines and process
    lines = comment_body.split("\n")

    for line in lines:
        # Check for category marker
        marker_match = HITL_MARKER_PATTERN.search(line)
        if marker_match:
            category_str = marker_match.group(1).lower()
            if category_str in [c.value for c in HitlDecisionCategory]:
                current_category = HitlDecisionCategory(category_str)
            continue

        # Check for checkbox
        checkbox_match = CHECKBOX_PATTERN.match(line.strip())
        if checkbox_match and current_category:
            checked = checkbox_match.group(1).lower() == "x"
            label = checkbox_match.group(2).strip()

            # Try to map label to known option ID
            option_id = _label_to_option_id(label, current_category)

            options.append(HitlOption(
                id=option_id,
                label=label,
                category=current_category,
                checked=checked,
            ))

    # Determine if there are changes from previous state
    has_changes = False
    if previous_state:
        prev_checked = {opt.id for opt in previous_state.options if opt.checked}
        curr_checked = {opt.id for opt in options if opt.checked}
        has_changes = prev_checked != curr_checked

    # Parse debounce time if present
    debounce_until = None
    debounce_match = DEBOUNCE_PATTERN.search(comment_body)
    if debounce_match:
        seconds = int(debounce_match.group(1))
        debounce_until = datetime.now(UTC) + timedelta(seconds=seconds)

    return HitlCheckboxState(
        options=options,
        has_changes=has_changes,
        debounce_until=debounce_until,
        raw_comment=comment_body,
    )


def _label_to_option_id(label: str, category: HitlDecisionCategory) -> str:
    """Map a label to an option ID based on keywords."""
    label_lower = label.lower()

    # Guidance mappings
    if category == HitlDecisionCategory.GUIDANCE:
        if "context" in label_lower or "requirements" in label_lower:
            return HitlOptionId.PROVIDE_CONTEXT
        if "criteria" in label_lower or "adjust" in label_lower:
            return HitlOptionId.ADJUST_CRITERIA
        if "break" in label_lower or "sub-task" in label_lower or "subtask" in label_lower:
            return HitlOptionId.BREAK_INTO_SUBTASKS

    # Override mappings
    if category == HitlDecisionCategory.OVERRIDE:
        if "complete" in label_lower and "mark" in label_lower:
            return HitlOptionId.MARK_COMPLETE
        if "skip" in label_lower:
            return HitlOptionId.SKIP_TASKS
        if "cancel" in label_lower:
            return HitlOptionId.CANCEL_PIPELINE

    # Manual mappings
    if category == HitlDecisionCategory.MANUAL:
        if "manually" in label_lower:
            return HitlOptionId.COMPLETE_MANUALLY
        if "assign" in label_lower or "reassign" in label_lower:
            return HitlOptionId.REASSIGN

    # Default to a sanitized version of the label
    return label_lower.replace(" ", "_").replace("-", "_")[:50]


def update_comment_with_countdown(
    original_comment: str,
    seconds_remaining: int,
) -> str:
    """
    Update a comment to show the current countdown status.

    Args:
        original_comment: The original comment body
        seconds_remaining: Seconds remaining in countdown

    Returns:
        Updated comment body with countdown
    """
    # Find and replace the debounce notice
    new_notice = generate_debounce_notice(seconds_remaining)

    # Pattern to match the debounce notice block
    debounce_block_pattern = re.compile(
        r"> ⏳ \*\*Debounce active:\*\*.*?(?=\n\n---|\n---|\Z)",
        re.DOTALL,
    )

    finalized_pattern = re.compile(r"> ✅ \*\*Decision finalized\.\*\*.*")

    # Try to replace existing notice
    if debounce_block_pattern.search(original_comment):
        return debounce_block_pattern.sub(new_notice, original_comment)
    elif finalized_pattern.search(original_comment):
        return finalized_pattern.sub(new_notice, original_comment)

    # If no existing notice, add before the final separator
    return original_comment.rstrip() + "\n\n" + new_notice


def calculate_debounce_remaining(debounce_until: datetime | None) -> int:
    """
    Calculate seconds remaining in debounce period.

    Args:
        debounce_until: The debounce expiration time

    Returns:
        Seconds remaining (0 if expired or no debounce)
    """
    if debounce_until is None:
        return 0

    now = datetime.now(UTC)
    if now >= debounce_until:
        return 0

    return int((debounce_until - now).total_seconds())


def should_process_decision(state: HitlCheckboxState) -> bool:
    """
    Check if a decision should be processed based on debounce.

    Args:
        state: The current checkbox state

    Returns:
        True if debounce has expired and there are checked options
    """
    if not state.get_checked_options():
        return False

    remaining = calculate_debounce_remaining(state.debounce_until)
    return remaining == 0


def start_debounce(
    state: HitlCheckboxState,
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
) -> HitlCheckboxState:
    """
    Start or reset the debounce timer for a state.

    Args:
        state: The current checkbox state
        debounce_seconds: Debounce period in seconds

    Returns:
        Updated state with new debounce_until
    """
    return HitlCheckboxState(
        options=state.options,
        has_changes=state.has_changes,
        debounce_until=datetime.now(UTC) + timedelta(seconds=debounce_seconds),
        raw_comment=state.raw_comment,
    )
