"""Progress/env subcommands (env/progress emit/progress query).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import os
import sys
from typing import Any
from urllib.parse import urlencode

from egg_lib import orch_cli as _pkg

from ._common import _render_handler_error
from ._http import (
    get_gateway_url,
    get_orchestrator_url,
    get_session_token,
    print_json,
    require_pipeline_id,
)


def cmd_env(args: argparse.Namespace) -> int:
    """Show current orchestrator environment variables."""
    env_vars = [
        ("EGG_ORCHESTRATOR_URL", get_orchestrator_url()),
        ("EGG_ORCHESTRATOR_MODE", os.environ.get("EGG_ORCHESTRATOR_MODE", "(not set)")),
        ("EGG_PIPELINE_ID", os.environ.get("EGG_PIPELINE_ID", "(not set)")),
        ("EGG_AGENT_ROLE", os.environ.get("EGG_AGENT_ROLE", "(not set)")),
        ("EGG_ISSUE_NUMBER", os.environ.get("EGG_ISSUE_NUMBER", "(not set)")),
        ("GATEWAY_URL", get_gateway_url()),
        ("EGG_SESSION_TOKEN", "(set)" if get_session_token() else "(not set)"),
    ]

    if args.json:
        env_dict = {}
        for name, value in env_vars:
            env_dict[name] = value
        print_json(env_dict)
        return 0

    for name, value in env_vars:
        print(f"  {name}={value}")
    return 0


# ---------------------------------------------------------------------------
# Progress commands (structured progress tracking)
# ---------------------------------------------------------------------------


def cmd_progress_emit(args: argparse.Namespace) -> int:
    """Emit a structured progress event.

    Delegates to :func:`egg_agent_tools.handlers.progress.progress_emit`
    so the MCP ``mcp__progress__emit`` tool and the CLI share one
    handler.  Stdout / exit-code parity preserved.
    """
    from egg_agent_tools.handlers import progress as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = args.role or _pkg.get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        sys.exit(1)

    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "step": args.step,
        "state": args.state,
    }
    if args.detail:
        req["detail"] = args.detail
    if args.blocker:
        req["blocker"] = args.blocker

    try:
        resp = _handlers.progress_emit(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    if args.json:
        print_json(resp.get("signal", {}))
        return 0

    event_id = resp.get("event_id") or "unknown"
    print(f"Progress emitted: {event_id} [{args.state}] {args.step}")
    return 0


def cmd_progress_query(args: argparse.Namespace) -> int:
    """Query structured progress events."""
    pid = require_pipeline_id(args)

    params: dict[str, str] = {}
    if args.agent:
        params["agent_role"] = args.agent
    if args.since:
        params["since"] = args.since
    if args.limit:
        params["limit"] = str(args.limit)

    endpoint = f"/api/v1/pipelines/{pid}/progress"
    if params:
        endpoint += "?" + urlencode(params)

    result = _pkg.orch_request(endpoint)

    if args.json:
        print_json(result)
        return 0

    events = result.get("data", {}).get("events", [])
    if not events:
        print("No progress events.")
        return 0

    for ev in events:
        ts = ev.get("timestamp", "")[:19]
        role = ev.get("agent_role", "?")
        state = ev.get("state", "?")
        step = ev.get("step", "?")
        detail = ev.get("detail", "")
        print(f"  [{ts}] {role} [{state}] {step}")
        if detail:
            print(f"    {detail}")
        blocker = ev.get("blocker", "")
        if blocker:
            print(f"    BLOCKER: {blocker}")

    print(f"\n{len(events)} event(s)")
    return 0


# ---------------------------------------------------------------------------
# Health alerts command
# ---------------------------------------------------------------------------
