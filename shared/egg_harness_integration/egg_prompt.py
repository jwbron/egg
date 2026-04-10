"""Egg system-prompt assembly and settings loader.

Replicates the CLAUDE.md rule-merging behaviour from
``sandbox/entrypoint.py`` so that headless harness sessions receive the
same system instructions as interactive Claude Code sessions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Canonical rule file ordering -- must match setup_agent_rules() in
# sandbox/entrypoint.py.
_RULES_ORDER: list[str] = [
    "mission.md",
    "environment.md",
    "code-standards.md",
    "test-workflow.md",
    "pr-descriptions.md",
    "orchestrator.md",
    "contract.md",
    "checkpoint.md",
]

# Separator between rule sections (matches CLAUDE.md convention).
_SECTION_SEPARATOR: str = "\n\n---\n\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_egg_system_prompt(
    rules_dir: str | Path = "/opt/claude-rules",
    project_claude_md: str | Path | None = None,
) -> str:
    """Assemble a system prompt from egg rule files and an optional project CLAUDE.md.

    Reads rule files from *rules_dir* in the canonical order defined by
    ``setup_agent_rules()`` in ``sandbox/entrypoint.py``.  Missing files
    are silently skipped.  Sections are joined with the standard
    ``"\\n\\n---\\n\\n"`` separator.

    If *project_claude_md* is provided and the file exists, its content is
    appended as a final section.

    Args:
        rules_dir: Directory containing rule markdown files.
        project_claude_md: Optional path to a project-level CLAUDE.md file.

    Returns:
        The assembled system prompt string.  May be empty if no rule files
        or project CLAUDE.md are found.
    """
    rules_path = Path(rules_dir)
    content_parts: list[str] = []

    for rule_file in _RULES_ORDER:
        rule_path = rules_path / rule_file
        if rule_path.exists():
            try:
                text = rule_path.read_text(encoding="utf-8")
                if text.strip():
                    content_parts.append(text)
            except OSError:
                logger.warning("Failed to read rule file: %s", rule_path)

    # Append project CLAUDE.md if provided and present on disk.
    if project_claude_md is not None:
        project_path = Path(project_claude_md)
        if project_path.exists():
            try:
                text = project_path.read_text(encoding="utf-8")
                if text.strip():
                    content_parts.append(text)
            except OSError:
                logger.warning(
                    "Failed to read project CLAUDE.md: %s", project_path
                )

    return _SECTION_SEPARATOR.join(content_parts)


def load_settings_json(settings_path: str | Path) -> dict[str, Any]:
    """Load a settings.json file, returning an empty dict if absent.

    Used by the harness factory to read default configuration values
    (e.g. model, timeout overrides) from the Claude Code settings file.

    Args:
        settings_path: Path to a ``settings.json`` file.

    Returns:
        The parsed JSON content as a dict, or ``{}`` if the file does
        not exist or cannot be parsed.
    """
    path = Path(settings_path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        logger.warning("settings.json is not a JSON object: %s", path)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load settings.json (%s): %s", path, exc)
        return {}
