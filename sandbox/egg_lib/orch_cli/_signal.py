"""Signal subcommands (complete/progress/error/heartbeat/readiness).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import sys
from typing import Any

from egg_lib import orch_cli as _pkg

from ._common import _render_handler_error, _require_role
from ._http import (
    print_json,
    require_pipeline_id,
)


def cmd_signal_complete(args: argparse.Namespace) -> int:
    """Signal agent completion."""
    pid = require_pipeline_id(args)
    role = _require_role(args)
    data: dict[str, Any] = {
        "signal_type": "complete",
        "agent_role": role,
    }
    if args.commit:
        data["commit"] = args.commit
    if args.files:
        data["files_changed"] = args.files

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Signaled complete for {role}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_signal_progress(args: argparse.Namespace) -> int:
    """Signal percent-based progress update (legacy /signal endpoint).

    This is a separate code path from ``mcp__progress__emit``, which
    hits the structured-event endpoint (``/progress``, step/state).
    Kept inline for CLI parity; no MCP tool ever exposed this verb.
    """
    pid = require_pipeline_id(args)
    role = _require_role(args)
    data: dict[str, Any] = {
        "signal_type": "progress",
        "agent_role": role,
        "progress_percent": args.percent,
    }
    if args.task:
        data["current_task"] = args.task
    if args.message:
        data["message"] = args.message

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Progress: {args.percent}%")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_signal_error(args: argparse.Namespace) -> int:
    """Signal an error.

    Delegates to :func:`egg_agent_tools.handlers.progress.progress_signal_error`.
    """
    from egg_agent_tools.handlers import progress as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)
    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "error": args.error,
        "recoverable": args.recoverable,
    }
    try:
        resp = _handlers.progress_signal_error(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    print(f"Signaled error for {role}")
    return 0


def cmd_signal_heartbeat(args: argparse.Namespace) -> int:
    """Send heartbeat.

    Delegates to :func:`egg_agent_tools.handlers.progress.progress_heartbeat`.
    """
    from egg_agent_tools.handlers import progress as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)
    req: dict[str, Any] = {"pipeline_id": pid, "role": role}
    try:
        resp = _handlers.progress_heartbeat(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    print("Heartbeat sent")
    return 0


# ---------------------------------------------------------------------------
# Phase commands
# ---------------------------------------------------------------------------


def cmd_signal_readiness(args: argparse.Namespace) -> int:
    """Signal readiness state for consensus."""
    pid = require_pipeline_id(args)
    role = _require_role(args)
    data: dict[str, Any] = {
        "signal_type": "readiness",
        "agent_role": role,
        "state": args.state,
    }
    if args.reason:
        data["reason"] = args.reason

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        consensus = result.get("data", {}).get("consensus", {})
        print(f"Readiness: {role} -> {args.state}")
        if consensus.get("is_complete"):
            print("Consensus reached!")
        else:
            blocking = consensus.get("blocking_agents", [])
            if blocking:
                print(f"Waiting on: {', '.join(blocking)}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Consensus commands (BRC protocol)
# ---------------------------------------------------------------------------
