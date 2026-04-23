"""argparse → JSON-schema helpers for egg_agent_tools.

The SDK's ``@tool`` decorator accepts either a TypedDict-style Python
type, a ``{name: type}`` dict, or a full JSON schema.  For tools that
already have a shell CLI counterpart we derive a JSON schema skeleton
from the argparse subparser definition and allow callers to merge
per-tool overrides (descriptions, ``enum`` constraints, ``required``
shape, etc.).

The goal is not a 1:1 argparse→JSON schema translator — it is a best-
effort helper that catches the common parameter shape (str/int/bool,
required flags, nargs='*'/'+') and lets tool authors fill in the gaps.
"""

from __future__ import annotations

import argparse
import copy
from typing import Any


def _argparse_type_to_json(
    action: argparse.Action,
) -> tuple[str, dict[str, Any]]:
    """Map an argparse action's Python type to a JSON-schema fragment.

    Returns a (``json_type``, ``extra``) tuple where ``extra`` carries
    supplementary keywords like ``items`` or ``enum``.
    """
    py_type = getattr(action, "type", None)
    extra: dict[str, Any] = {}

    # store_true / store_false → boolean
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "boolean", extra

    # nargs='*' / '+' / int → array
    nargs = getattr(action, "nargs", None)
    if nargs in ("*", "+") or (isinstance(nargs, int) and nargs > 1):
        item_type = "string"
        if py_type is int:
            item_type = "integer"
        elif py_type is float:
            item_type = "number"
        extra["items"] = {"type": item_type}
        if nargs == "+":
            extra["minItems"] = 1
        return "array", extra

    if py_type is int:
        return "integer", extra
    if py_type is float:
        return "number", extra
    if py_type is bool:
        return "boolean", extra

    # choices → enum
    if action.choices:
        extra["enum"] = list(action.choices)
    return "string", extra


def _option_name(action: argparse.Action) -> str | None:
    """Return the long form (``--foo``) of an argparse option action."""
    for s in action.option_strings:
        if s.startswith("--"):
            return s.lstrip("-").replace("-", "_")
    if action.option_strings:
        return action.option_strings[0].lstrip("-").replace("-", "_")
    return action.dest


def derive_schema_from_argparse(
    subparser: argparse.ArgumentParser,
    *,
    drop: set[str] | None = None,
) -> dict[str, Any]:
    """Convert an argparse subparser definition to a JSON-schema dict.

    Args:
        subparser: The subparser (returned by ``subparsers.add_parser``).
        drop: Set of arg names to omit (useful for CLI-only flags like
            ``--json`` that do not apply to MCP tools).

    Returns:
        ``{ "type": "object", "properties": {...}, "required": [...] }``.
    """
    drop = drop or set()
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for action in subparser._actions:
        # Skip help and the trailing ``func`` default.
        if action.dest in ("help", "func"):
            continue
        name = _option_name(action) or action.dest
        if name in drop:
            continue

        json_type, extra = _argparse_type_to_json(action)
        prop: dict[str, Any] = {"type": json_type}
        prop.update(extra)
        if action.help:
            prop["description"] = action.help
        if action.default is not None and action.default is not argparse.SUPPRESS:
            # Do not leak argparse-internal defaults like SUPPRESS or
            # mutable lists that would confuse the SDK schema.
            if isinstance(action.default, (str, int, float, bool)):
                prop["default"] = action.default

        properties[name] = prop
        if action.required:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def build_tool_schema(
    base_schema: dict[str, Any] | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a derived schema with per-tool overrides.

    Override semantics:

    - ``overrides['properties'][<name>]`` replaces the entire property
      definition for ``<name>`` (so callers can set enums, constraints,
      or descriptions without having to restate the whole schema).
    - ``overrides['required']`` *replaces* the derived list (callers
      explicitly control what is required).
    - Other top-level keys (``description``, ``additionalProperties``, …)
      are shallow-merged.

    The derived schema is never mutated.
    """
    merged = copy.deepcopy(base_schema or {"type": "object", "properties": {}})
    if not overrides:
        return merged

    for key, value in overrides.items():
        if key == "properties":
            existing = merged.setdefault("properties", {})
            existing.update(copy.deepcopy(value))
        elif key == "required":
            merged["required"] = list(value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
