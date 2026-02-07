"""
Human-in-the-Loop (HITL) checkbox handling.

Provides utilities for:
- Generating markdown checkbox blocks for decisions
- Parsing checkbox state from comment body
- Handling debounce timing for checkbox changes
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any

from .models import Contract, Decision, DecisionOption, DecisionType


@dataclass
class CheckboxState:
    """State of a checkbox option."""

    id: str
    label: str
    checked: bool
    description: str | None = None


@dataclass
class ParsedCheckboxBlock:
    """Parsed checkbox block from a comment."""

    question: str
    options: list[CheckboxState]
    checked_option: str | None = None

    @property
    def has_selection(self) -> bool:
        """Check if any option is selected."""
        return self.checked_option is not None


# Debounce period in seconds
DEBOUNCE_SECONDS = 30


def generate_checkbox_block(
    question: str,
    options: list[dict[str, str]] | list[DecisionOption],
    *,
    header: str | None = None,
) -> str:
    """
    Generate a markdown checkbox block for a decision.

    Args:
        question: The question to ask
        options: List of options with 'id', 'label', and optional 'description'
        header: Optional header text

    Returns:
        Markdown formatted checkbox block
    """
    lines = []

    if header:
        lines.append(f"### {header}")
        lines.append("")

    lines.append(question)
    lines.append("")
    lines.append("Please select one option:")
    lines.append("")

    for opt in options:
        if isinstance(opt, DecisionOption):
            label = opt.label
            desc = opt.description
        else:
            label = opt.get("label", "")
            desc = opt.get("description")

        if desc:
            lines.append(f"- [ ] **{label}** - {desc}")
        else:
            lines.append(f"- [ ] **{label}**")

    return "\n".join(lines)


def generate_phase_approval_block(phase_name: str) -> str:
    """
    Generate a checkbox block for phase approval.

    Args:
        phase_name: Name of the phase to approve

    Returns:
        Markdown formatted checkbox block
    """
    return generate_checkbox_block(
        f"Approve {phase_name} phase?",
        [
            {"label": "Approve", "description": f"Proceed to next phase"},
            {"label": "Request changes", "description": "I'll provide feedback below"},
            {"label": "Reject", "description": "This approach won't work"},
        ],
        header=f"Decision Required: Approve {phase_name.title()}?",
    )


def generate_escalation_block(task_id: str, reason: str) -> str:
    """
    Generate a checkbox block for escalation response.

    Args:
        task_id: ID of the stuck task
        reason: Reason for escalation

    Returns:
        Markdown formatted checkbox block
    """
    return generate_checkbox_block(
        f"Task {task_id} has been escalated: {reason}",
        [
            {"label": "Continue", "description": "Reset cycle count and retry"},
            {"label": "Skip task", "description": "Mark as failed and continue"},
            {"label": "Abort", "description": "Stop the pipeline"},
        ],
        header="Escalation: Human Decision Required",
    )


def parse_checkbox_block(comment_body: str) -> list[ParsedCheckboxBlock]:
    """
    Parse checkbox blocks from a comment body.

    Args:
        comment_body: Full markdown comment body

    Returns:
        List of parsed checkbox blocks
    """
    blocks = []

    # Pattern for checkbox lines
    # Matches: - [ ] **Label** or - [x] **Label** - Description
    checkbox_pattern = r"- \[([ x])\] \*\*([^*]+)\*\*(?:\s*-\s*(.+))?"

    # Find all checkbox groups (separated by questions or headers)
    lines = comment_body.split("\n")
    current_block: ParsedCheckboxBlock | None = None

    for i, line in enumerate(lines):
        # Check for header or question (potential start of block)
        if line.startswith("###"):
            if current_block and current_block.options:
                blocks.append(current_block)
            question = line.lstrip("#").strip()
            current_block = ParsedCheckboxBlock(question=question, options=[])

        # Check for checkbox line
        match = re.match(checkbox_pattern, line.strip())
        if match:
            checked = match.group(1) == "x"
            label = match.group(2).strip()
            description = match.group(3).strip() if match.group(3) else None

            option = CheckboxState(
                id=label.lower().replace(" ", "-"),
                label=label,
                checked=checked,
                description=description,
            )

            if current_block is None:
                # Look back for a question
                question = ""
                for j in range(i - 1, max(i - 5, -1), -1):
                    if lines[j].strip() and not lines[j].startswith("-"):
                        question = lines[j].strip()
                        break
                current_block = ParsedCheckboxBlock(question=question, options=[])

            current_block.options.append(option)

            if checked:
                current_block.checked_option = label

    # Add last block
    if current_block and current_block.options:
        blocks.append(current_block)

    return blocks


def get_checked_option(comment_body: str) -> str | None:
    """
    Get the first checked option from a comment.

    Args:
        comment_body: Comment body to parse

    Returns:
        Label of checked option or None
    """
    blocks = parse_checkbox_block(comment_body)
    for block in blocks:
        if block.checked_option:
            return block.checked_option
    return None


def is_within_debounce(decision: Decision) -> bool:
    """
    Check if a decision is within its debounce period.

    Args:
        decision: Decision to check

    Returns:
        True if still in debounce period
    """
    if decision.debounce_until is None:
        return False
    return datetime.now(UTC) < decision.debounce_until


def set_debounce(decision: Decision) -> datetime:
    """
    Set the debounce expiry time for a decision.

    Args:
        decision: Decision to update

    Returns:
        Debounce expiry time
    """
    expiry = datetime.now(UTC) + timedelta(seconds=DEBOUNCE_SECONDS)
    decision.debounce_until = expiry
    return expiry


def resolve_decision_from_checkbox(
    decision: Decision,
    checked_option: str,
    resolved_by: str,
) -> None:
    """
    Resolve a decision based on a checkbox selection.

    Args:
        decision: Decision to resolve
        checked_option: The selected option label
        resolved_by: Who made the selection (GitHub username)
    """
    decision.resolved = True
    decision.resolution = checked_option
    decision.resolved_by = resolved_by
    decision.resolved_at = datetime.now(UTC)
    decision.debounce_until = None


def find_pending_decision(contract: Contract) -> Decision | None:
    """
    Find the first unresolved decision in a contract.

    Args:
        contract: Contract to search

    Returns:
        First unresolved decision or None
    """
    if not contract.decisions:
        return None

    for decision in contract.decisions:
        if not decision.resolved:
            return decision

    return None


def create_hitl_decision(
    contract: Contract,
    question: str,
    options: list[dict[str, str]] | None = None,
) -> Decision:
    """
    Create a new HITL decision in the contract.

    Args:
        contract: Contract to update
        question: Question to ask
        options: Optional list of options

    Returns:
        Created decision
    """
    decision_id = contract.next_decision_id()

    decision_options = None
    if options:
        decision_options = [
            DecisionOption(
                id=opt.get("id", f"opt-{i}"),
                label=opt.get("label", ""),
                description=opt.get("description"),
            )
            for i, opt in enumerate(options)
        ]

    decision = Decision(
        id=decision_id,
        question=question,
        type=DecisionType.HITL,
        options=decision_options,
        resolved=False,
    )

    if contract.decisions is None:
        contract.decisions = []
    contract.decisions.append(decision)

    return decision
