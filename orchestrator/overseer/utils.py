"""Shared utilities for the overseer package."""

from __future__ import annotations

import json
from typing import Any


def parse_json_or_fallback(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Try to parse *raw* as JSON; return *fallback* on failure.

    Handles both raw JSON strings and JSON embedded in markdown code fences.
    """
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError, TypeError:
        pass
    # Try extracting a JSON block from markdown fences
    if "```" in raw:
        for block in raw.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{"):
                try:
                    return json.loads(block)  # type: ignore[no-any-return]
                except json.JSONDecodeError, TypeError:
                    pass
    return fallback
