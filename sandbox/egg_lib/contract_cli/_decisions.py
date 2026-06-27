"""HITL decision-id validation and markdown formatting.

Extracted verbatim from the monolithic ``contract_cli.py`` (#3312,
slice-1). No behaviour change.
"""

import re
from typing import Any


def validate_decision_id(decision_id: str) -> None:
    """Validate decision_id matches the expected format.

    The workflow regex expects [a-z0-9-]+ and the ID must end at a valid
    boundary (space or '>') in the HTML comment.

    Args:
        decision_id: The decision ID to validate

    Raises:
        ValueError: If decision_id contains invalid characters
    """
    if not re.match(r"^[a-z0-9-]+$", decision_id):
        raise ValueError(
            f"Invalid decision_id '{decision_id}': must contain only "
            "lowercase letters, numbers, and hyphens"
        )


def format_decision_markdown(decision_id: str, question: str, options: list[dict[str, Any]]) -> str:
    """Format a HITL decision as markdown with proper markers.

    The output format matches what the HITL decision handler expects:
    - HTML comment marker with decision ID for detection
    - Checkbox list for options

    Args:
        decision_id: The decision ID (e.g., "decision-1"). Must match [a-z0-9-]+
        question: The decision question
        options: List of option dicts with 'label' keys

    Returns:
        Formatted markdown string ready for GitHub comment

    Raises:
        ValueError: If decision_id contains invalid characters
    """
    validate_decision_id(decision_id)

    lines = [
        f"<!-- egg-hitl-decision id={decision_id} -->",
        "",
        f"**{question}**",
        "",
    ]

    for opt in options:
        lines.append(f"- [ ] {opt['label']}")

    return "\n".join(lines)
