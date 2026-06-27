"""BRC inspection subcommands (next-action/get-state/list-blocking/resolve-obligation/read-peer-artifact).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import sys
from typing import Any

from egg_lib import orch_cli as _pkg

from ._common import (
    _ProseArgError,
    _render_handler_error,
    _require_role,
    _resolve_prose_arg,
)
from ._http import (
    print_json,
    require_pipeline_id,
    resolve_slice_id,
)

# Valid phase names for ``brc read-peer-artifact --phase`` (#2908
# slice-5). Mirrors ``_VALID_PHASES`` in ``handlers/brc.py``; kept as a
# tuple here so the argparse ``choices=`` directive can introspect the
# allowed values without importing the handler package at parser-build
# time (the handler import is deferred to cmd-execution time).
_VALID_BRC_HISTORY_PHASES: tuple[str, ...] = ("refine", "plan", "implement", "pr")


def cmd_brc_next_action(args: argparse.Namespace) -> int:
    """Derive the next BRC action for a role from the orchestrator.

    Calls the new ``POST /api/v1/pipelines/{pid}/consensus/next-action``
    route (#2908 slice-1, ``orchestrator/routes/consensus.py``). The
    route inspects the in-memory consensus tracker (with replay
    fallback) and returns one of::

        {"action": "wait" | "propose" | "ack" | "nack" | "confirm" | "complete",
         "event_payload": {...optional...}, "reason": "..."}

    Used by the event-pump wrapper to decide whether to invoke the
    agent (propose / review / confirm) or block on the message bus
    (wait).
    """
    pid = require_pipeline_id(args)
    role = args.role or _pkg.get_agent_role_from_env()
    if not role:
        print(
            "Error: --role required or set EGG_AGENT_ROLE",
            file=sys.stderr,
        )
        return 1

    data: dict[str, Any] = {"role": role}
    slice_id = getattr(args, "slice_id", None) or resolve_slice_id()
    if slice_id:
        data["slice_id"] = slice_id

    result = _pkg.orch_request(
        f"/api/v1/pipelines/{pid}/consensus/next-action",
        method="POST",
        data=data,
    )

    if args.json:
        # Drop the ``success`` envelope so jq-driven wrapper bash gets
        # the action payload directly; preserve everything else.
        body = {k: v for k, v in result.items() if k != "success"}
        print_json(body)
        return 0

    action = result.get("action", "unknown")
    reason = result.get("reason", "")
    print(f"Next action for {role}: {action}")
    if reason:
        print(f"  Reason: {reason}")
    event_payload = result.get("event_payload") or {}
    if event_payload:
        print("  Event payload:")
        for k, v in event_payload.items():
            print(f"    {k}: {v}")
    return 0


def cmd_brc_get_state(args: argparse.Namespace) -> int:
    """Return the BRC consensus state as structured JSON.

    Verb-level alias for ``mcp__brc__get_state``. The MCP-tool surface
    exposed shape ``{ok, slice_id, consensus, is_complete,
    blocking_agents, raw?}``; we mirror that exact shape so the
    event-pump wrapper (#2908 slice-2) can call the CLI form
    interchangeably with the MCP form.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    req: dict[str, Any] = {"pipeline_id": pid}
    slice_id = getattr(args, "slice_id", None) or resolve_slice_id()
    if slice_id:
        req["slice_id"] = slice_id
    if getattr(args, "verbose", False):
        req["verbose"] = True

    try:
        resp = _handlers.brc_get_state(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Always JSON — the handler's response is the only useful payload.
    # ``--verbose`` flips ``raw`` on as documented in the MCP-tool
    # description.
    print_json(resp)
    return 0


def cmd_brc_list_blocking(args: argparse.Namespace) -> int:
    """List agent roles currently blocking consensus.

    Verb-level CLI wrapper around ``mcp__brc__list_blocking``. Default
    output is one role per line for shell-friendly consumption
    (``while read role; do …; done``); ``--json`` returns the
    ``{blocking_agents: [...]}`` array. Exit code 0 even when the
    list is empty so the wrapper bash can call this unconditionally
    in the event-pump loop.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    try:
        resp = _handlers.brc_list_blocking({"pipeline_id": pid})
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    blocking = list(resp.get("blocking_agents", []) or [])
    if args.json:
        print_json({"blocking_agents": blocking})
        return 0

    for role in blocking:
        print(role)
    return 0


def cmd_brc_resolve_obligation(args: argparse.Namespace) -> int:
    """Mark a reviewer's conditional-ACK obligation satisfied in-cycle (#2338).

    Verb-level CLI wrapper around ``mcp__brc__resolve_obligation``. The
    write-side BRC verbs (propose / ack / nack / withdraw / confirmed)
    live under ``consensus``; the read/derive verbs and the
    obligation-management verbs live under ``brc``. Slice-5 adds this
    CLI because slice-6 deletes the agent-side MCP server — the
    wrapper bash must reach this signal without an MCP round-trip.

    Args mirror the handler request: ``--reviewer-role`` and
    ``--producer-role`` are required; ``--commit-sha`` and ``--note``
    are optional. Prose ``--note`` is loaded via the shared #2741
    prose-arg plumbing (argv / stdin sentinel / ``--note-file PATH``).
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)
    reviewer_role = args.reviewer_role
    producer_role = args.producer_role
    commit_sha = getattr(args, "commit_sha", None) or None
    # --note is optional, so don't fail if neither channel is set.
    try:
        note_text = _resolve_prose_arg(
            argv_value=getattr(args, "note", None),
            file_path=getattr(args, "note_file", None),
            arg_name="--note",
            file_flag="--note-file",
            required=False,
        )
    except _ProseArgError:
        return 2

    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "reviewer_role": reviewer_role,
        "producer_role": producer_role,
    }
    if commit_sha:
        req["commit_sha"] = commit_sha
    if note_text:
        req["note"] = note_text

    try:
        resp = _handlers.brc_resolve_obligation(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    if args.json:
        print_json(resp.get("signal", resp))
        return 0
    print(
        f"Obligation resolved by {role}: reviewer={reviewer_role} "
        f"producer={producer_role}" + (f" (commit={commit_sha})" if commit_sha else "")
    )
    return 0


def cmd_brc_read_peer_artifact(args: argparse.Namespace) -> int:
    """Read consensus history for a peer from the local brc-history log.

    Verb-level CLI wrapper around ``mcp__brc__read_peer_artifact``. The
    handler reads ``.egg-state/brc-history/<identifier>-<phase>.json``
    (and the per-slice partition for ``phase == "implement"`` when
    ``EGG_SLICE_ID`` is set). Output is always JSON; pagination uses an
    opaque ``next_cursor`` token round-tripped via ``--cursor``.

    Slice-5 adds this CLI because slice-6 deletes the MCP server —
    reviewers in the event-pump model invoke ``egg-orch brc
    read-peer-artifact`` from bash to inspect a peer's prior history
    without leaving the wrapper loop.

    Caller-supplied ``--pipeline-id`` / repo-path values are ignored
    by the handler (the identifier is resolved server-side from
    ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` for cross-pipeline-read
    hardening; risk_analyst R2). The CLI surface preserves a
    positional ``pipeline_id`` only for argparse-shape consistency
    with the other ``brc`` subcommands.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    req: dict[str, Any] = {
        "phase": args.phase,
        "include_unattributed": getattr(args, "include_unattributed", True),
    }
    if getattr(args, "peer_role", None):
        req["peer_role"] = args.peer_role
    if getattr(args, "message_type", None):
        # argparse with ``action="append"`` gives us a list when used
        # repeatedly. Single-value calls still produce a list, which
        # the handler accepts (it normalises both shapes; see
        # ``handlers/brc.py`` brc_read_peer_artifact docstring).
        req["message_type"] = list(args.message_type)
    if getattr(args, "limit", None) is not None:
        req["limit"] = args.limit
    if getattr(args, "cursor", None):
        req["cursor"] = args.cursor

    try:
        resp = _handlers.brc_read_peer_artifact(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Stdout JSON regardless of --json: the response is structured and
    # the only useful surface (mirrors brc_get_state's CLI shape).
    print_json(resp)
    return 0


# ---------------------------------------------------------------------------
# Phase verb-level subcommands (#2908 slice-1)
# ---------------------------------------------------------------------------
