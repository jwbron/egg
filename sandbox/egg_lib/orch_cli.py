#!/usr/bin/env python3
"""
Orchestrator CLI for interacting with the egg orchestrator API.

Provides commands for pipeline management, signal sending, phase transitions,
decision queuing, and container operations. Usable by both the egg agent
and humans.

Commands:
    egg-orch health                              Check orchestrator + gateway health
    egg-orch pipeline list                       List all pipelines
    egg-orch pipeline get <id>                   Get pipeline details
    egg-orch pipeline create --repo <r> ...      Create a pipeline
    egg-orch pipeline status <id>                Get pipeline status
    egg-orch pipeline delete <id>                Delete a pipeline
    egg-orch signal complete <pipeline_id> ...   Signal completion
    egg-orch signal progress <pipeline_id> ...   Signal progress
    egg-orch signal error <pipeline_id> ...      Signal error
    egg-orch signal heartbeat <pipeline_id> ...  Send heartbeat
    egg-orch phase get <pipeline_id>             Get current phase
    egg-orch phase advance <pipeline_id>         Advance to next phase
    egg-orch phase start <pipeline_id>           Start current phase
    egg-orch phase complete <pipeline_id>        Complete current phase
    egg-orch decision list <pipeline_id>         List decisions
    egg-orch decision create <pipeline_id> ...   Queue a decision
    egg-orch decision resolve <pid> <did> ...    Resolve a decision
    egg-orch decision status <pipeline_id>       Decision queue status
    egg-orch container list <pipeline_id>        List containers
    egg-orch container spawn <pipeline_id> ...   Spawn a container
    egg-orch container get <pid> <cid>           Get container info
    egg-orch container stop <pid> <cid>          Stop a container
    egg-orch container logs <pid> <cid>          Get container logs
    egg-orch message send <pid> --to <role> ...  Send inter-agent message (concurrent mode)
    egg-orch message poll <pid> ...              Poll for messages (concurrent mode)
    egg-orch message wait <pid> --for TYPE ...   Block until typed event arrives
    egg-orch message wait-loop <pid> --for TYPE  Loop message wait until match / cap
    egg-orch message heartbeat <pid> --state X   Emit structured HEARTBEAT
    egg-orch message status <pid>                Get message bus status (concurrent mode)
    egg-orch signal readiness <pid> --state ...  Signal readiness state (concurrent mode)
    egg-orch consensus propose <pid> ...         Send BRC consensus proposal
    egg-orch consensus ack <producer> ...        ACK a producer's proposal
    egg-orch consensus nack <producer> ...       NACK a producer's proposal
    egg-orch consensus withdraw <pid> ...        Withdraw proposal
    egg-orch consensus confirmed <pid>           Confirm after all reviewers ACK
    egg-orch consensus status <pid>              Show BRC consensus status
    egg-orch overseer alert <pid> ...            Broadcast OVERSEER_ALERT to human operator
"""

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

