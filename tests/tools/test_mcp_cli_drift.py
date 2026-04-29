"""Drift test: every MCP tool with a declared CLI counterpart must be
reachable from the CLI parser and must share a dispatch path with the
cmd_* shim.

For every entry in ``TOOL_REGISTRY`` with ``cli_command`` set:

1. The CLI subparser identified by ``cli_command`` exists in the
   corresponding ``create_parser()`` tree
   (egg-orch / egg-contract / egg-checkpoint).
2. The cmd_* function registered on that subparser (via
   ``set_defaults(func=cmd_*)``) shares a dispatch path with the MCP
   handler.  Two patterns are accepted:

   a. **Handler-import pattern** — most iter-1 / iter-2 verbs: the
      cmd_* function imports
      ``from egg_agent_tools.handlers import <ns> as _handlers`` and
      calls ``_handlers.<fn>(req)``.  The drift test resolves that
      reference via AST and asserts it is the same handler the MCP
      tool wraps.
   b. **Shared-helper pattern** — the checkpoint verbs (iter-2
      decision-20): both the cmd_* function AND the MCP handler
      import the helpers ``collect_checkpoints`` / ``load_checkpoint``
      / ``search_checkpoints`` from ``egg_contracts.checkpoint_cli``.
      The drift test asserts the cmd_* function references the same
      helper name that the MCP handler's body references.

Tools with ``cli_command=None`` (the iter-1 capability-gap verbs plus
the iter-2 net-new capabilities: ``brc__read_peer_artifact``,
``task__mark_gap``) are skipped in the subparser/handler parity tests
— they have no CLI counterpart by design.  The
``test_cli_less_tools_are_documented_gaps`` assertion keeps that set
explicit.
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
from egg_contracts import checkpoint_cli  # noqa: E402
from egg_lib import (  # noqa: E402
    contract_cli,
    orch_cli,
)

PARSERS = {
    "egg-contract": contract_cli.create_parser(),
    "egg-orch": orch_cli.create_parser(),
    "egg-checkpoint": checkpoint_cli.create_parser(),
}

# Map CLI subcommand → source module hosting its cmd_* function.
SOURCE_MODULE = {
    "egg-contract": contract_cli,
    "egg-orch": orch_cli,
    "egg-checkpoint": checkpoint_cli,
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

    Handler-import pattern (most tools): parse the cmd_* function's
    source to find the
    ``from egg_agent_tools.handlers import <ns> as _handlers`` import
    and the ``_handlers.<fn>(req)`` call, then resolve the reference
    dynamically.  A purely static approach — no execution of the CLI.
    """
    fn = getattr(module, cmd_func_name, None)
    if fn is None:
        return None
    try:
        src = inspect.getsource(fn)
    except OSError, TypeError:
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


# Shared-helper pattern: the checkpoint verbs share one helper from
# ``shared/egg_contracts/checkpoint_cli.py`` (decision-20).  The helper
# name is the authoritative dispatch anchor; we check both the CLI
# shim and the MCP handler reference the same helper.  Mapping:
# MCP tool name → (helper_module, helper_attr).  When this map is
# consulted the generic AST walk above is skipped.
_SHARED_HELPER_DISPATCH: dict[str, tuple[str, str]] = {
    "mcp__checkpoint__list": ("egg_contracts.checkpoint_cli", "collect_checkpoints"),
    "mcp__checkpoint__show": ("egg_contracts.checkpoint_cli", "load_checkpoint"),
    "mcp__checkpoint__search": ("egg_contracts.checkpoint_cli", "search_checkpoints"),
}


