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
    egg-orch message status <pid>                Get message bus status (concurrent mode)
    egg-orch signal readiness <pid> --state ...  Signal readiness state (concurrent mode)
    egg-orch consensus propose <pid> ...         Send BRC consensus proposal
    egg-orch consensus ack <producer> ...        ACK a producer's proposal
    egg-orch consensus nack <producer> ...       NACK a producer's proposal
    egg-orch consensus withdraw <pid> ...        Withdraw proposal
    egg-orch consensus confirmed <pid>           Confirm after all reviewers ACK
    egg-orch consensus status <pid>              Show BRC consensus status
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
    """Make a request to the orchestrator API."""
    return api_request_or_exit(get_orchestrator_url(), endpoint, method, data, timeout)


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
    """Get pipeline status."""
    pid = require_pipeline_id(args)
    result = orch_request(f"/api/v1/pipelines/{pid}/status")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    print(f"Pipeline: {pid}")
    print(f"Status:   {data.get('status')}")
    print(f"Phase:    {data.get('current_phase')}")
    pending = data.get("pending_decisions", 0)
    if pending:
        print(f"Pending decisions: {pending}")
    if data.get("updated_at"):
        print(f"Updated: {data.get('updated_at')}")
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
    """Signal progress update."""
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
    """Signal an error."""
    pid = require_pipeline_id(args)
    role = _require_role(args)
    data: dict[str, Any] = {
        "signal_type": "error",
        "agent_role": role,
        "error": args.error,
        "recoverable": args.recoverable,
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Signaled error for {role}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_signal_heartbeat(args: argparse.Namespace) -> int:
    """Send heartbeat."""
    pid = require_pipeline_id(args)
    role = _require_role(args)
    data: dict[str, Any] = {
        "signal_type": "heartbeat",
        "agent_role": role,
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print("Heartbeat sent")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


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
        phase_data = result.get("data", {})
        new_phase = phase_data.get("current_phase", phase_data.get("phase", "?"))
        print(f"Advanced to phase: {new_phase}")
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
        print("Phase started")
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
        print("Phase completed")
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
        params["lines"] = str(args.lines)

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
            print(f"    {body[:200]}")

    print(f"\n{len(messages)} message(s)")
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


def cmd_consensus_propose(args: argparse.Namespace) -> int:
    """Send CONSENSUS_PROPOSE signal."""
    pid = require_pipeline_id(args)
    role = _require_role(args)

    # Read payload from file or construct from args
    payload: dict[str, Any]
    if getattr(args, "file", None):
        with open(args.file) as f:
            payload = json.load(f)
    else:
        payload = {
            "summary": getattr(args, "summary", "") or "",
            "attestation": {},
            "artifacts": getattr(args, "artifacts", []) or [],
            "risk_considered": getattr(args, "risk", "") or "",
        }

    changed_artifacts = getattr(args, "changed_artifacts", None)

    data: dict[str, Any] = {
        "signal_type": "consensus_propose",
        "agent_role": role,
        "payload": payload,
    }
    if changed_artifacts:
        data["changed_artifacts"] = changed_artifacts

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Proposal sent by {role}")
        consensus = result.get("data", {}).get("consensus", {})
        phase = consensus.get("agents", {}).get(role, {}).get("phase", "")
        if phase:
            print(f"  BRC phase: {phase}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_consensus_ack(args: argparse.Namespace) -> int:
    """Send CONSENSUS_ACK signal for a producer."""
    pid = require_pipeline_id(args)
    role = _require_role(args)

    data: dict[str, Any] = {
        "signal_type": "consensus_ack",
        "agent_role": role,
        "producer_role": args.producer_role,
        "payload": {
            "artifact_references": args.files_reviewed,
        },
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"ACK sent by {role} for {args.producer_role}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_consensus_nack(args: argparse.Namespace) -> int:
    """Send CONSENSUS_NACK signal for a producer."""
    pid = require_pipeline_id(args)
    role = _require_role(args)

    data: dict[str, Any] = {
        "signal_type": "consensus_nack",
        "agent_role": role,
        "producer_role": args.producer_role,
        "payload": {
            "reason": args.reason,
            "artifact_references": args.files_reviewed,
        },
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"NACK sent by {role} for {args.producer_role}: {args.reason}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


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
    """Send CONSENSUS_CONFIRMED signal after all reviewers ACK."""
    pid = require_pipeline_id(args)
    role = _require_role(args)

    data: dict[str, Any] = {
        "signal_type": "consensus_confirmed",
        "agent_role": role,
    }

    result = orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        data = result.get("data", {})
        # 202: producer is waiting for reviewer re-ACKs after re-proposal
        if data.get("status") == "pending_acks":
            print(f"Waiting for reviewer re-ACKs: {result.get('message')}")
            return 2
        consensus_reached = data.get("consensus_reached", False)
        print(f"Confirmation recorded for {role}")
        if consensus_reached:
            print("  Consensus reached!")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_consensus_status(args: argparse.Namespace) -> int:
    """Show BRC consensus status (approval matrix and review graph)."""
    pid = require_pipeline_id(args)

    result = orch_request(f"/api/v1/pipelines/{pid}/status")

    consensus = result.get("data", {}).get("concurrent", {}).get("consensus", {})

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
    """Emit a structured progress event."""
    pid = require_pipeline_id(args)
    role = args.role or get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        sys.exit(1)

    data: dict[str, Any] = {
        "agent_role": role,
        "step": args.step,
        "state": args.state,
    }
    if args.detail:
        data["detail"] = args.detail
    if args.blocker:
        data["blocker"] = args.blocker

    result = orch_request(f"/api/v1/pipelines/{pid}/progress", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        event = result.get("data", {}).get("event", {})
        print(f"Progress emitted: {event.get('id', 'unknown')} [{args.state}] {args.step}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


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


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Add --json flag to a subparser."""
    parser.add_argument("--json", action="store_true", help="Output raw JSON")


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
    msg_send.add_argument("--type", required=True, help="Message type (PROGRESS, QUESTION, STATUS)")
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
        "--changed-artifacts",
        nargs="*",
        help="Changed artifacts (for re-proposals after NACK)",
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
    progress_parser = subparsers.add_parser(
        "progress", help="Structured progress event commands"
    )
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