try:
    from egg_config.constants import (
        GATEWAY_PORT,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    ORCHESTRATOR_PORT = 9849  # noqa: EGG002
    GATEWAY_PORT = 9848  # noqa: EGG002

# Validation pattern for IDs used in URL path segments
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


class ApiError(Exception):
    """Error from an API request."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details


def _proposal_version_type(raw: str) -> int:
    """argparse type for ``--ack-version`` / ``--nack-version``.

    Mirrors the handler-side ``_require_version_int`` constraint at parse time
    so the error surfaces in ``--help`` and the rejection lands before the
    request is built.  v0 is meaningless because it predates the producer's
    first ``CONSENSUS_PROPOSE``.
    """
    try:
        version = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer; got {raw!r}") from exc
    if version < 1:
        raise argparse.ArgumentTypeError(
            f"must be >= 1; got {version} (v0 means no proposal exists yet)"
        )
    return version


def validate_id(value: str, name: str) -> str:
    """Validate that an ID is safe for use in URL paths.

    Accepts UUIDs and alphanumeric strings with hyphens, underscores, and dots.
    """
    if not value:
        print(f"Error: {name} cannot be empty", file=sys.stderr)
        sys.exit(1)
    if not _SAFE_ID_PATTERN.match(value):
        print(
            f"Error: Invalid {name} '{value}': must contain only "
            "alphanumeric characters, hyphens, underscores, and dots",
            file=sys.stderr,
        )
        sys.exit(1)
    return quote(value, safe="")


def get_orchestrator_url() -> str:
    """Get the orchestrator base URL.

    Uses hostname instead of IP so the CLI works from both egg-isolated
    and egg-external Docker networks.
    """
    url = os.environ.get("EGG_ORCHESTRATOR_URL")
    if url:
        return url.rstrip("/")
    return f"http://egg-orchestrator:{ORCHESTRATOR_PORT}"


def get_gateway_url() -> str:
    """Get the gateway base URL."""
    url = os.environ.get("GATEWAY_URL")
    if url:
        return url.rstrip("/")
    return f"http://egg-gateway:{GATEWAY_PORT}"


def get_pipeline_id_from_env() -> str | None:
    """Get pipeline ID from environment if set."""
    return os.environ.get("EGG_PIPELINE_ID")


def get_agent_role_from_env() -> str | None:
    """Get agent role from environment if set."""
    return os.environ.get("EGG_AGENT_ROLE")


def get_issue_number() -> int | None:
    """Get the current issue number from environment."""
    issue_str = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


def get_session_token() -> str | None:
    """Get session token for gateway auth."""
    from pathlib import Path

    token = os.environ.get("EGG_SESSION_TOKEN")
    if token:
        return token
    token_file = Path.home() / ".egg-session-token"
    if token_file.exists():
        return token_file.read_text().strip()
    return None


# Bypass proxy for internal network requests
_opener = build_opener(ProxyHandler({}))


def api_request(
    base_url: str,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an HTTP request to an internal API.

    Args:
        base_url: Base URL (orchestrator or gateway)
        endpoint: API path
        method: HTTP method
        data: JSON body data
        timeout: Request timeout in seconds
        headers: Additional headers

    Returns:
        Response JSON

    Raises:
        ApiError: On request failure
    """
    url = f"{base_url}{endpoint}"
    req_headers: dict[str, str] = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    body = json.dumps(data).encode() if data is not None else None

    try:
        request = Request(url, data=body, headers=req_headers, method=method)
        with _opener.open(request, timeout=timeout) as response:
            result: dict[str, Any] = json.loads(response.read().decode())
            return result
    except HTTPError as e:
        error_body = e.read().decode()
        try:
            error_data = json.loads(error_body)
            raise ApiError(
                error_data.get("message", str(e)),
                status_code=e.code,
                details=error_data.get("details"),
            ) from e
        except json.JSONDecodeError:
            raise ApiError(f"{e}: {error_body}", status_code=e.code) from e
    except URLError as e:
        raise ApiError(f"Connection error: {e.reason}") from e
    except TimeoutError as e:
        raise ApiError(f"Request timed out: {url}") from e


def api_request_or_exit(
    base_url: str,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an API request, printing errors and exiting on failure."""
    try:
        return api_request(base_url, endpoint, method, data, timeout, headers)
    except ApiError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        if e.status_code:
            print(f"Status: {e.status_code}", file=sys.stderr)
        if e.details:
            print(f"Details: {json.dumps(e.details, indent=2)}", file=sys.stderr)
        sys.exit(1)


def orch_request(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Make a request to the orchestrator API.

    Attaches ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>`` and
    ``X-Egg-Source: cli`` when the env var is present. Lifecycle-control
    endpoints (HITL resolve, pipeline CRUD, phase overrides, container
    spawn/stop) require this header. Agents don't get the env var, so
    they'll 401; humans running ``egg-orch`` from their shell will pass.
    """
    headers: dict[str, str] = {}
    lifecycle_secret = os.environ.get("EGG_LIFECYCLE_SECRET")
    if lifecycle_secret:
        headers["Authorization"] = f"Bearer {lifecycle_secret}"
        headers["X-Egg-Source"] = "cli"
    return api_request_or_exit(
        get_orchestrator_url(), endpoint, method, data, timeout, headers or None
    )


def gateway_request(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Make a request to the gateway API with session auth."""
    headers: dict[str, str] = {}
    token = get_session_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return api_request_or_exit(get_gateway_url(), endpoint, method, data, timeout, headers)


def print_json(data: Any) -> None:
    """Pretty-print JSON data."""
    print(json.dumps(data, indent=2))


def require_pipeline_id(args: argparse.Namespace) -> str:
    """Get pipeline_id from args or environment, validate, and return URL-safe value."""
    pid = getattr(args, "pipeline_id", None) or get_pipeline_id_from_env()
    if not pid:
        print(
            "Error: pipeline_id required. Provide as argument or set EGG_PIPELINE_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    return validate_id(pid, "pipeline_id")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace) -> int:
    """Check orchestrator and gateway health."""
    health_data: dict[str, Any] = {"orchestrator": {}, "gateway": {}}
    errors = 0

    # Orchestrator health
    if not args.json:
        print("Orchestrator:")
    try:
        result = api_request(get_orchestrator_url(), "/api/v1/health", timeout=5)
        status = result.get("status", "unknown")
        health_data["orchestrator"] = {
            "status": status,
            "reachable": True,
            "timestamp": result.get("timestamp"),
            "components": result.get("components", {}),
        }
        if not args.json:
            icon = "ok" if status == "healthy" else "UNHEALTHY"
            print(f"  Status: {icon} ({status})")
            if result.get("timestamp"):
                print(f"  Time:   {result['timestamp']}")
            components = result.get("components", {})
            for name, state in components.items():
                print(f"  {name}: {state}")
    except ApiError:
        health_data["orchestrator"] = {"status": "unreachable", "reachable": False}
        if not args.json:
            print("  Status: UNREACHABLE")
        errors += 1

    if not args.json:
        print()

    # Gateway health
    if not args.json:
        print("Gateway:")
    try:
        result = api_request(get_gateway_url(), "/api/v1/health", timeout=5)
        valid = result.get("github_token_valid", False)
        health_data["gateway"] = {
            "status": "ok",
            "reachable": True,
            "github_token_valid": valid,
        }
        if not args.json:
            print("  Status: ok")
            print(f"  GitHub token valid: {valid}")
    except ApiError:
        health_data["gateway"] = {"status": "unreachable", "reachable": False}
        if not args.json:
            print("  Status: UNREACHABLE")
        errors += 1

    if args.json:
        print_json(health_data)

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Pipeline commands
# ---------------------------------------------------------------------------


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

    result = orch_request(endpoint)

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
    result = orch_request(f"/api/v1/pipelines/{pid}")

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

    result = orch_request("/api/v1/pipelines", method="POST", data=data)

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
    result = orch_request(f"/api/v1/pipelines/{pid}", method="DELETE")

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
        # Path B (no_change): silent, loop again.

    return 1


# ---------------------------------------------------------------------------
# Signal commands
# ---------------------------------------------------------------------------


def _require_role(args: argparse.Namespace) -> str:
    """Get agent role from args or environment."""
    role = args.role or get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        sys.exit(1)
    return role


def _render_handler_error(err: Any) -> int:
    """Render a GatewayError / HandlerError in the legacy orch_cli stderr shape.

    Used by the MCP-counterpart ``cmd_*`` functions so CLI parity is
    preserved when a handler raises instead of returning ``success=False``.
    """
    message = getattr(err, "message", None) or str(err)
    print(f"Error: {message}", file=sys.stderr)
    status = getattr(err, "status_code", None)
    if status:
        print(f"Status: {status}", file=sys.stderr)
    details = getattr(err, "details", None)
    if details:
        try:
            print(f"Details: {json.dumps(details, indent=2)}", file=sys.stderr)
        except TypeError, ValueError:
            pass
    return int(getattr(err, "exit_code", 1))


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

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

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

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

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


def cmd_phase_get(args: argparse.Namespace) -> int:
    """Get current phase for a pipeline."""
    pid = require_pipeline_id(args)
    result = orch_request(f"/api/v1/pipelines/{pid}/phase")

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

    result = orch_request(f"/api/v1/pipelines/{pid}/phase", method="POST", data=data)

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
    result = orch_request(f"/api/v1/pipelines/{pid}/phase/start", method="POST", data={})

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

    result = orch_request(f"/api/v1/pipelines/{pid}/phase/complete", method="POST", data=data)

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


def cmd_decision_list(args: argparse.Namespace) -> int:
    """List decisions for a pipeline."""
    pid = require_pipeline_id(args)
    result = orch_request(f"/api/v1/pipelines/{pid}/decisions")

    if args.json:
        print_json(result)
        return 0

    decisions = result.get("data", {}).get("decisions", [])
    if not decisions:
        print("No decisions found.")
        return 0

    for d in decisions:
        did = d.get("decision_id", d.get("id", "?"))
        status = d.get("status", "pending")
        question = d.get("question", "")
        icon = {"pending": "?", "resolved": "ok", "cancelled": "x", "timed_out": "!"}
        print(f"  [{icon.get(status, '?')}] {did}: {question}")

    return 0


def cmd_decision_create(args: argparse.Namespace) -> int:
    """Queue a new decision."""
    pid = require_pipeline_id(args)
    data: dict[str, Any] = {
        "question": args.question,
    }
    if args.context:
        data["context"] = args.context
    if args.options:
        data["options"] = args.options
    else:
        # Warn about empty options for choice-type decisions.  Agents should
        # use `egg-contract add-decision` which formats options properly and
        # auto-appends an "Other" option.  See #1016.
        decision_type = args.decision_type or "choice"
        if decision_type == "choice":
            print(
                "Warning: No --options provided for choice decision. "
                "Consider using `egg-contract add-decision` which formats "
                "options properly and auto-appends an 'Other' option.",
                file=sys.stderr,
            )
    if args.phase:
        data["phase"] = args.phase
    if args.decision_type:
        data["decision_type"] = args.decision_type

    result = orch_request(f"/api/v1/pipelines/{pid}/decisions", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        dec_data = result.get("data", {}).get("decision", result.get("data", {}))
        did = dec_data.get("id", dec_data.get("decision_id", "?"))
        print(f"Created decision: {did}")
        print(f"Question: {args.question}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_decision_resolve(args: argparse.Namespace) -> int:
    """Resolve a decision."""
    pid = require_pipeline_id(args)
    did = validate_id(args.decision_id, "decision_id")
    data: dict[str, Any] = {
        "resolution": args.resolution,
    }
    if args.resolved_by:
        data["resolved_by"] = args.resolved_by

    result = orch_request(
        f"/api/v1/pipelines/{pid}/decisions/{did}/resolve",
        method="POST",
        data=data,
    )

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Resolved decision: {args.decision_id}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_decision_status(args: argparse.Namespace) -> int:
    """Get decision queue status."""
    pid = require_pipeline_id(args)
    result = orch_request(f"/api/v1/pipelines/{pid}/decisions/status")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    print(f"Pending:   {data.get('pending', 0)}")
    print(f"Resolved:  {data.get('resolved', 0)}")
    print(f"Cancelled: {data.get('cancelled', 0)}")
    print(f"Timed out: {data.get('timed_out', 0)}")
    return 0


# ---------------------------------------------------------------------------
# Container commands
# ---------------------------------------------------------------------------


def cmd_container_list(args: argparse.Namespace) -> int:
    """List containers for a pipeline."""
    pid = require_pipeline_id(args)
    result = orch_request(f"/api/v1/pipelines/{pid}/containers")

    if args.json:
        print_json(result)
        return 0

    containers = result.get("data", {}).get("containers", [])
    if not containers:
        print("No containers found.")
        return 0

    for c in containers:
        cid = c.get("container_id", "?")[:12]
        role = c.get("agent_role", "?")
        status = c.get("status", "?")
        print(f"  {cid}  {role:<12}  {status}")

    return 0


def cmd_container_spawn(args: argparse.Namespace) -> int:
    """Spawn a container for a pipeline."""
    pid = require_pipeline_id(args)
    data: dict[str, Any] = {
        "agent_role": args.role,
    }
    if args.issue:
        data["issue_number"] = args.issue
    if args.private:
        data["private_mode"] = True

    result = orch_request(
        f"/api/v1/pipelines/{pid}/spawn",
        method="POST",
        data=data,
        timeout=60,
    )

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        cid = result.get("data", {}).get("container_id", "?")
        print(f"Spawned container: {cid}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_container_get(args: argparse.Namespace) -> int:
    """Get container info."""
    pid = require_pipeline_id(args)
    cid = validate_id(args.container_id, "container_id")
    result = orch_request(f"/api/v1/pipelines/{pid}/containers/{cid}")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", {}).get("container", result.get("data", result))
    print(f"Container: {data.get('container_id')}")
    print(f"Role:      {data.get('agent_role')}")
    print(f"Status:    {data.get('status')}")
    if data.get("created_at"):
        print(f"Created:   {data.get('created_at')}")
    return 0


def cmd_container_stop(args: argparse.Namespace) -> int:
    """Stop a container."""
    pid = require_pipeline_id(args)
    cid = validate_id(args.container_id, "container_id")
    result = orch_request(
        f"/api/v1/pipelines/{pid}/containers/{cid}/stop",
        method="POST",
        data={},
    )

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Stopped container: {args.container_id}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_container_logs(args: argparse.Namespace) -> int:
    """Get container logs."""
    pid = require_pipeline_id(args)
    cid = validate_id(args.container_id, "container_id")
    params: dict[str, str] = {}
    if args.lines:
        params["tail"] = str(args.lines)

    endpoint = f"/api/v1/pipelines/{pid}/containers/{cid}/logs"
    if params:
        endpoint += "?" + urlencode(params)

    result = orch_request(endpoint)

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    logs = data.get("logs", data.get("output", ""))
    if logs:
        print(logs)
    else:
        print("(no logs)")
    return 0


# ---------------------------------------------------------------------------
# Gateway-specific commands
# ---------------------------------------------------------------------------


def cmd_gateway_health(args: argparse.Namespace) -> int:
    """Check gateway health."""
    try:
        result = api_request(get_gateway_url(), "/api/v1/health", timeout=5)
    except ApiError:
        if args.json:
            print_json({"status": "unreachable", "reachable": False})
            return 1
        print("Status: UNREACHABLE")
        return 1

    if args.json:
        print_json(result)
        return 0

    print("Status: ok")
    print(f"GitHub token valid: {result.get('github_token_valid', False)}")
    return 0


def cmd_gateway_phase(args: argparse.Namespace) -> int:
    """Get current phase from gateway."""
    issue = args.issue
    if not issue:
        issue = get_issue_number()
    if not issue:
        print("Error: --issue required or set EGG_ISSUE_NUMBER", file=sys.stderr)
        return 1

    endpoint = f"/api/v1/phase/current/{issue}"
    result = gateway_request(endpoint)

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    print(f"Phase: {data.get('phase', 'unknown')}")
    return 0


def cmd_gateway_permissions(args: argparse.Namespace) -> int:
    """Get allowed operations for a phase."""
    phase = quote(args.phase, safe="")
    result = gateway_request(f"/api/v1/phase/permissions/{phase}")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    allowed = data.get("allowed_operations", data.get("allowed", []))
    blocked = data.get("blocked_operations", data.get("blocked", []))

    if allowed:
        print("Allowed:")
        for op in allowed:
            desc = op.get("description", op) if isinstance(op, dict) else op
            print(f"  + {desc}")
    if blocked:
        print("Blocked:")
        for op in blocked:
            desc = op.get("description", op) if isinstance(op, dict) else op
            print(f"  - {desc}")
    return 0


# ---------------------------------------------------------------------------
# Message commands (concurrent mode)
# ---------------------------------------------------------------------------


def cmd_message_send(args: argparse.Namespace) -> int:
    """Send an inter-agent message."""
    pid = require_pipeline_id(args)
    role = args.role or get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        sys.exit(1)

    data: dict[str, Any] = {
        "from_role": role,
        "to_role": args.to,
        "message_type": args.type,
        "subject": args.subject or "",
        "body": args.body or "",
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/messages", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        msg = result.get("data", {}).get("message", {})
        print(f"Message sent: {msg.get('id', 'unknown')}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_message_poll(args: argparse.Namespace) -> int:
    """Poll for inter-agent messages."""
    pid = require_pipeline_id(args)

    params: dict[str, str] = {}
    role = args.role or get_agent_role_from_env()
    if role:
        params["role"] = role
    if args.since:
        params["since_id"] = args.since
    if args.limit:
        params["limit"] = str(args.limit)
    wait = getattr(args, "wait", None)
    if wait is not None:
        params["wait"] = str(wait)

    endpoint = f"/api/v1/pipelines/{pid}/messages"
    if params:
        endpoint += "?" + urlencode(params)

    # Use a longer timeout when long-polling to avoid client-side timeout
    timeout = (wait + 5) if wait else 15
    result = orch_request(endpoint, timeout=timeout)

    if args.json:
        print_json(result)
        return 0

    messages = result.get("data", {}).get("messages", [])
    if not messages:
        print("No messages.")
        return 0

    for msg in messages:
        ts = msg.get("timestamp", "")[:19]
        from_r = msg.get("from_role", "?")
        to_r = msg.get("to_role", "?")
        mtype = msg.get("message_type", "?")
        subject = msg.get("subject", "")
        print(f"  [{ts}] {from_r} -> {to_r} ({mtype}): {subject}")
        body = msg.get("body", "")
        if body:
            # Indent multi-line bodies for readability
            indented = body.replace("\n", "\n    ")
            print(f"    {indented}")

    print(f"\n{len(messages)} message(s)")
    return 0


def _classify_gateway_error_rc(status: int | None) -> int:
    """Map a GatewayError status onto message-wait's transient/permanent rc."""
    if status is not None and 400 <= status < 500 and status != 408:
        return 3
    # 5xx, 408, connection / timeout failures
    return 2


def cmd_message_wait(args: argparse.Namespace) -> int:
    """Event-driven wait for a message of one or more types.

    Issue #1897: the canonical blocking primitive for BRC coordination.
    Agents should prefer this over ``message poll --wait`` with shell-level
    retry loops.

    Delegates to :func:`egg_agent_tools.handlers.message.message_wait`.

    Exit codes (contract):
        0 — one or more matching messages returned (printed to stdout).
        1 — timeout elapsed with no match.
        2 — transient error (5xx, network hiccup, JSON parse failure).
            Retrying is safe.
        3 — permanent error (4xx other than 408, bad pipeline id,
            argparse/config failure). Retrying will not help.
    """
    from egg_agent_tools.handlers import message as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    try:
        pid = require_pipeline_id(args)
    except SystemExit:
        return 3
    # require_pipeline_id validates but exits(1) — wrap semantics into 3.

    role = args.role or get_agent_role_from_env()
    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "for_types": list(args.for_ or []),
        "timeout": args.timeout if args.timeout is not None else 60,
    }
    if getattr(args, "from_", None):
        req["from_role"] = args.from_
    if args.since:
        req["since"] = args.since
    if args.limit:
        req["limit"] = args.limit

    try:
        resp = _handlers.message_wait(req)
    except GatewayError as err:
        # GatewayError subclasses HandlerError — match it first so
        # transient (5xx/408/network) failures map to rc=2, not rc=3.
        rc = _classify_gateway_error_rc(err.status_code)
        prefix = "Error" if rc == 3 else "Transient error"
        print(f"{prefix}: {err.message}", file=sys.stderr)
        return rc
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 3
    except Exception as err:  # pragma: no cover - defensive
        print(f"Unexpected error: {err}", file=sys.stderr)
        return 2

    messages = list(resp.get("messages") or [])
    matched = bool(resp.get("matched"))

    if args.json:
        print_json(resp.get("raw", {}))
    else:
        if matched:
            for msg in messages:
                ts = msg.get("timestamp", "")[:19]
                from_r = msg.get("from_role", "?")
                to_r = msg.get("to_role", "?")
                mtype = msg.get("message_type", "?")
                subject = msg.get("subject", "")
                print(f"  [{ts}] {from_r} -> {to_r} ({mtype}): {subject}")
                body = msg.get("body", "")
                if body:
                    indented = body.replace("\n", "\n    ")
                    print(f"    {indented}")
            print(f"\n{len(messages)} message(s) matched")

    return 0 if matched else 1


def cmd_message_wait_loop(args: argparse.Namespace) -> int:
    """Canonical wait-loop convenience command (issue #1897).

    Loops **forever** until a matching message arrives (exit 0, prints
    the message) OR a permanent error occurs (exit 1).  The outer
    timeout is intentional — BRC consensus can legitimately take hours
    on long phases, and an agent wrapping this in its own outer loop
    would defeat the purpose.

    Exit codes (wrapper contract, DIFFERENT from ``message wait``):

      * 0 — a matching message arrived; it is printed to stdout.
      * 1 — a permanent error occurred (bad pipeline id, auth, argparse
        misuse propagated from an inner ``message wait`` rc=3).
        Callers should NOT retry.

    Transient errors (rc=2 from the inner call) are retried with short
    exponential backoff (cap 5s).  Timeouts (rc=1) re-enter the loop
    with a fresh inner call so the agent keeps blocking on the next
    event.

    ``--max-iterations`` is a safety valve only — its default is
    effectively unbounded (``sys.maxsize``) so normal BRC consensus
    never trips it.  The CLI help advertises it as "loops forever by
    default".

    Delegates to
    :func:`egg_agent_tools.handlers.message.message_wait_loop`.
    """
    from egg_agent_tools.handlers import message as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    # --json is not supported on wait-loop (produces concatenated JSON
    # objects on stdout across iterations).  Force it off so downstream
    # renders match the legacy behaviour.
    args.json = False

    try:
        pid = require_pipeline_id(args)
    except SystemExit:
        return 1

    role = args.role or get_agent_role_from_env()
    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "for_types": list(args.for_ or []),
        "timeout": args.timeout if args.timeout is not None else 60,
    }
    if getattr(args, "from_", None):
        req["from_role"] = args.from_
    if args.since:
        req["since"] = args.since
    if args.limit:
        req["limit"] = args.limit
    if args.max_iterations is not None and args.max_iterations > 0:
        req["max_iterations"] = args.max_iterations

    try:
        resp = _handlers.message_wait_loop(req)
    except GatewayError as err:
        # GatewayError subclasses HandlerError — match it first.  The
        # handler reclassifies 4xx (non-408) as permanent and re-raises;
        # the wait-loop contract collapses both GatewayError and
        # HandlerError to rc=1 so callers can't confuse them with
        # transient misses.
        print(f"Error: {err.message}", file=sys.stderr)
        return 1
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 1

    messages = list(resp.get("messages") or [])
    matched = bool(resp.get("matched"))
    if matched:
        for msg in messages:
            ts = msg.get("timestamp", "")[:19]
            from_r = msg.get("from_role", "?")
            to_r = msg.get("to_role", "?")
            mtype = msg.get("message_type", "?")
            subject = msg.get("subject", "")
            print(f"  [{ts}] {from_r} -> {to_r} ({mtype}): {subject}")
            body = msg.get("body", "")
            if body:
                indented = body.replace("\n", "\n    ")
                print(f"    {indented}")
        print(f"\n{len(messages)} message(s) matched")
        return 0
    # Safety cap tripped — extraordinarily unlikely with the default
    # sys.maxsize cap. Return 1 (no match) so callers behave the same
    # as a bounded-retry timeout.
    return 1


def cmd_message_heartbeat(args: argparse.Namespace) -> int:
    """Emit a structured HEARTBEAT message (issue #1897).

    Delegates to
    :func:`egg_agent_tools.handlers.message.message_heartbeat`.  The
    handler POSTs to the dedicated ``/api/v1/pipelines/{id}/heartbeat``
    endpoint which handles schema validation, per-role dedup, and the
    ``EGG_HEARTBEAT_RATE_LIMIT`` 429 response.  HTTP 429 is treated as a
    rate-limit error (exit 3 per the CLI contract — caller should honour
    the server's suggested ``retry_after``).
    """
    from egg_agent_tools.handlers import message as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    try:
        pid = require_pipeline_id(args)
    except SystemExit:
        return 3

    role = args.role or get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        return 3

    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "state": args.state,
    }
    if args.waiting_on:
        req["waiting_on"] = args.waiting_on
    if args.since:
        req["since"] = args.since
    if args.body:
        req["body"] = args.body

    try:
        resp = _handlers.message_heartbeat(req)
    except GatewayError as err:
        # GatewayError subclasses HandlerError — match it first so we
        # can distinguish transport failures (rc=2/3 by status) from
        # user input errors (rc=3).
        # 429 rate-limit is a permanent error from this invocation's
        # perspective — caller should honour retry_after and try again
        # later.
        if err.status_code == 429:
            retry_after = 60
            try:
                if err.details and isinstance(err.details, dict):
                    retry_after = int(err.details.get("retry_after", 60))
            except TypeError, ValueError:
                pass
            print(
                f"Error: HEARTBEAT rate limit exceeded; retry after {retry_after}s.",
                file=sys.stderr,
            )
            return 3
        rc = _classify_gateway_error_rc(err.status_code)
        prefix = "Error" if rc == 3 else "Transient error"
        print(f"{prefix}: {err.message}", file=sys.stderr)
        return rc
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 3

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    if resp.get("deduped"):
        print(f"HEARTBEAT deduped (unchanged state {args.state})")
    else:
        print(f"HEARTBEAT sent: {args.state}")
    return 0


