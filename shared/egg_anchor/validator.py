"""Schema and size budget validation for agent anchors."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .constants import (
    ANCHOR_HARD_LIMIT_BYTES,
    ANCHOR_SOFT_LIMIT_BYTES,
    ANCHOR_TEAM_HARD_LIMIT_BYTES,
    ANCHOR_TEAM_SOFT_LIMIT_BYTES,
)
from .models import AgentAnchor

logger = logging.getLogger(__name__)

# Path to the JSON Schema file (shared/egg_anchor/validator.py -> repo_root/.egg/schemas/)
_SCHEMA_PATH = Path(__file__).parent.parent.parent / ".egg" / "schemas" / "agent-anchor.schema.json"


def _load_schema() -> dict[str, Any] | None:
    """Load the JSON Schema for anchor validation."""
    import os

    candidates = [_SCHEMA_PATH, Path("/app/.egg/schemas/agent-anchor.schema.json")]
    repo_path = os.environ.get("EGG_REPO_PATH")
    if repo_path:
        candidates.insert(0, Path(repo_path) / ".egg" / "schemas" / "agent-anchor.schema.json")

    for path in candidates:
        if path.exists():
            return json.loads(path.read_text())
    return None


def validate_anchor(anchor: AgentAnchor | dict[str, Any]) -> list[str]:
    """Validate an anchor against the JSON Schema.

    Returns a list of validation errors (empty if valid).
    """
    if isinstance(anchor, AgentAnchor):
        data = anchor.to_dict()
    else:
        data = anchor

    errors: list[str] = []

    try:
        import jsonschema

        schema = _load_schema()
        if schema:
            validator = jsonschema.Draft202012Validator(schema)
            for error in validator.iter_errors(data):
                errors.append(f"{error.json_path}: {error.message}")
        else:
            logger.warning("JSON Schema not found, skipping schema validation")
    except ImportError:
        logger.warning("jsonschema not installed, skipping schema validation")

    return errors


class SizeBudgetResult:
    """Result of a size budget check."""

    def __init__(
        self,
        within_budget: bool,
        size_bytes: int,
        soft_limit: int,
        hard_limit: int,
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        self.within_budget = within_budget
        self.size_bytes = size_bytes
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.warnings = warnings or []
        self.errors = errors or []


def check_size_budget(
    anchor: AgentAnchor | dict[str, Any],
    is_team: bool = False,
) -> SizeBudgetResult:
    """Check if an anchor is within its size budget.

    Args:
        anchor: The anchor to check.
        is_team: If True, use team size limits instead of individual.

    Returns:
        SizeBudgetResult with budget status, warnings, and errors.
    """
    if isinstance(anchor, AgentAnchor):
        data = anchor.to_dict()
    else:
        data = anchor

    serialized = json.dumps(data, separators=(",", ":"))
    size_bytes = len(serialized.encode("utf-8"))

    soft_limit = ANCHOR_TEAM_SOFT_LIMIT_BYTES if is_team else ANCHOR_SOFT_LIMIT_BYTES
    hard_limit = ANCHOR_TEAM_HARD_LIMIT_BYTES if is_team else ANCHOR_HARD_LIMIT_BYTES

    warnings: list[str] = []
    errors: list[str] = []

    if size_bytes > hard_limit:
        errors.append(f"Anchor size ({size_bytes} bytes) exceeds hard limit ({hard_limit} bytes)")
        return SizeBudgetResult(
            within_budget=False,
            size_bytes=size_bytes,
            soft_limit=soft_limit,
            hard_limit=hard_limit,
            errors=errors,
        )

    if size_bytes > soft_limit:
        warnings.append(f"Anchor size ({size_bytes} bytes) exceeds soft limit ({soft_limit} bytes)")

    return SizeBudgetResult(
        within_budget=True,
        size_bytes=size_bytes,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        warnings=warnings,
    )
