"""Drift test: every MCP tool with a declared CLI counterpart must be
reachable from the CLI parser and must share a handler with the cmd_*
shim.

For every entry in ``TOOL_REGISTRY`` with ``cli_command`` set:

1. The CLI subparser identified by ``cli_command`` exists in the
   corresponding ``create_parser()`` tree
   (egg-orch / egg-contract).
2. The cmd_* function registered on that subparser (via
   ``set_defaults(func=cmd_*)``) delegates to the same handler the MCP
   tool wraps (module-level ``is`` identity).

Tools with ``cli_command=None`` (the five capability-gap verbs:
``check_hitl_answers``, ``brc_get_state``, ``brc_list_blocking``,
``phase_get_context``, ``phase_get_assigned_tasks``) are skipped — they
have no CLI counterpart by design.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.tools import TOOL_REGISTRY  # noqa: E402
from egg_lib import (
    contract_cli,  # noqa: E402
    orch_cli,  # noqa: E402
)

PARSERS = {
    "egg-contract": contract_cli.create_parser(),
    "egg-orch": orch_cli.create_parser(),
}

# Map CLI subcommand → source module hosting its cmd_* function.
SOURCE_MODULE = {
    "egg-contract": contract_cli,
    "egg-orch": orch_cli,
}


def _resolve_subparser(
    parser: argparse.ArgumentParser, path: tuple[str, ...]
) -> argparse.ArgumentParser | None:
    """Walk the parser tree along ``path``; return the leaf subparser.

    Returns None if any hop is missing.
    """
    current = parser
    for hop in path:
        subparsers_actions = [
            a for a in current._actions if isinstance(a, argparse._SubParsersAction)
        ]
        if not subparsers_actions:
            return None
        choices = subparsers_actions[0].choices
        if hop not in choices:
            return None
        current = choices[hop]
    return current


def _extract_handler_reference(module, cmd_func_name: str) -> object | None:
    """Return the handler module.<name> imported by ``cmd_func_name``.

    We parse the cmd_* function's source to find the
    ``from egg_agent_tools.handlers import <ns> as _handlers`` import
    and the ``_handlers.<fn>(req)`` call, then resolve the reference
    dynamically.  A purely static approach — no execution of the CLI.
    """
    fn = getattr(module, cmd_func_name, None)
    if fn is None:
        return None
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    tree = ast.parse(src)

    ns_alias = None  # alias name → namespace module (e.g. '_handlers' -> 'brc')
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "egg_agent_tools.handlers":
            # pattern: `from egg_agent_tools.handlers import brc as _handlers`
            for alias in node.names:
                ns_alias = (alias.asname or alias.name, alias.name)
                break
            break

    if ns_alias is None:
        return None

    alias_name, namespace = ns_alias
    # Find `_handlers.<fn>(...)` call.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == alias_name
        ):
            handler_attr = node.func.attr
            import importlib

            handlers_module = importlib.import_module(f"egg_agent_tools.handlers.{namespace}")
            return getattr(handlers_module, handler_attr, None)
    return None


CLI_BACKED_TOOLS = [
    (name, reg) for name, reg in TOOL_REGISTRY.items() if reg.cli_command is not None
]


@pytest.mark.parametrize(
    "tool_name,registration", CLI_BACKED_TOOLS, ids=[n for n, _ in CLI_BACKED_TOOLS]
)
def test_cli_subparser_exists(tool_name: str, registration) -> None:
    cli = registration.cli_command
    assert cli is not None
    binary, *path = cli
    assert binary in PARSERS, f"Unknown CLI binary '{binary}' for tool {tool_name}"
    leaf = _resolve_subparser(PARSERS[binary], tuple(path))
    assert leaf is not None, (
        f"Tool {tool_name} declares CLI {' '.join(cli)} but the subparser is "
        f"missing from {binary} create_parser()"
    )


@pytest.mark.parametrize(
    "tool_name,registration", CLI_BACKED_TOOLS, ids=[n for n, _ in CLI_BACKED_TOOLS]
)
def test_cli_shim_delegates_to_tool_handler(tool_name: str, registration) -> None:
    """The cmd_* function bound to the subparser must delegate to the
    same handler the MCP wrapper invokes."""
    cli = registration.cli_command
    binary, *path = cli
    leaf = _resolve_subparser(PARSERS[binary], tuple(path))
    assert leaf is not None
    # argparse stores the handler function under defaults['func'].
    # Some leaves may also expose it on an `_defaults` attribute.
    func = leaf._defaults.get("func")
    assert func is not None, f"Subparser {' '.join(cli)} has no set_defaults(func=...)"
    # Resolve the handler the cmd_* function delegates to.
    handler_fn = _extract_handler_reference(SOURCE_MODULE[binary], func.__name__)
    assert handler_fn is not None, (
        f"Could not statically resolve the handler delegated to by {func.__name__} "
        f"in {binary}; drift test cannot verify tool {tool_name}."
    )
    assert handler_fn is registration.handler, (
        f"Drift: tool {tool_name} wraps {registration.handler} but CLI shim "
        f"{func.__name__} delegates to {handler_fn}"
    )


def test_cli_less_tools_are_documented_gaps():
    """Tools without CLI counterparts are the planned capability-gap
    verbs (brc_get_state, brc_list_blocking, check_hitl_answers,
    phase_get_context, phase_get_assigned_tasks).  If this set changes,
    the drift test must be updated to match the design intent."""
    expected_gaps = {
        "mcp__sdlc__check_hitl_answers",
        "mcp__brc__get_state",
        "mcp__brc__list_blocking",
        "mcp__phase__get_context",
        "mcp__phase__get_assigned_tasks",
    }
    actual_gaps = {name for name, reg in TOOL_REGISTRY.items() if reg.cli_command is None}
    assert actual_gaps == expected_gaps, (
        "CLI-less tool set drifted from the iteration-1 design.  "
        "Either add a CLI counterpart or update this test."
    )