def cmd_message_status(args: argparse.Namespace) -> int:
    """Get message bus status."""
    pid = require_pipeline_id(args)
    result = orch_request(f"/api/v1/pipelines/{pid}/messages/status")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    print(f"Total messages: {data.get('total', 0)}")
    by_type = data.get("by_type", {})
    if by_type:
        for mtype, count in by_type.items():
            print(f"  {mtype}: {count}")
    return 0


# ---------------------------------------------------------------------------
# Overseer commands (escalation surface)
# ---------------------------------------------------------------------------


def cmd_overseer_alert(args: argparse.Namespace) -> int:
    """Broadcast an OVERSEER_ALERT to the human operator.

    Wraps the message-send endpoint with message_type=OVERSEER_ALERT and
    to_role="all" hard-coded so the overseer agent never picks the type by
    hand. The human-facing alert surfaces (sdlc skill, get_status enrichment)
    only react to OVERSEER_ALERT — STATUS/HANDOFF blend into normal traffic.

    Delegates to :func:`egg_agent_tools.handlers.progress.progress_overseer_alert`
    so the CLI and the ``mcp__progress__overseer_alert`` MCP tool share a
    handler (iter-2 drift gate).
    """
    from egg_agent_tools.handlers import progress as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = args.role or get_agent_role_from_env() or "overseer"

    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "anomaly": args.anomaly,
        "priority": args.priority,
        "summary": args.summary,
    }
    if args.detail:
        req["detail"] = args.detail
    if args.recommend:
        req["recommend"] = args.recommend
    # Issue #1962: structured recommendation + payload.
    recommendation = getattr(args, "recommendation", None)
    payload_file = getattr(args, "recommendation_payload_file", None)
    if recommendation:
        if not payload_file:
            print(
                "Error: --recommendation requires --recommendation-payload-file",
                file=sys.stderr,
            )
            return 2
        try:
            with open(payload_file, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"Error: cannot read --recommendation-payload-file: {exc}",
                file=sys.stderr,
            )
            return 2
        req["recommendation"] = recommendation
        req["recommendation_payload"] = payload

    try:
        resp = _handlers.progress_overseer_alert(req)
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
        print_json(resp.get("signal", {}))
        return 0

    msg = resp.get("alert") or {}
    print(f"OVERSEER_ALERT broadcast: {msg.get('id', 'unknown')} ({args.anomaly}, {args.priority})")
    return 0


# ---------------------------------------------------------------------------
# Overseer file-issue (issue #1962, decision-9 opt-1)
# ---------------------------------------------------------------------------

# Hard limits matching the gateway's defense-in-depth checks. Local-side
# rejection means the gateway doesn't have to fail us at the network
# boundary.
_OVERSEER_TITLE_MAX_CHARS = 120
_OVERSEER_BODY_MAX_BYTES = 50_000
_OVERSEER_VALID_LABEL_PRIORITIES = ("p0", "p1", "p2", "p3")


