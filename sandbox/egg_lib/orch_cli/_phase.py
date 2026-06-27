"""Phase subcommands (get/advance/start/complete/get-context).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import sys
from typing import Any

from egg_lib import orch_cli as _pkg

from ._common import _render_handler_error
from ._http import (
    get_pipeline_id_from_env,
    print_json,
    require_pipeline_id,
    validate_id,
)


def cmd_phase_get(args: argparse.Namespace) -> int:
    """Get current phase for a pipeline."""
    pid = require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/phase")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    print(f"Phase:    {data.get('phase', data.get('current_phase', 'unknown'))}")
    print(f"Status:   {data.get('status', 'unknown')}")
    if data.get("started_at"):
        print(f"Started:  {data.get('started_at')}")
    return 0


def cmd_phase_advance(args: argparse.Namespace) -> int:
    """Advance pipeline to next phase."""
    pid = require_pipeline_id(args)
    data: dict[str, Any] = {"target_phase": args.target_phase}
    if args.reason:
        data["reason"] = args.reason

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/phase", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(result.get("message", "Phase advanced"))
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_phase_start(args: argparse.Namespace) -> int:
    """Start the current phase."""
    pid = require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/phase/start", method="POST", data={})

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(result.get("message", "Phase started"))
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_phase_complete(args: argparse.Namespace) -> int:
    """Complete the current phase."""
    pid = require_pipeline_id(args)
    data: dict[str, Any] = {}
    if args.reason:
        data["reason"] = args.reason

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/phase/complete", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        msg = result.get("message", "Phase completed")
        phase_data = result.get("data", {})
        next_phase = phase_data.get("next_phase")
        if next_phase:
            msg += f"\nRun: egg-orch phase advance --target-phase {next_phase}"
        print(msg)
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Decision commands
# ---------------------------------------------------------------------------


def cmd_phase_get_context(args: argparse.Namespace) -> int:
    """Bundle phase context (pipeline_id, phase, role, tasks, artifacts).

    Verb-level alias for ``mcp__phase__get_context``. Returns the same
    JSON shape the MCP tool produces so wrapper bash can call this
    interchangeably. Defaults pull from ``$EGG_PIPELINE_ID`` /
    ``$EGG_AGENT_ROLE`` / ``$EGG_PHASE`` env vars.
    """
    from egg_agent_tools.handlers import phase as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    req: dict[str, Any] = {}
    pid = getattr(args, "pipeline_id", None) or get_pipeline_id_from_env()
    if pid:
        req["pipeline_id"] = validate_id(pid, "pipeline_id")
    if getattr(args, "phase", None):
        req["phase"] = args.phase
    if getattr(args, "role", None):
        req["role"] = args.role
    if getattr(args, "no_artifacts", False):
        req["include_artifacts"] = False

    try:
        resp = _handlers.phase_get_context(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    print_json(resp)
    return 0


# ---------------------------------------------------------------------------
# Environment info
# ---------------------------------------------------------------------------
