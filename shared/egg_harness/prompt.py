"""System prompt assembly for the egg harness.

Loads CLAUDE.md files from user and project directories,
replicating the rule-merging behavior of Claude Code.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Default locations for CLAUDE.md files
_USER_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"


def load_claude_md(
    *,
    project_dir: str | None = None,
    user_file: str | Path | None = None,
    extra_rules: str | None = None,
) -> str:
    """Load and merge CLAUDE.md files into a system prompt.

    Loads from (in order):
    1. User-level ~/.claude/CLAUDE.md
    2. Project-level CLAUDE.md (in project_dir)
    3. Extra rules (e.g., injected system prompt)

    All sections are concatenated with clear headers.

    Args:
        project_dir: Path to the project directory (for project CLAUDE.md).
        user_file: Override path for user CLAUDE.md.
        extra_rules: Additional rules to append.

    Returns:
        Combined system prompt string.
    """
    sections: list[str] = []

    # 1. User-level CLAUDE.md
    user_path = Path(user_file) if user_file else _USER_CLAUDE_MD
    user_content = _read_file(user_path)
    if user_content:
        sections.append(user_content)

    # 2. Project-level CLAUDE.md
    if project_dir:
        project_path = Path(project_dir) / "CLAUDE.md"
        project_content = _read_file(project_path)
        if project_content:
            sections.append(project_content)

    # 3. Extra rules
    if extra_rules:
        sections.append(extra_rules)

    return "\n\n---\n\n".join(sections)


def _read_file(path: Path) -> str | None:
    """Read a file, returning None if it doesn't exist."""
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                logger.debug(f"Loaded rules from {path} ({len(content)} chars)")
                return content
    except Exception as e:
        logger.warning(f"Failed to read {path}: {e}")
    return None