def cmd_overseer_file_issue(args: argparse.Namespace) -> int:
    """File a GitHub issue from the overseer role (issue #1962).

    Runs ``gh issue create`` itself, inside the sandbox, mediated by
    the gateway. There is no orchestrator-side endpoint that runs
    ``gh`` (decision-9 opt-1).

    Reads the issue title and body from local files (no shell-escaping
    headaches). Looks up an existing issue with the same anomaly
    signature first (intra-phase JSONL cache + cross-phase ``gh issue
    list --search`` fallback) and skips the gh call when one is found.
    On a fresh filing, appends a ``FiledIssueRecord`` to
    ``.egg-state/oversight/filed-issues.jsonl`` so a later cycle's
    dedup can short-circuit.

    Output: JSON ``{"issue_number": int, "filed": bool,
    "dedup_match": int|null}``. Exit code 0 on either outcome
    (filed-or-dedup); non-zero only on gh failure or local validation
    failure. The ``--dry-run`` flag prints the composed argv + JSON
    without invoking gh.
    """
    from datetime import UTC, datetime

    from egg_overseer.state import FiledIssueRecord, append_filed_issue

    repo = os.environ.get("EGG_PIPELINE_REPO")
    if not repo:
        print(
            "Error: EGG_PIPELINE_REPO env var is required (set by orchestrator)",
            file=sys.stderr,
        )
        return 2

    # Read title + body files locally so we can validate sizes before
    # the gateway has to. Files are sandbox-local (CLI-supplied paths).
    try:
        with open(args.issue_title_file, encoding="utf-8") as fh:
            title = fh.read().strip()
    except OSError as exc:
        print(f"Error: cannot read --issue-title-file: {exc}", file=sys.stderr)
        return 2
    try:
        with open(args.issue_body_file, "rb") as bfh:
            body_bytes = bfh.read()
    except OSError as exc:
        print(f"Error: cannot read --issue-body-file: {exc}", file=sys.stderr)
        return 2
    # Body itself is passed to gh via --body-file (no need to decode here);
    # the byte length is what we cap on.

    if len(title) > _OVERSEER_TITLE_MAX_CHARS:
        print(
            f"Error: title exceeds {_OVERSEER_TITLE_MAX_CHARS} chars (got {len(title)})",
            file=sys.stderr,
        )
        return 2
    if len(body_bytes) > _OVERSEER_BODY_MAX_BYTES:
        print(
            f"Error: body exceeds {_OVERSEER_BODY_MAX_BYTES} bytes (got {len(body_bytes)})",
            file=sys.stderr,
        )
        return 2

    if args.priority not in _OVERSEER_VALID_LABEL_PRIORITIES:
        # argparse already enforces this via choices; double-check for
        # programmatic callers that bypass the parser.
        print(
            f"Error: --priority must be one of "
            f"{list(_OVERSEER_VALID_LABEL_PRIORITIES)}; got {args.priority!r}",
            file=sys.stderr,
        )
        return 2

    # Dedup pre-check.
    from egg_lib.overseer_issue_body import find_existing_issue

    existing = find_existing_issue(
        repo=repo,
        anomaly_signature=args.anomaly_signature,
    )
    if existing is not None:
        result: dict[str, Any] = {
            "issue_number": existing,
            "filed": False,
            "dedup_match": existing,
        }
        if args.dry_run or args.json:
            print_json(result)
        else:
            print(
                f"Existing issue #{existing} already covers this anomaly; skipping gh issue create."
            )
        # Structured log line for metrics.
        import logging as _logging

        _logging.getLogger("egg_lib.overseer_file_issue").info(
            "overseer_event",
            extra={
                "event": "issue_filed",
                "outcome": "dedup",
                "issue_number": existing,
                "anomaly_signature": args.anomaly_signature,
                "anomaly_type": args.anomaly_type,
                "agent_role": args.agent_role,
            },
        )
        return 0

    # Build the gh argv. We pass --title inline (we've already read and
    # validated the title file locally) because the sandbox `gh` wrapper
    # at sandbox/scripts/gh::handle_issue_create only recognises
    # --title|-t, --body|-b, --body-file|-F, --label|-l, --assignee|-a.
    # --title-file is not a recognised flag and would cause the wrapper
    # to error before reaching the gateway. We also drop --json because
    # the wrapper doesn't pass it through; instead we parse the URL
    # gh prints to stdout to extract the issue number.
    argv = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        args.issue_body_file,
        "--label",
        "agent:overseer",
        "--label",
        args.priority,
    ]

    if args.dry_run:
        dry_result: dict[str, Any] = {
            "issue_number": None,
            "filed": False,
            "dedup_match": None,
            "dry_run": True,
            "argv": argv,
            "title": title,
            "body_bytes": len(body_bytes),
        }
        print_json(dry_result)
        return 0

    import subprocess as _subprocess

    try:
        proc = _subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired) as exc:
        print(f"Error: gh subprocess failed: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(
            f"Error: gh issue create exited {proc.returncode}: {proc.stderr}",
            file=sys.stderr,
        )
        return 1

    # gh issue create (without --json) prints the issue URL to stdout,
    # one line, e.g. "https://github.com/owner/repo/issues/123\n".
    # The sandbox `gh` wrapper does not pass --json through, so we
    # parse the trailing integer off the URL. Keep this resilient to
    # minor whitespace and trailing-slash variations.
    issue_number: int | None = None
    raw_stdout = (proc.stdout or "").strip()
    # First try JSON (covers tests / future wrapper extensions that
    # surface the --json output).
    if raw_stdout.startswith("{"):
        try:
            gh_payload = json.loads(raw_stdout)
            num = gh_payload.get("number")
            if isinstance(num, int):
                issue_number = num
        except json.JSONDecodeError:
            issue_number = None
    if issue_number is None:
        match = re.search(r"/issues/(\d+)", raw_stdout)
        if match:
            try:
                issue_number = int(match.group(1))
            except ValueError:
                issue_number = None
    if issue_number is None:
        print(
            f"Error: gh stdout did not contain an issue number: {raw_stdout!r}",
            file=sys.stderr,
        )
        return 1

    # Persist to JSONL cache for intra-phase dedup.
    try:
        append_filed_issue(
            ".egg-state/oversight/filed-issues.jsonl",
            FiledIssueRecord(
                issue_number=issue_number,
                anomaly_type=args.anomaly_type,
                anomaly_signature=args.anomaly_signature,
                agent_role=args.agent_role,
                repo=repo,
                pipeline_id=os.environ.get("EGG_PIPELINE_ID", ""),
                phase=os.environ.get("EGG_PHASE", ""),
                filed_at=datetime.now(UTC),
                parent_alert_message_id=getattr(args, "parent_alert_message_id", None),
                hitl_outcome="filed",
            ),
        )
    except OSError as exc:
        # Filing succeeded; cache write failure is loggable but not fatal.
        import logging as _logging

        _logging.getLogger("egg_lib.overseer_file_issue").warning(
            "overseer_event",
            extra={
                "event": "issue_filed_cache_failed",
                "issue_number": issue_number,
                "error": str(exc),
            },
        )

    import logging as _logging

    _logging.getLogger("egg_lib.overseer_file_issue").info(
        "overseer_event",
        extra={
            "event": "issue_filed",
            "outcome": "filed",
            "issue_number": issue_number,
            "anomaly_signature": args.anomaly_signature,
            "anomaly_type": args.anomaly_type,
            "agent_role": args.agent_role,
        },
    )

    filed_result: dict[str, Any] = {
        "issue_number": issue_number,
        "filed": True,
        "dedup_match": None,
    }
    if args.json:
        print_json(filed_result)
    else:
        print(f"Filed issue #{issue_number} ({args.anomaly_type}, {args.priority})")
    return 0


