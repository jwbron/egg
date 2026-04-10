"""System prompt assembly for egg environments.

Replicates the exact CLAUDE.md rule-merging from sandbox/agent-config/rules/
as specified by HITL decision #9.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from egg_harness.prompt import load_claude_md

logger = logging.getLogger(__name__)

# Standard rule file locations in the sandbox
_RULES_DIR = Path("/home/egg/.claude")


def build_egg_system_prompt(
    *,
    project_dir: str | None = None,
    system_prompt_override: str | None = None,
    extra_rules: str | None = None,
) -> str:
    """Build the full system prompt for an egg agent.

    Loads CLAUDE.md from standard locations and appends any overrides.

    Args:
        project_dir: Project directory for project-level CLAUDE.md.
        system_prompt_override: If provided, prepend to the system prompt.
        extra_rules: Additional rules to append.

    Returns:
        Combined system prompt string.
    """
    parts: list[str] = []

    # System prompt override goes first (e.g., from --system-prompt CLI flag)
    if system_prompt_override:
        parts.append(system_prompt_override)

    # Load CLAUDE.md files
    claude_md = load_claude_md(
        project_dir=project_dir or os.environ.get("EGG_REPO_PATH"),
        extra_rules=extra_rules,
    )
    if claude_md:
        parts.append(claude_md)

    return "\n\n---\n\n".join(parts)