def _function_references_name(fn, *, attr_name: str) -> bool:
    """Return True when ``fn``'s source references ``attr_name`` as a
    callable (direct call, attribute access, or imported name).

    Intentionally lax so it works for both
    ``from egg_contracts.checkpoint_cli import collect_checkpoints``
    (direct `collect_checkpoints(...)`) and
    ``egg_contracts.checkpoint_cli.collect_checkpoints(...)`` access
    styles.
    """
    try:
        src = inspect.getsource(fn)
    except OSError, TypeError:
        return False
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == attr_name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == attr_name:
            return True
    return False


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
    """The cmd_* function bound to the subparser must share a dispatch
    path with the handler the MCP wrapper invokes.

    Two patterns are accepted — see module docstring for details.
    """
    cli = registration.cli_command
    binary, *path = cli
    leaf = _resolve_subparser(PARSERS[binary], tuple(path))
    assert leaf is not None
    # argparse stores the handler function under defaults['func'].
    # Some leaves may also expose it on an `_defaults` attribute.
    func = leaf._defaults.get("func")
    assert func is not None, f"Subparser {' '.join(cli)} has no set_defaults(func=...)"

    # Shared-helper pattern (iter-2, decision-20): the checkpoint verbs
    # share a pure helper from ``egg_contracts.checkpoint_cli``.  Both
    # the CLI shim and the MCP handler must reference the same helper.
    if tool_name in _SHARED_HELPER_DISPATCH:
        helper_module, helper_attr = _SHARED_HELPER_DISPATCH[tool_name]
        assert _function_references_name(func, attr_name=helper_attr), (
            f"Drift: tool {tool_name} is expected to dispatch through "
            f"{helper_module}.{helper_attr}, but CLI shim {func.__name__} "
            f"never references that helper."
        )
        assert _function_references_name(registration.handler, attr_name=helper_attr), (
            f"Drift: tool {tool_name} MCP handler "
            f"{registration.handler.__module__}.{registration.handler.__name__} "
            f"does not reference the shared helper "
            f"{helper_module}.{helper_attr}."
        )
        return

    # Handler-import pattern (default): resolve the handler the cmd_*
    # function delegates to via AST walk.
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
    """Tools without CLI counterparts are the documented no-CLI
    capabilities:

    - Iter-1 capability-gap verbs: ``brc_get_state``, ``brc_list_blocking``,
      ``check_hitl_answers``, ``phase_get_context``, ``phase_get_assigned_tasks``.
    - Iter-2 net-new capabilities (#1917, decisions 4 and 8):
      ``brc_read_peer_artifact``, ``task_mark_gap``.

    The #1897 ``send_heartbeat`` primitive DOES have a CLI counterpart
    (``egg-orch message heartbeat``) and is covered by the parametrised
    subparser + delegation tests above.  The ``wait_for_event`` /
    ``wait_loop`` MCP wrappers were removed in #2211 — long-poll waits
    don't fit the SDK MCP transport's tool-call cap; agents use the
    ``egg-orch message wait`` / ``wait-loop`` Bash CLI instead.

    If this set changes, the drift test must be updated to match the
    design intent — every cli_command=None entry also needs a
    docstring rationale (decision-13), covered by
    ``tests/tools/test_rule_doc_drift.py`` assertion C.
    """
    expected_gaps = {
        # Iter-1
        "mcp__sdlc__check_hitl_answers",
        "mcp__brc__get_state",
        "mcp__brc__list_blocking",
        "mcp__phase__get_context",
        "mcp__phase__get_assigned_tasks",
        # Iter-2
        "mcp__brc__read_peer_artifact",
        "mcp__task__mark_gap",
    }
    actual_gaps = {name for name, reg in TOOL_REGISTRY.items() if reg.cli_command is None}
    assert actual_gaps == expected_gaps, (
        "CLI-less tool set drifted from the iteration-2 design.  "
        "Either add a CLI counterpart, update this test, or add a "
        "docstring rationale entry for the new no-CLI verb."
    )


def test_iter2_cli_backed_tools_land_in_expected_binaries():
    """Sanity check: the 10 iter-2 CLI-backed verbs dispatch through
    the expected CLI binary (egg-contract / egg-orch / egg-checkpoint).

    Catches a registration landing under the wrong binary (e.g. the
    checkpoint verbs defaulting to egg-contract).  Distinct from the
    generic parametrised tests above because those rely on
    CLI_BACKED_TOOLS being correct — this tethers the iter-2 entries
    explicitly.
    """
    expected = {
        "mcp__sdlc__show_contract": "egg-contract",
        "mcp__sdlc__verify_criterion": "egg-contract",
        "mcp__task__add_commit": "egg-contract",
        "mcp__task__update_notes": "egg-contract",
        "mcp__phase__complete_phase": "egg-contract",
        "mcp__progress__overseer_alert": "egg-orch",
        "mcp__progress__query_status": "egg-orch",
        "mcp__checkpoint__list": "egg-checkpoint",
        "mcp__checkpoint__show": "egg-checkpoint",
        "mcp__checkpoint__search": "egg-checkpoint",
    }
    for tool_name, binary in expected.items():
        reg = TOOL_REGISTRY[tool_name]
        assert reg.cli_command is not None, f"{tool_name} must be CLI-backed"
        assert reg.cli_command[0] == binary, (
            f"{tool_name} expected CLI binary {binary!r} got {reg.cli_command[0]!r}"
        )
