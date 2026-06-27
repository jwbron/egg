"""Pipeline-lifecycle subcommands (list/get/create/status/delete/wait-status).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import json
import sys
from typing import Any
from urllib.parse import quote, urlencode

from egg_lib import orch_cli as _pkg

from ._common import _render_handler_error
from ._http import (
    ApiError,
    api_request,
    get_orchestrator_url,
    print_json,
    require_pipeline_id,
)


def cmd_pipeline_list(args: argparse.Namespace) -> int:
    """List all pipelines."""
    params: dict[str, str] = {}
    if args.status:
        params["status"] = args.status
    if args.limit:
        params["limit"] = str(args.limit)

    endpoint = "/api/v1/pipelines"
    if params:
        endpoint += "?" + urlencode(params)

    result = _pkg.orch_request(endpoint)

    if args.json:
        print_json(result)
        return 0

    pipelines = result.get("data", {}).get("pipelines", [])
    if not pipelines:
        print("No pipelines found.")
        return 0

    for p in pipelines:
        status = p.get("status", "unknown")
        phase = p.get("current_phase", "?")
        pid = p.get("id", p.get("pipeline_id", "?"))
        repo = p.get("repo", "?")
        print(f"  {pid}  {status:<12}  phase={phase:<12}  repo={repo}")

    print(f"\n{len(pipelines)} pipeline(s)")
    return 0


def cmd_pipeline_get(args: argparse.Namespace) -> int:
    """Get pipeline details."""
    pid = require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", {}).get("pipeline", result.get("data", result))
    print(f"Pipeline: {data.get('id', data.get('pipeline_id'))}")
    print(f"Status:   {data.get('status')}")
    print(f"Phase:    {data.get('current_phase')}")
    print(f"Repo:     {data.get('repo')}")
    print(f"Branch:   {data.get('branch')}")
    if data.get("issue_number"):
        print(f"Issue:    #{data.get('issue_number')}")
    if data.get("created_at"):
        print(f"Created:  {data.get('created_at')}")
    if data.get("updated_at"):
        print(f"Updated:  {data.get('updated_at')}")
    return 0


def cmd_pipeline_create(args: argparse.Namespace) -> int:
    """Create a new pipeline."""
    data: dict[str, Any] = {
        "repo": args.repo,
    }
    if args.issue:
        data["issue_number"] = args.issue
    if args.branch:
        data["branch"] = args.branch
    if args.prompt:
        data["prompt"] = args.prompt
    if args.network_mode:
        data["network_mode"] = args.network_mode
    if args.concurrent:
        data["config"] = {"concurrent_execution": True}

    result = _pkg.orch_request("/api/v1/pipelines", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    pipeline_data = result.get("data", {}).get("pipeline", result.get("data", {}))
    pid = pipeline_data.get("id", pipeline_data.get("pipeline_id", "unknown"))
    print(f"Created pipeline: {pid}")
    return 0


def cmd_pipeline_status(args: argparse.Namespace) -> int:
    """Get pipeline status.

    Delegates to :func:`egg_agent_tools.handlers.progress.progress_query_status`
    so the CLI and the ``mcp__progress__query_status`` MCP tool share a
    handler (iter-2 drift gate).
    """
    from egg_agent_tools.handlers import progress as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    req: dict[str, Any] = {"pipeline_id": pid, "include_raw": bool(args.json)}

    try:
        resp = _handlers.progress_query_status(req)
    except GatewayError as err:
        if args.json:
            print_json({"success": False, "message": err.message or str(err)})
            return int(getattr(err, "exit_code", 1))
        return _render_handler_error(err)
    except HandlerError as err:
        if args.json:
            print_json({"success": False, "message": err.message or str(err)})
            return int(err.exit_code)
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code

    if args.json:
        # Preserve the legacy shape: `{"success": true, "data": <status>}`.
        print_json({"success": True, "data": resp.get("raw") or resp})
        return 0

    print(f"Pipeline: {pid}")
    print(f"Status:   {resp.get('status')}")
    print(f"Phase:    {resp.get('current_phase')}")
    pending = resp.get("pending_decisions", 0)
    if pending:
        print(f"Pending decisions: {pending}")
    if resp.get("updated_at"):
        print(f"Updated: {resp.get('updated_at')}")
    return 0


def cmd_pipeline_delete(args: argparse.Namespace) -> int:
    """Delete a pipeline."""
    pid = require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}", method="DELETE")

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Deleted pipeline: {pid}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


# Terminal pipeline statuses that end the wait-status loop (issue #2211).
_WAIT_STATUS_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled"})
_WAIT_STATUS_TERMINAL_EVENTS = frozenset(
    {"pipeline.completed", "pipeline.failed", "pipeline.cancelled"}
)
# Map terminal ``status`` strings to the Path-A ``event_type`` they
# correspond to.  Used by the Path-B defense-in-depth path (issue #2378)
# so synthetic-terminal JSON lines carry the same ``event_type`` field
# Path-A consumers already key off (``failed``/``completed``/
# ``cancelled``).
_WAIT_STATUS_TO_EVENT_TYPE = {
    "complete": "pipeline.completed",
    "failed": "pipeline.failed",
    "cancelled": "pipeline.cancelled",
}


def cmd_pipeline_wait_status(args: argparse.Namespace) -> int:
    """Long-poll for pipeline events; emit JSON-lines (issue #2211).

    Host-side counterpart to ``egg-orch message wait-loop``. Loops the
    orchestrator's ``/api/v1/pipelines/<id>/status/wait`` route,
    threading the response cursor between calls. On Path-A (changed)
    emits one JSON-line on stdout with the dashboard-relevant subset
    of the envelope; on Path-B (no_change) loops silently. Exits per
    the §3 contract in ``docs/reference/agent-wait-patterns.md``: 0
    terminal, 1 max-iter, 2 transient, 3 permanent.
    """
    import time as _time

    # ``require_pipeline_id`` calls ``validate_id`` which already URL-encodes
    # the result; ``pid`` is therefore safe to interpolate into the endpoint
    # path directly.
    pid = require_pipeline_id(args)
    cursor = (getattr(args, "since", "") or "").strip()
    inner_timeout = max(int(getattr(args, "inner_timeout", 25) or 25), 1)
    max_iterations = getattr(args, "max_iterations", None)
    if not isinstance(max_iterations, int) or max_iterations <= 0:
        max_iterations = sys.maxsize

    backoff = 1.0
    # Cumulative *backoff sleep* time across consecutive transient failures.
    # NOT wall-clock — a stuck orchestrator that holds connections open for
    # ``inner_timeout + 15`` s per call would not advance this counter while
    # blocked, only while sleeping between retries.  The 60 s budget below
    # bounds how long the loop *backs off* before giving up; the operator
    # always retains the option to re-invoke after exit 2.
    backoff_sleep_total = 0.0
    backoff_sleep_budget = 60.0

    for _ in range(max_iterations):
        endpoint = f"/api/v1/pipelines/{pid}/status/wait?wait={inner_timeout}"
        if cursor:
            endpoint += f"&since={quote(cursor, safe='')}"

        try:
            result = api_request(get_orchestrator_url(), endpoint, timeout=inner_timeout + 15)
        except ApiError as err:
            status = err.status_code or 0
            if status and 400 <= status < 500:
                # 4xx — permanent.
                print(f"wait-status: {status} {err.message}", file=sys.stderr)
                return 3
            # 5xx, network errors, timeouts — transient.
            backoff_sleep_total += backoff
            if backoff_sleep_total > backoff_sleep_budget:
                print(
                    f"wait-status: transient error backoff budget exceeded ({err.message})",
                    file=sys.stderr,
                )
                return 2
            _time.sleep(min(backoff, 5.0))
            backoff = min(backoff * 2, 5.0)
            continue

        # Successful call resets the backoff window.
        backoff = 1.0
        backoff_sleep_total = 0.0

        envelope = result.get("data") if isinstance(result.get("data"), dict) else result
        if not isinstance(envelope, dict):
            print("wait-status: unexpected envelope shape", file=sys.stderr)
            return 3

        new_cursor = envelope.get("cursor")
        if isinstance(new_cursor, str) and new_cursor:
            cursor = new_cursor

        if envelope.get("changed") is True:
            line: dict[str, Any] = {
                "trigger": envelope.get("trigger"),
                "cursor": envelope.get("cursor"),
                "current_phase": envelope.get("current_phase"),
                "status": envelope.get("status"),
            }
            if "phase_elapsed_seconds" in envelope:
                line["phase_elapsed_seconds"] = envelope["phase_elapsed_seconds"]
            if envelope.get("trigger") == "event":
                line["event_type"] = envelope.get("event_type")
            elif envelope.get("trigger") == "message":
                line["messages"] = envelope.get("messages")
            if "concurrent" in envelope:
                line["concurrent"] = envelope["concurrent"]
            print(json.dumps(line), flush=True)

            status_str = envelope.get("status")
            event_type = envelope.get("event_type") or ""
            if (
                isinstance(status_str, str) and status_str in _WAIT_STATUS_TERMINAL_STATUSES
            ) or event_type in _WAIT_STATUS_TERMINAL_EVENTS:
                return 0
        else:
            # Path B (no_change): defense-in-depth for issue #2378. The
            # server short-circuits already-terminal pipelines on Path A,
            # but if any other code path leaves us subscribed past a
            # terminal state, the envelope's ``status`` field still
            # carries the truth — emit a synthetic terminal line and
            # exit 0 instead of looping silently.
            status_str = envelope.get("status")
            if isinstance(status_str, str) and status_str in _WAIT_STATUS_TERMINAL_STATUSES:
                line = {
                    "trigger": "synthetic-terminal",
                    "cursor": envelope.get("cursor"),
                    "current_phase": envelope.get("current_phase"),
                    "status": status_str,
                    "event_type": _WAIT_STATUS_TO_EVENT_TYPE[status_str],
                }
                print(json.dumps(line), flush=True)
                return 0

    return 1


# ---------------------------------------------------------------------------
# Signal commands
# ---------------------------------------------------------------------------