def cmd_overseer_consult_advisor(args: argparse.Namespace) -> int:
    """Consult the Opus advisor for a structured verdict (issue #1962).

    Runs ``egg_overseer.advisor.consult_advisor`` itself, inside the
    sandbox, so the underlying ``run_agent_async`` call lives on the
    LLM-execution side of the EGG200 boundary (``docs/guides/agent-mode-design.md``)
    and the orchestrator pod never touches Anthropic credentials.

    Reads the inputs (Haiku classification + Tier-1 health alerts +
    optional progress events / log lines) from a JSON file. Writes the
    validated ``AdvisorVerdict`` JSON to ``--output-file`` (or stdout
    when omitted). The caller (the overseer agent) is expected to gate
    the call behind ``should_consult_advisor`` (Haiku confidence ≥ 0.8
    AND a Tier-1 health alert active).

    Output (JSON): the verdict dict from ``AdvisorVerdict.model_dump()``.
    Exit codes:
        0 — success
        1 — advisor parse failure (the SDK returned a payload that did
            not match the ``AdvisorVerdict`` schema; the caller should
            classify as a parse drift, not a transient failure)
        2 — input validation / I/O failure (missing or malformed
            ``--inputs-file``; unwritable ``--output-file``)
        3 — advisor runtime failure (network, auth, rate-limit, or any
            other unhandled exception from the SDK call); distinct from
            parse failure so the caller can back off / retry vs. treat
            as a model-output drift
    """
    import asyncio
    from types import SimpleNamespace

    from egg_overseer.advisor import AdvisorParseError, consult_advisor

    inputs_path = args.inputs_file
    try:
        with open(inputs_path, encoding="utf-8") as fh:
            inputs = json.load(fh)
    except OSError as exc:
        print(f"Error: cannot read --inputs-file: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Error: --inputs-file is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(inputs, dict):
        print("Error: --inputs-file must be a JSON object", file=sys.stderr)
        return 2

    classification = inputs.get("classification") or {}
    health_alerts = inputs.get("health_alerts") or []
    progress_events = inputs.get("progress_events") or []
    recent_log_lines = inputs.get("recent_log_lines") or []

    if not isinstance(classification, dict):
        print("Error: inputs.classification must be an object", file=sys.stderr)
        return 2
    if not isinstance(health_alerts, list):
        print("Error: inputs.health_alerts must be an array", file=sys.stderr)
        return 2
    if not isinstance(progress_events, list):
        print("Error: inputs.progress_events must be an array", file=sys.stderr)
        return 2
    if not isinstance(recent_log_lines, list):
        print("Error: inputs.recent_log_lines must be an array", file=sys.stderr)
        return 2

    # Resolve advisor config knobs from PipelineConfig when a pipeline-id
    # is available (issues #2113, #2170). The orchestrator's status
    # endpoint exposes the overseer-relevant config subset
    # (orchestrator/routes/pipelines.py); we read each field and pass a
    # duck-typed config to consult_advisor. Falling back to config=None
    # keeps the historic defaults ("opus" model, 256 KiB log cap) for
    # callers that do not provide a pipeline-id, and for any failure
    # (orchestrator unreachable, malformed env, missing client module) —
    # never crash the verb on the lookup path. NOTE: extend the
    # SimpleNamespace assembly below if consult_advisor ever reads more
    # `config.*` attributes; the duck-typed surface silently falls back
    # to AttributeError today.
    advisor_config: Any = None
    pid = getattr(args, "pipeline_id", None) or get_pipeline_id_from_env()
    if pid and _SAFE_ID_PATTERN.match(pid):
        # Nested try so ImportError is handled before OrchestratorError is
        # referenced — combining them in a single except clause raises
        # NameError when the import itself fails (OrchestratorError is
        # never bound). See review feedback on PR #2158.
        try:
            from egg_lib.orch_client import OrchClient, OrchestratorError
        except ImportError as exc:
            print(
                f"Warning: cannot import egg_lib.orch_client ({exc}); "
                f"falling back to default advisor config",
                file=sys.stderr,
            )
        else:
            try:
                status = OrchClient().get_pipeline_status(quote(pid, safe=""))
                cfg_dict = status.get("config") if isinstance(status, dict) else None
                if isinstance(cfg_dict, dict):
                    ns_kwargs: dict[str, Any] = {}
                    model = cfg_dict.get("overseer_advisor_model")
                    if model:
                        ns_kwargs["overseer_advisor_model"] = model
                    # bytes-cap can legitimately be 0 (disable sentinel),
                    # so distinguish "absent" from "explicitly zero" with
                    # `is not None` rather than truthiness.
                    cap = cfg_dict.get("overseer_advisor_recent_log_bytes_cap")
                    if cap is not None:
                        ns_kwargs["overseer_advisor_recent_log_bytes_cap"] = cap
                    if ns_kwargs:
                        advisor_config = SimpleNamespace(**ns_kwargs)
            except OrchestratorError as exc:
                print(
                    f"Warning: cannot read PipelineConfig for {pid} "
                    f"({exc}); falling back to default advisor config",
                    file=sys.stderr,
                )
    elif pid:
        # Malformed pipeline-id (e.g. corrupted EGG_PIPELINE_ID): skip
        # the lookup silently rather than crashing via validate_id's
        # sys.exit(1), which would collide with AdvisorParseError's
        # exit-code semantics. See review feedback on PR #2158.
        print(
            f"Warning: pipeline_id {pid!r} is not a safe ID; falling back to default advisor config",
            file=sys.stderr,
        )

    recent_log_bytes_cap = getattr(args, "recent_log_bytes_cap", None)
    try:
        verdict = asyncio.run(
            consult_advisor(
                classification=classification,
                health_alerts=health_alerts,
                progress_events=progress_events,
                recent_log_lines=recent_log_lines,
                config=advisor_config,
                recent_log_bytes_cap=recent_log_bytes_cap,
            )
        )
    except AdvisorParseError as exc:
        print(f"Error: advisor parse failure: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Distinct exit code for SDK / runtime failures (network, auth,
        # rate-limit) so the caller can distinguish them from a parse
        # drift on AdvisorVerdict. The overseer agent uses this to
        # decide between retry / back-off and re-classifying the model
        # output.
        print(
            f"Error: advisor runtime failure ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 3

    payload = verdict.model_dump()
    rendered = json.dumps(payload, indent=2, sort_keys=True)

    if args.output_file:
        try:
            with open(args.output_file, "w", encoding="utf-8") as fh:
                fh.write(rendered)
        except OSError as exc:
            print(f"Error: cannot write --output-file: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print_json(payload)
        else:
            print(f"Wrote AdvisorVerdict to {args.output_file}")
    else:
        # No --output-file: stdout is the only sink, so the verdict
        # JSON always lands there. ``--json`` is meaningful only with
        # ``--output-file`` (where it controls whether to tee the
        # verdict to stdout in addition to writing the file).
        print(rendered)
    return 0


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

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

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


def _consensus_push() -> int:
    """Back-compat alias for :func:`egg_agent_tools.push.consensus_push`.

    The implementation moved to ``egg_agent_tools.push`` in #1994 so the
    ``mcp__brc__propose`` tool can share it.  Kept here as a thin alias
    so existing CLI callers and unit tests keep working.

    Returns only the exit code (discards error message) — the CLI
    surfaces errors via stderr prints inside ``consensus_push()``.
    """
    from egg_agent_tools.push import consensus_push as _impl

    rc, _err = _impl()
    return rc


def _render_stale_version_rejection(
    args: argparse.Namespace, resp: dict[str, Any], verdict: str
) -> int:
    """Render a stale-version ACK / NACK rejection (#2142).

    The orchestrator returns the producer's current proposal snapshot
    inline so the reviewer can re-fetch and re-review without a separate
    call.  Always exits 2 to signal "retry after re-review."
    """
    rejection = resp.get("rejection", {}) or {}
    if getattr(args, "json", False):
        print_json(rejection)
        return 2
    snap = rejection.get("current_proposal", {}) or {}
    producer = snap.get("producer") or resp.get("producer_role")
    print(
        f"{verdict} rejected: producer {producer} "
        f"is at v{snap.get('version')} (you reviewed an older version).",
        file=sys.stderr,
    )
    if snap.get("commit_sha"):
        print(f"  Current commit: {snap['commit_sha']}", file=sys.stderr)
    if snap.get("artifacts"):
        print(f"  Current artifacts: {', '.join(snap['artifacts'])}", file=sys.stderr)
    print(
        "Re-fetch the branch, re-review against the current version, and re-submit your verdict.",
        file=sys.stderr,
    )
    return 2


def cmd_consensus_propose(args: argparse.Namespace) -> int:
    """Send CONSENSUS_PROPOSE signal, optionally pushing code first.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_propose` so the
    MCP ``mcp__brc__propose`` tool and the CLI share one handler.  The
    ``--push`` / ``--file`` / ``--json`` / ``--commit-sha`` surface is
    preserved.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)

    # If --push, run git push before proposing so the code is on the remote
    # before the proposal is sent.  Because the proposal and push happen
    # together, auto-repropose is suppressed (the explicit proposal covers
    # the push within the debounce window).
    if getattr(args, "push", False):
        push_result = _consensus_push()
        if push_result != 0:
            return push_result

    req: dict[str, Any]
    if getattr(args, "file", None):
        # File-based payload: forward the parsed dict VERBATIM to the
        # handler via the ``raw_payload`` key so unknown/custom schema
        # fields are not silently dropped.  The handler still layers
        # our structured request on top for required defaults
        # (pipeline_id / role / commit_sha fallback).
        with open(args.file) as f:
            file_payload: dict[str, Any] = json.load(f)
        req = {
            "pipeline_id": pid,
            "role": role,
            "raw_payload": file_payload,
        }
        # Resolve commit SHA fallback even for --file so the handler
        # can default to HEAD when the payload omits it.
        commit_sha = file_payload.get("commit_sha") or getattr(args, "commit_sha", None)
        if commit_sha:
            req["commit_sha"] = commit_sha
    else:
        req = {
            "pipeline_id": pid,
            "role": role,
            "summary": getattr(args, "summary", "") or "",
            "artifacts": list(getattr(args, "artifacts", []) or []),
            "risk_considered": getattr(args, "risk", "") or "",
            "files_changed": list(getattr(args, "files_changed", []) or []),
            "tests_run": list(getattr(args, "tests_run", []) or []),
            "tasks": list(getattr(args, "tasks", []) or []),
        }
        if getattr(args, "commit_sha", None):
            req["commit_sha"] = args.commit_sha

    changed_artifacts = getattr(args, "changed_artifacts", None)
    if changed_artifacts:
        req["changed_artifacts"] = list(changed_artifacts)

    try:
        resp = _handlers.brc_propose(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Open-NACK barrier rejection (#2142): brc_propose returns a
    # structured ``open_nacks_blocked`` payload instead of raising so
    # the agent can introspect the inline NACK list and aggregate
    # fixes.  Render the rejection cleanly and exit non-zero so shell
    # callers can branch on it.
    if resp.get("status") == "open_nacks_blocked":
        rejection = resp.get("rejection", {}) or {}
        if args.json:
            print_json(rejection)
            return 2
        nacks = rejection.get("nacks") or []
        print(
            f"Re-propose blocked: {len(nacks)} unresolved NACK(s) "
            f"on v{rejection.get('current_version')}",
            file=sys.stderr,
        )
        for nack in nacks:
            print(
                f"  [{nack.get('reviewer')}] (v{nack.get('version')}) {nack.get('reason', '')}",
                file=sys.stderr,
            )
        print(
            "Address every finding above and re-propose. "
            "The retry will succeed once you've been notified of the full set.",
            file=sys.stderr,
        )
        return 2

    signal = resp.get("signal", {})
    if args.json:
        print_json(signal)
        return 0

    print(f"Proposal sent by {role}")
    phase = resp.get("phase")
    if phase:
        print(f"  BRC phase: {phase}")
    return 0


def cmd_consensus_ack(args: argparse.Namespace) -> int:
    """Send CONSENSUS_ACK signal for a producer.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_ack`.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)
    req = {
        "pipeline_id": pid,
        "role": role,
        "producer_role": args.producer_role,
        "reason": args.reason,
        "files_reviewed": list(args.files_reviewed or []),
        "pre_merge_condition": getattr(args, "pre_merge_condition", "") or "",
        "ack_version": args.ack_version,
    }
    try:
        resp = _handlers.brc_ack(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Stale-version rejection (#2142): re-fetch and re-review.
    if resp.get("status") == "stale_version":
        return _render_stale_version_rejection(args, resp, "ACK")

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    if req["pre_merge_condition"]:
        print(
            f"Conditional ACK sent by {role} for {args.producer_role} "
            f"(obligation: {req['pre_merge_condition']})"
        )
    else:
        print(f"ACK sent by {role} for {args.producer_role}")
    return 0


def cmd_consensus_nack(args: argparse.Namespace) -> int:
    """Send CONSENSUS_NACK signal for a producer.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_nack`.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)
    req = {
        "pipeline_id": pid,
        "role": role,
        "producer_role": args.producer_role,
        "reason": args.reason,
        "files_reviewed": list(args.files_reviewed or []),
        "nack_version": args.nack_version,
    }
    try:
        resp = _handlers.brc_nack(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Stale-version rejection (#2142): re-fetch and re-review.
    if resp.get("status") == "stale_version":
        return _render_stale_version_rejection(args, resp, "NACK")

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    print(f"NACK sent by {role} for {args.producer_role}: {args.reason}")
    return 0


def cmd_consensus_withdraw(args: argparse.Namespace) -> int:
    """Send CONSENSUS_WITHDRAW signal."""
    pid = require_pipeline_id(args)
    role = _require_role(args)

    data: dict[str, Any] = {
        "signal_type": "consensus_withdraw",
        "agent_role": role,
        "reason": args.reason,
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Proposal withdrawn by {role}: {args.reason}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_consensus_confirmed(args: argparse.Namespace) -> int:
    """Send CONSENSUS_CONFIRMED signal after all reviewers ACK.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_confirm`.
    Exit-code parity preserved: 2 for ``pending_acks``, 0 for
    confirmed, 1 for gateway error.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = _require_role(args)
    try:
        resp = _handlers.brc_confirm({"pipeline_id": pid, "role": role})
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    if args.json:
        print_json(resp.get("signal", {}))
        return 0

    if resp.get("status") == "pending_acks":
        print(f"Waiting for reviewer re-ACKs: {resp.get('message')}")
        return 2
    print(f"Confirmation recorded for {role}")
    if resp.get("consensus_reached"):
        print("  Consensus reached!")
    return 0


def cmd_consensus_status(args: argparse.Namespace) -> int:
    """Show BRC consensus status (approval matrix and review graph).

    Delegates the structured data-build to
    :func:`egg_agent_tools.handlers.brc.brc_get_state` so the MCP
    ``mcp__brc__get_state`` tool and this CLI share one handler.  The
    human-readable rendering stays here in the shim.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)

    try:
        resp = _handlers.brc_get_state({"pipeline_id": pid})
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    consensus = resp.get("consensus", {}) or {}
    if args.json:
        print_json(consensus)
        return 0

    if not consensus:
        print("No consensus data available.")
        return 0

    is_complete = consensus.get("is_complete", False)
    print(f"Consensus complete: {is_complete}")

    agents = consensus.get("agents", {})
    if agents:
        print("\nAgent states:")
        for agent_name, agent_data in agents.items():
            producer_phase = agent_data.get("producer_phase")
            reviewer_phase = agent_data.get("reviewer_phase")
            confirmed = agent_data.get("confirmed", False)
            parts = [f"  {agent_name}:"]
            if producer_phase:
                parts.append(f"producer={producer_phase}")
            if reviewer_phase:
                parts.append(f"reviewer={reviewer_phase}")
            if not producer_phase and not reviewer_phase:
                parts.append("phase=unknown")
            state_str = " ".join(parts)
            if confirmed:
                state_str += " [CONFIRMED]"
            print(state_str)

    blocking = consensus.get("blocking_agents", [])
    if blocking:
        print(f"\nBlocking agents: {', '.join(blocking)}")

    conditions = consensus.get("pre_merge_conditions") or []
    if conditions:
        print("\nPending pre-merge obligations:")
        for cond in conditions:
            reviewer = cond.get("reviewer", "?")
            producer = cond.get("producer", "?")
            text = cond.get("condition", "")
            print(f"  {reviewer} → {producer}: {text}")

    return 0


# ---------------------------------------------------------------------------
# Environment info
# ---------------------------------------------------------------------------


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
    role = args.role or get_agent_role_from_env()
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

    result = orch_request(endpoint)

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


def cmd_health_alerts(args: argparse.Namespace) -> int:
    """Get active health alerts for a pipeline."""
    pid = require_pipeline_id(args)

    result = orch_request(f"/api/v1/pipelines/{pid}/health/alerts")

    if args.json:
        print_json(result)
        return 0

    alerts = result.get("alerts", [])
    if not alerts:
        print("No active alerts.")
        return 0

    for alert in alerts:
        severity = alert.get("severity", "?").upper()
        alert_type = alert.get("alert_type", "?")
        agent = alert.get("agent_id", "?")
        message = alert.get("message", "")
        ts = alert.get("timestamp", "")[:19]
        print(f"  [{severity}] {alert_type} ({agent}) [{ts}]: {message}")

    print(f"\n{len(alerts)} alert(s)")
    return 0


def cmd_health_resolve(args: argparse.Namespace) -> int:
    """Resolve (remove) health alerts for a specific agent and alert type."""
    pid = require_pipeline_id(args)
    agent_id = args.agent_id
    alert_type = args.alert_type

    result = orch_request(
        f"/api/v1/pipelines/{pid}/health/alerts/resolve",
        method="POST",
        data={"agent_id": agent_id, "alert_type": alert_type},
    )

    if args.json:
        print_json(result)
        return 0

    if result.get("resolved"):
        print(f"Resolved {alert_type} alerts for {agent_id}.")
    else:
        print(f"Failed to resolve alerts: {result.get('error', 'unknown')}")
        return 1

    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Add --json flag to a subparser."""
    parser.add_argument("--json", action="store_true", help="Output raw JSON")


def _non_negative_int(value: str) -> int:
    """argparse type validator: reject negative ints, mirror PipelineConfig ge=0."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"{ivalue} must be >= 0 (use 0 to disable)")
    return ivalue


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-orch",
        description="CLI for the egg orchestrator and gateway APIs",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command group")

    # -- health --
    health_parser = subparsers.add_parser("health", help="Health check and alerts")
    health_sub = health_parser.add_subparsers(dest="health_command")

    # health check (default when no subcommand given)
    health_check_parser = health_sub.add_parser("check", help="Check orchestrator + gateway health")
    _add_json_flag(health_check_parser)
    health_check_parser.set_defaults(func=cmd_health)

    # health alerts
    health_alerts_parser = health_sub.add_parser("alerts", help="Get active health alerts")
    health_alerts_parser.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(health_alerts_parser)
    health_alerts_parser.set_defaults(func=cmd_health_alerts)

    # health resolve
    health_resolve_parser = health_sub.add_parser("resolve", help="Resolve health alerts")
    health_resolve_parser.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    health_resolve_parser.add_argument(
        "--agent-id", required=True, dest="agent_id", help="Agent ID"
    )
    health_resolve_parser.add_argument(
        "--alert-type", required=True, dest="alert_type", help="Alert type"
    )
    _add_json_flag(health_resolve_parser)
    health_resolve_parser.set_defaults(func=cmd_health_resolve)

    # Default: if no subcommand, run health check
    _add_json_flag(health_parser)
    health_parser.set_defaults(func=cmd_health)

    # -- env --
    env_parser = subparsers.add_parser("env", help="Show orchestrator environment variables")
    _add_json_flag(env_parser)
    env_parser.set_defaults(func=cmd_env)

    # -- pipeline --
    pipeline_parser = subparsers.add_parser("pipeline", help="Pipeline operations")
    pipeline_sub = pipeline_parser.add_subparsers(dest="pipeline_command")

    # pipeline list
    pl_list = pipeline_sub.add_parser("list", help="List pipelines")
    pl_list.add_argument("--status", help="Filter by status")
    pl_list.add_argument("--limit", type=int, help="Max results")
    _add_json_flag(pl_list)
    pl_list.set_defaults(func=cmd_pipeline_list)

    # pipeline get
    pl_get = pipeline_sub.add_parser("get", help="Get pipeline details")
    pl_get.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(pl_get)
    pl_get.set_defaults(func=cmd_pipeline_get)

    # pipeline create
    pl_create = pipeline_sub.add_parser("create", help="Create a pipeline")
    pl_create.add_argument("--repo", required=True, help="Repository (owner/name)")
    pl_create.add_argument("--issue", type=int, help="Issue number")
    pl_create.add_argument("--branch", help="Branch name")
    pl_create.add_argument("--prompt", help="Prompt (for prompt-driven pipelines)")
    pl_create.add_argument(
        "--network-mode",
        choices=["public", "private"],
        help="Network mode for spawned containers",
    )
    pl_create.add_argument(
        "--concurrent",
        action="store_true",
        default=False,
        help="Enable concurrent agent execution within phases",
    )
    _add_json_flag(pl_create)
    pl_create.set_defaults(func=cmd_pipeline_create)

    # pipeline status
    pl_status = pipeline_sub.add_parser("status", help="Get pipeline status")
    pl_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(pl_status)
    pl_status.set_defaults(func=cmd_pipeline_status)

    # pipeline delete
    pl_delete = pipeline_sub.add_parser("delete", help="Delete a pipeline")
    pl_delete.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(pl_delete)
    pl_delete.set_defaults(func=cmd_pipeline_delete)

    # pipeline wait-status — host-side blocking-wait CLI (issue #2211).
    # Counterpart to `egg-orch message wait-loop`. Loops the orchestrator's
    # /status/wait route, threads the cursor, emits JSON-lines on each
    # Path-A event; silent on Path-B. Exit codes per
    # docs/reference/agent-wait-patterns.md §3.
    pl_wait_status = pipeline_sub.add_parser(
        "wait-status",
        help="Long-poll for pipeline events; JSON-lines on stdout",
        description=(
            "Loops the orchestrator's /status/wait route server-side, "
            "threading the response cursor between calls. Emits one JSON "
            "line per pipeline-relevant event (phase transition, terminal "
            "state, HITL DECISION_CREATED, OVERSEER_ALERT, consensus "
            "message). Silent on no_change. Exits 0 on terminal pipeline "
            "state, 1 on --max-iterations cap (test only), 2 on transient "
            "errors after backoff budget, 3 on permanent errors (4xx). "
            "Use --since <cursor> to resume after a Bash-cap timeout."
        ),
    )
    pl_wait_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    pl_wait_status.add_argument(
        "--since",
        default="",
        help=(
            "Opaque cursor from a prior wait-status JSON-line. Empty / "
            "absent snaps to the tip of both event sources."
        ),
    )
    pl_wait_status.add_argument(
        "--inner-timeout",
        type=int,
        default=25,
        help=(
            "Per-call server-side block timeout in seconds (default 25, "
            "clamped server-side by GET_STATUS_MAX_WAIT)."
        ),
    )
    pl_wait_status.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Safety cap on outer-loop iterations (test harnesses only). "
            "Loops until terminal pipeline state by default."
        ),
    )
    # --json is intentionally NOT supported on wait-status: the loop emits
    # one JSON object per event already (JSON-lines on stdout); a --json
    # toggle would just re-print the last envelope and confuse the
    # streaming contract.
    pl_wait_status.set_defaults(func=cmd_pipeline_wait_status)

    # -- signal --
    signal_parser = subparsers.add_parser("signal", help="Send signals to orchestrator")
    signal_sub = signal_parser.add_subparsers(dest="signal_command")

    # Common signal args helper
    def add_signal_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
        p.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
        _add_json_flag(p)

    # signal complete
    sig_complete = signal_sub.add_parser("complete", help="Signal completion")
    add_signal_args(sig_complete)
    sig_complete.add_argument("--commit", help="Commit SHA")
    sig_complete.add_argument("--files", nargs="*", help="Changed files")
    sig_complete.set_defaults(func=cmd_signal_complete)

    # signal progress
    sig_progress = signal_sub.add_parser("progress", help="Signal progress")
    add_signal_args(sig_progress)
    sig_progress.add_argument(
        "--percent", type=int, required=True, help="Progress percentage (0-100)"
    )
    sig_progress.add_argument("--task", help="Current task description")
    sig_progress.add_argument("--message", help="Status message")
    sig_progress.set_defaults(func=cmd_signal_progress)

    # signal error
    sig_error = signal_sub.add_parser("error", help="Signal error")
    add_signal_args(sig_error)
    sig_error.add_argument("--error", required=True, help="Error message")
    sig_error.add_argument("--recoverable", action="store_true", help="Error is recoverable")
    sig_error.set_defaults(func=cmd_signal_error)

    # signal heartbeat
    sig_hb = signal_sub.add_parser("heartbeat", help="Send heartbeat")
    add_signal_args(sig_hb)
    sig_hb.set_defaults(func=cmd_signal_heartbeat)

    # signal readiness (concurrent mode)
    sig_ready = signal_sub.add_parser("readiness", help="Signal readiness state (concurrent mode)")
    add_signal_args(sig_ready)
    sig_ready.add_argument(
        "--state",
        required=True,
        choices=["WORKING", "READY", "BLOCKED", "OBJECTING"],
        help="Readiness state",
    )
    sig_ready.add_argument("--reason", help="Reason for state")
    sig_ready.set_defaults(func=cmd_signal_readiness)

    # -- message (concurrent mode) --
    msg_parser = subparsers.add_parser("message", help="Inter-agent messaging (concurrent mode)")
    msg_sub = msg_parser.add_subparsers(dest="message_command")

    # message send
    msg_send = msg_sub.add_parser("send", help="Send a message")
    msg_send.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_send.add_argument("--role", help="Sender role (default: EGG_AGENT_ROLE)")
    msg_send.add_argument("--to", required=True, help="Target role or 'all'")
    msg_send.add_argument(
        "--type",
        required=True,
        choices=["PROGRESS", "STATUS", "HANDOFF", "HEARTBEAT"],
        help=(
            "Message type (PROGRESS, STATUS, HANDOFF, HEARTBEAT). "
            "QUESTION was removed in issue #1897 — put clarifying "
            "questions in a NACK --reason block marked "
            '"### Non-blocking" so the producer sees them with the '
            "verdict.  For HEARTBEAT prefer the dedicated "
            "`message heartbeat` subcommand (schema validation + "
            "rate limit + dedup)."
        ),
    )
    msg_send.add_argument("--subject", help="Message subject")
    msg_send.add_argument("--body", help="Message body")
    _add_json_flag(msg_send)
    msg_send.set_defaults(func=cmd_message_send)

    # message poll
    msg_poll = msg_sub.add_parser("poll", help="Poll for messages")
    msg_poll.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_poll.add_argument("--role", help="Filter for role (default: EGG_AGENT_ROLE)")
    msg_poll.add_argument("--since", help="Return messages after this ID")
    msg_poll.add_argument("--limit", type=int, help="Max messages")
    msg_poll.add_argument(
        "--wait", type=int, help="Long-poll timeout in seconds (server holds connection)"
    )
    _add_json_flag(msg_poll)
    msg_poll.set_defaults(func=cmd_message_poll)

    # message wait — typed event-driven blocking primitive (issue #1897)
    msg_wait = msg_sub.add_parser(
        "wait",
        help="Block until a message of one or more types arrives",
        description=(
            "Block on a typed BRC event.  Exit 0 = matched, "
            "1 = timeout, 2 = transient (retry ok), "
            "3 = permanent.  Prefer this over shell retry loops."
        ),
    )
    msg_wait.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_wait.add_argument(
        "--for",
        dest="for_",
        action="append",
        required=True,
        help="Message type to wait for (repeatable, required)",
    )
    msg_wait.add_argument("--role", help="Filter for role (default: EGG_AGENT_ROLE)")
    msg_wait.add_argument("--from", dest="from_", help="Filter by sender role")
    msg_wait.add_argument("--since", help="Return messages after this ID")
    msg_wait.add_argument("--limit", type=int, help="Max messages")
    msg_wait.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Server-side block timeout in seconds (clamped by "
        "EGG_MESSAGE_POLL_MAX_WAIT, default 60)",
    )
    _add_json_flag(msg_wait)
    msg_wait.set_defaults(func=cmd_message_wait)

    # message wait-loop — canonical idiom for BRC stay-alive polling
    msg_wait_loop = msg_sub.add_parser(
        "wait-loop",
        help="Loop message wait until matched or max iterations reached",
        description=(
            "Convenience wrapper: call `message wait` in a loop until a "
            "match arrives (exit 0) or max-iterations / permanent error "
            "occurs (exit 1).  Agents should invoke this "
            "instead of shelling out their own while-loop."
        ),
    )
    msg_wait_loop.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_wait_loop.add_argument(
        "--for",
        dest="for_",
        action="append",
        required=True,
        help="Message type to wait for (repeatable, required)",
    )
    msg_wait_loop.add_argument("--role", help="Filter for role (default: EGG_AGENT_ROLE)")
    msg_wait_loop.add_argument("--from", dest="from_", help="Filter by sender role")
    msg_wait_loop.add_argument("--since", help="Return messages after this ID")
    msg_wait_loop.add_argument("--limit", type=int, help="Max messages")
    msg_wait_loop.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Per-call block timeout in seconds",
    )
    msg_wait_loop.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Safety cap on outer-loop iterations.  **Loops forever by "
            "default** (value is effectively unbounded unless set) so "
            "normal BRC consensus never trips it.  Set to a positive "
            "integer only for test harnesses or deterministic "
            "reproductions."
        ),
    )
    # --json is intentionally NOT supported on wait-loop: the loop calls
    # cmd_message_wait repeatedly, and each timeout iteration would print
    # a JSON object to stdout, producing concatenated invalid JSON.
    # Use ``egg-orch message wait --json`` directly for single-shot JSON.
    msg_wait_loop.set_defaults(func=cmd_message_wait_loop)

    # message heartbeat — emit a structured HEARTBEAT (issue #1897)
    msg_hb = msg_sub.add_parser(
        "heartbeat",
        help="Emit a structured HEARTBEAT state message",
        description=(
            "Emit a HEARTBEAT with a required --state "
            "(WORKING|WAITING_ON_ROLE|WAITING_FOR_EVENT|PROPOSED|IDLE). "
            "--state WAITING_ON_ROLE requires --waiting-on."
        ),
    )
    msg_hb.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_hb.add_argument("--role", help="Sender role (default: EGG_AGENT_ROLE)")
    msg_hb.add_argument(
        "--state",
        required=True,
        choices=[
            "WORKING",
            "WAITING_ON_ROLE",
            "WAITING_FOR_EVENT",
            "PROPOSED",
            "IDLE",
        ],
        help="Agent state",
    )
    msg_hb.add_argument(
        "--waiting-on",
        dest="waiting_on",
        help="Peer role the agent is waiting on (required for WAITING_ON_ROLE)",
    )
    msg_hb.add_argument(
        "--since",
        help="Optional ISO-8601 / epoch timestamp naming when the current state began",
    )
    msg_hb.add_argument("--body", help="Free-form body text")
    _add_json_flag(msg_hb)
    msg_hb.set_defaults(func=cmd_message_heartbeat)

    # message status
    msg_status = msg_sub.add_parser("status", help="Message bus status")
    msg_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(msg_status)
    msg_status.set_defaults(func=cmd_message_status)

    # -- consensus (BRC protocol) --
    consensus_parser = subparsers.add_parser("consensus", help="BRC consensus protocol commands")
    consensus_sub = consensus_parser.add_subparsers(dest="consensus_command")

    # consensus propose
    cons_propose = consensus_sub.add_parser("propose", help="Send consensus proposal")
    cons_propose.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_propose.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    cons_propose.add_argument("--file", help="JSON file with proposal payload")
    cons_propose.add_argument("--summary", help="Proposal summary")
    cons_propose.add_argument("--artifacts", nargs="*", help="Artifact paths")
    cons_propose.add_argument("--risk", help="Risk considerations")
    cons_propose.add_argument(
        "--commit-sha",
        default=None,
        help="Commit SHA pushed to the remote branch (defaults to HEAD)",
    )
    cons_propose.add_argument(
        "--changed-artifacts",
        nargs="*",
        help="Changed artifacts (for re-proposals after NACK)",
    )
    cons_propose.add_argument("--files-changed", nargs="*", help="Files changed in this proposal")
    cons_propose.add_argument("--tests-run", nargs="*", help="Tests executed for this proposal")
    cons_propose.add_argument(
        "--tasks", nargs="*", help="Contract tasks satisfied by this proposal"
    )
    cons_propose.add_argument(
        "--push",
        action="store_true",
        help="Run git push before sending the proposal (bundles push+propose "
        "so auto-repropose is suppressed)",
    )
    _add_json_flag(cons_propose)
    cons_propose.set_defaults(func=cmd_consensus_propose)

    # consensus ack
    cons_ack = consensus_sub.add_parser("ack", help="ACK a producer's proposal")
    cons_ack.add_argument("producer_role", help="Producer role to ACK")
    cons_ack.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_ack.add_argument("--role", help="Reviewer role (default: EGG_AGENT_ROLE)")
    cons_ack.add_argument(
        "--files-reviewed",
        nargs="+",
        required=True,
        help="Artifact references (files, commits) reviewed",
    )
    cons_ack.add_argument(
        "--reason",
        required=True,
        help="Substantive rationale: what was read, what was checked, why the verdict follows",
    )
    cons_ack.add_argument(
        "--ack-version",
        dest="ack_version",
        type=_proposal_version_type,
        required=True,
        help=(
            "The producer's proposal version you reviewed (must be >= 1). "
            "The orchestrator rejects the ACK with HTTP 409 (stale_version) "
            "if the producer has since re-proposed (#2142). Read it from the "
            "CONSENSUS_PROPOSE message you waited on, or from "
            "`egg-orch consensus status --json`."
        ),
    )
    cons_ack.add_argument(
        "--pre-merge-condition",
        dest="pre_merge_condition",
        default="",
        help=(
            "Optional: mark this as a conditional ACK (#1998). The work is "
            "approved but the named action must be performed by a human "
            "before merging (e.g. 'git mv old/path new/path'). Surfaces as "
            "a Pre-merge Obligations section on the auto-created PR."
        ),
    )
    _add_json_flag(cons_ack)
    cons_ack.set_defaults(func=cmd_consensus_ack)

    # consensus nack
    cons_nack = consensus_sub.add_parser("nack", help="NACK a producer's proposal")
    cons_nack.add_argument("producer_role", help="Producer role to NACK")
    cons_nack.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_nack.add_argument("--role", help="Reviewer role (default: EGG_AGENT_ROLE)")
    cons_nack.add_argument("--reason", required=True, help="Reason for NACK")
    cons_nack.add_argument(
        "--files-reviewed",
        nargs="+",
        required=True,
        help="Artifact references (files, commits) reviewed",
    )
    cons_nack.add_argument(
        "--nack-version",
        dest="nack_version",
        type=_proposal_version_type,
        required=True,
        help=(
            "The producer's proposal version you reviewed (must be >= 1). "
            "The orchestrator rejects the NACK with HTTP 409 (stale_version) "
            "if the producer has since re-proposed (#2142). Read it from the "
            "CONSENSUS_PROPOSE message you waited on, or from "
            "`egg-orch consensus status --json`."
        ),
    )
    _add_json_flag(cons_nack)
    cons_nack.set_defaults(func=cmd_consensus_nack)

    # consensus withdraw
    cons_withdraw = consensus_sub.add_parser("withdraw", help="Withdraw proposal")
    cons_withdraw.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_withdraw.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    cons_withdraw.add_argument("--reason", required=True, help="Reason for withdrawal")
    _add_json_flag(cons_withdraw)
    cons_withdraw.set_defaults(func=cmd_consensus_withdraw)

    # consensus confirmed
    cons_confirmed = consensus_sub.add_parser("confirmed", help="Confirm after all reviewers ACK")
    cons_confirmed.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_confirmed.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    _add_json_flag(cons_confirmed)
    cons_confirmed.set_defaults(func=cmd_consensus_confirmed)

    # consensus status
    cons_status = consensus_sub.add_parser("status", help="Show consensus status")
    cons_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(cons_status)
    cons_status.set_defaults(func=cmd_consensus_status)

    # -- phase --
    phase_parser = subparsers.add_parser("phase", help="Phase operations")
    phase_sub = phase_parser.add_subparsers(dest="phase_command")

    # phase get
    ph_get = phase_sub.add_parser("get", help="Get current phase")
    ph_get.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(ph_get)
    ph_get.set_defaults(func=cmd_phase_get)

    # phase advance
    ph_advance = phase_sub.add_parser("advance", help="Advance to next phase")
    ph_advance.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ph_advance.add_argument(
        "--target-phase",
        required=True,
        choices=["refine", "plan", "implement", "pr"],
        help="Target phase to advance to",
    )
    ph_advance.add_argument("--reason", help="Reason for advancement")
    _add_json_flag(ph_advance)
    ph_advance.set_defaults(func=cmd_phase_advance)

    # phase start
    ph_start = phase_sub.add_parser("start", help="Start current phase")
    ph_start.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(ph_start)
    ph_start.set_defaults(func=cmd_phase_start)

    # phase complete
    ph_complete = phase_sub.add_parser("complete", help="Complete current phase")
    ph_complete.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ph_complete.add_argument("--reason", help="Completion reason")
    _add_json_flag(ph_complete)
    ph_complete.set_defaults(func=cmd_phase_complete)

    # -- decision --
    decision_parser = subparsers.add_parser("decision", help="Decision queue operations")
    decision_sub = decision_parser.add_subparsers(dest="decision_command")

    # decision list
    dec_list = decision_sub.add_parser("list", help="List decisions")
    dec_list.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(dec_list)
    dec_list.set_defaults(func=cmd_decision_list)

    # decision create
    dec_create = decision_sub.add_parser("create", help="Queue a decision")
    dec_create.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    dec_create.add_argument("--question", required=True, help="Decision question")
    dec_create.add_argument("--context", help="Additional context")
    dec_create.add_argument("--options", nargs="*", help="Decision options")
    dec_create.add_argument(
        "--phase",
        choices=["refine", "plan", "implement", "pr"],
        help="Pipeline phase (auto-inferred from pipeline state if omitted)",
    )
    dec_create.add_argument(
        "--decision-type",
        dest="decision_type",
        choices=["phase_gate", "choice", "feedback"],
        default=None,
        help="Decision type (default: choice). phase_gate is typically created by the orchestrator but can be used for manual debugging/recovery.",
    )
    _add_json_flag(dec_create)
    dec_create.set_defaults(func=cmd_decision_create)

    # decision resolve
    dec_resolve = decision_sub.add_parser("resolve", help="Resolve a decision")
    dec_resolve.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    dec_resolve.add_argument("decision_id", help="Decision ID")
    dec_resolve.add_argument("--resolution", required=True, help="Resolution value")
    dec_resolve.add_argument("--resolved-by", help="Who resolved it")
    _add_json_flag(dec_resolve)
    dec_resolve.set_defaults(func=cmd_decision_resolve)

    # decision status
    dec_status = decision_sub.add_parser("status", help="Decision queue status")
    dec_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(dec_status)
    dec_status.set_defaults(func=cmd_decision_status)

    # -- container --
    container_parser = subparsers.add_parser("container", help="Container operations")
    container_sub = container_parser.add_subparsers(dest="container_command")

    # container list
    ct_list = container_sub.add_parser("list", help="List containers")
    ct_list.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(ct_list)
    ct_list.set_defaults(func=cmd_container_list)

    # container spawn
    ct_spawn = container_sub.add_parser("spawn", help="Spawn a container")
    ct_spawn.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_spawn.add_argument("--role", required=True, help="Agent role")
    ct_spawn.add_argument("--issue", type=int, help="Issue number")
    ct_spawn.add_argument("--private", action="store_true", help="Private mode")
    _add_json_flag(ct_spawn)
    ct_spawn.set_defaults(func=cmd_container_spawn)

    # container get
    ct_get = container_sub.add_parser("get", help="Get container info")
    ct_get.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_get.add_argument("container_id", help="Container ID")
    _add_json_flag(ct_get)
    ct_get.set_defaults(func=cmd_container_get)

    # container stop
    ct_stop = container_sub.add_parser("stop", help="Stop a container")
    ct_stop.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_stop.add_argument("container_id", help="Container ID")
    _add_json_flag(ct_stop)
    ct_stop.set_defaults(func=cmd_container_stop)

    # container logs
    ct_logs = container_sub.add_parser("logs", help="Get container logs")
    ct_logs.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_logs.add_argument("container_id", help="Container ID")
    ct_logs.add_argument("--lines", type=int, help="Number of log lines")
    _add_json_flag(ct_logs)
    ct_logs.set_defaults(func=cmd_container_logs)

    # -- gateway --
    gw_parser = subparsers.add_parser("gateway", help="Gateway operations")
    gw_sub = gw_parser.add_subparsers(dest="gateway_command")

    # gateway health
    gw_health = gw_sub.add_parser("health", help="Check gateway health")
    _add_json_flag(gw_health)
    gw_health.set_defaults(func=cmd_gateway_health)

    # gateway phase
    gw_phase = gw_sub.add_parser("phase", help="Get current phase from gateway")
    gw_phase.add_argument("--issue", type=int, help="Issue number")
    _add_json_flag(gw_phase)
    gw_phase.set_defaults(func=cmd_gateway_phase)

    # gateway permissions
    gw_perms = gw_sub.add_parser("permissions", help="Get allowed operations for a phase")
    gw_perms.add_argument(
        "phase",
        choices=["refine", "plan", "implement", "pr"],
        help="SDLC phase",
    )
    _add_json_flag(gw_perms)
    gw_perms.set_defaults(func=cmd_gateway_permissions)

    # -- progress (structured progress tracking) --
    progress_parser = subparsers.add_parser("progress", help="Structured progress event commands")
    progress_sub = progress_parser.add_subparsers(dest="progress_command")

    # progress emit
    prog_emit = progress_sub.add_parser("emit", help="Emit a structured progress event")
    prog_emit.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    prog_emit.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    prog_emit.add_argument("--step", required=True, help="Description of current step")
    prog_emit.add_argument(
        "--state",
        required=True,
        choices=["working", "blocked", "complete"],
        help="Progress state",
    )
    prog_emit.add_argument("--detail", help="Additional detail about the step")
    prog_emit.add_argument("--blocker", help="Description of blocker (when state=blocked)")
    _add_json_flag(prog_emit)
    prog_emit.set_defaults(func=cmd_progress_emit)

    # progress query
    prog_query = progress_sub.add_parser("query", help="Query progress events")
    prog_query.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    prog_query.add_argument("--agent", help="Filter by agent role")
    prog_query.add_argument("--since", help="Filter events after this ISO timestamp")
    prog_query.add_argument("--limit", type=int, help="Max events to return")
    _add_json_flag(prog_query)
    prog_query.set_defaults(func=cmd_progress_query)

    # -- overseer --
    overseer_parser = subparsers.add_parser(
        "overseer",
        help="Overseer-only operations (anomaly escalation)",
    )
    overseer_sub = overseer_parser.add_subparsers(dest="overseer_command")

    # overseer alert
    ov_alert = overseer_sub.add_parser(
        "alert",
        help="Broadcast an OVERSEER_ALERT to the human operator",
        description=(
            "Emit an OVERSEER_ALERT message that the human-facing alert "
            "surfaces watch for. Always sends with message_type=OVERSEER_ALERT "
            "and to_role=all. Use this whenever you observe an anomaly that "
            "requires human attention -- never use 'message send --type "
            "HANDOFF/STATUS' for anomaly escalation, those types blend into "
            "normal inter-agent traffic."
        ),
    )
    ov_alert.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ov_alert.add_argument("--role", help="Sender role (default: EGG_AGENT_ROLE or 'overseer')")
    ov_alert.add_argument(
        "--anomaly",
        required=True,
        help=(
            "Anomaly type -- intentionally free-text so new types can emerge "
            "without CLI changes. Known types: stuck-phase-transition, "
            "agent-heartbeat-stall, agent-loop, orchestrator-consensus-silent, "
            "unauthorized-overseer-action, unmediated-disagreement. NOTE: "
            "'unmediated-disagreement' is for observers (overseer/mediator) "
            "flagging that no one is adjudicating; producers blocked by "
            "reviewer NACKs naming an operator-decidable scope question "
            "should use 'egg-contract add-decision' / "
            "'mcp__sdlc__register_open_question' instead -- alerts are "
            "informational, decisions are HITL gates."
        ),
    )
    ov_alert.add_argument(
        "--priority",
        required=True,
        choices=["low", "medium", "high"],
        help="Alert priority",
    )
    ov_alert.add_argument(
        "--summary",
        required=True,
        help="One-line summary of what was observed",
    )
    ov_alert.add_argument("--detail", help="Longer description / observed evidence")
    ov_alert.add_argument(
        "--recommend",
        help="What you'd recommend the human do (optional, for context)",
    )
    # Issue #1962: structured advisor recommendation. Surfaces in /sdlc
    # as a HITL decision; the human gates the actual action (file_issue).
    ov_alert.add_argument(
        "--recommendation",
        choices=["file_issue"],
        help=(
            "Structured advisor recommendation (issue #1962). Currently "
            "the only legal value is 'file_issue'. The human gates the "
            "actual filing via the existing pending_decisions HITL flow."
        ),
    )
    ov_alert.add_argument(
        "--recommendation-payload-file",
        help=(
            "Path to a JSON file containing the recommendation payload "
            "(e.g. composed issue_title + issue_body + priority + "
            "anomaly_signature). Required when --recommendation is set. "
            "Bounded at 50 KB."
        ),
    )
    _add_json_flag(ov_alert)
    ov_alert.set_defaults(func=cmd_overseer_alert)

    # overseer file-issue (issue #1962, decision-9 opt-1)
    ov_file = overseer_sub.add_parser(
        "file-issue",
        help="File a GitHub issue from the overseer role (advisor-gated)",
        description=(
            "Run `gh issue create` itself, inside the sandbox, mediated "
            "by the gateway. Looks up an existing open issue with the "
            "same anomaly signature first; if found, prints "
            "{filed: false, dedup_match: <number>} and exits 0 without "
            "calling gh. On a fresh filing, appends a FiledIssueRecord "
            "to .egg-state/oversight/filed-issues.jsonl and prints "
            "{filed: true, issue_number: <number>}."
        ),
    )
    ov_file.add_argument(
        "--anomaly-type",
        required=True,
        help="Stable kebab-case anomaly identifier (e.g. agent-loop)",
    )
    ov_file.add_argument(
        "--priority",
        required=True,
        choices=list(_OVERSEER_VALID_LABEL_PRIORITIES),
        help="GitHub label priority (p0|p1|p2|p3)",
    )
    ov_file.add_argument(
        "--agent-role",
        required=True,
        help="Affected agent role (e.g. coder, refiner)",
    )
    ov_file.add_argument(
        "--anomaly-signature",
        required=True,
        help=(
            "16-hex anomaly signature (egg_overseer.state."
            "compute_anomaly_signature output). The first 8 chars "
            "embed in the issue title for cross-phase dedup."
        ),
    )
    ov_file.add_argument(
        "--issue-title-file",
        required=True,
        help="Path to a sandbox-local file containing the issue title",
    )
    ov_file.add_argument(
        "--issue-body-file",
        required=True,
        help="Path to a sandbox-local file containing the issue body",
    )
    ov_file.add_argument(
        "--parent-alert-message-id",
        help="ID of the parent OVERSEER_ALERT message",
    )
    ov_file.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the composed gh argv + JSON without calling gh",
    )
    _add_json_flag(ov_file)
    ov_file.set_defaults(func=cmd_overseer_file_issue)

    # overseer consult-advisor (issue #1962, EGG200 boundary fix)
    ov_advisor = overseer_sub.add_parser(
        "consult-advisor",
        help="Consult the Opus advisor for a structured verdict (sandbox-side LLM call)",
        description=(
            "Run egg_overseer.advisor.consult_advisor inside the "
            "sandbox so the Opus run_agent_async invocation lives on "
            "the LLM-execution side of the EGG200 boundary. The "
            "orchestrator pod never touches Anthropic credentials. "
            "Reads the inputs (Haiku classification + Tier-1 health "
            "alerts + optional progress events / log lines) from a "
            "JSON file and writes the validated AdvisorVerdict JSON "
            "to --output-file (or stdout when omitted)."
        ),
    )
    ov_advisor.add_argument(
        "pipeline_id",
        nargs="?",
        help=(
            "Optional pipeline ID. When provided (or EGG_PIPELINE_ID is "
            "set), the verb reads PipelineConfig.overseer_advisor_model "
            "from the orchestrator status endpoint and passes the "
            "configured alias to consult_advisor. Omitted: falls back "
            "to the 'opus' default."
        ),
    )
    ov_advisor.add_argument(
        "--inputs-file",
        required=True,
        help=(
            "Path to a JSON file with keys: classification (object), "
            "health_alerts (array), progress_events (array, optional), "
            "recent_log_lines (array, optional)."
        ),
    )
    ov_advisor.add_argument(
        "--output-file",
        help=(
            "Path to write the AdvisorVerdict JSON. When omitted the "
            "verdict is written to stdout (pretty-printed). With "
            "--output-file, --json additionally tees the verdict JSON "
            "to stdout; without --output-file, --json is a no-op "
            "since stdout is already JSON."
        ),
    )
    ov_advisor.add_argument(
        "--recent-log-bytes-cap",
        type=_non_negative_int,
        default=None,
        help=(
            "Byte cap for the recent_log_lines block in the advisor "
            "prompt (issue #2120). When omitted, consult_advisor uses "
            "the PipelineConfig value or its 256 KiB default. 0 "
            "disables the cap (not recommended). Negative values are "
            "rejected (matches PipelineConfig ge=0)."
        ),
    )
    _add_json_flag(ov_advisor)
    ov_advisor.set_defaults(func=cmd_overseer_consult_advisor)

    # --- push ---
    from egg_lib.cli_push import register_push_subcommand

    register_push_subcommand(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Handle subcommand groups that need their own help
    func = getattr(args, "func", None)
    if func is None:
        # No subcommand selected within the group
        sub = args.command
        # Re-parse to show the right help
        parser.parse_args([sub, "--help"])
        return 1

    result: int = func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
