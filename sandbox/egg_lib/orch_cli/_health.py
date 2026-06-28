"""Health and gateway-introspection subcommands (health, gateway *, health alerts/resolve).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import sys
from typing import Any
from urllib.parse import quote

from egg_lib import orch_cli as _pkg

from ._http import (
    ApiError,
    gateway_request,
    get_gateway_url,
    get_issue_number,
    get_orchestrator_url,
    print_json,
)


def cmd_health(args: argparse.Namespace) -> int:
    """Check orchestrator and gateway health."""
    health_data: dict[str, Any] = {"orchestrator": {}, "gateway": {}}
    errors = 0

    # Orchestrator health
    if not args.json:
        print("Orchestrator:")
    try:
        result = _pkg.api_request(get_orchestrator_url(), "/api/v1/health", timeout=5)
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
                if name == "state_store" and isinstance(state, dict):
                    # Per-repo map (#2176): print one line per repo so the
                    # human-text path stays readable even with many repos.
                    # The aggregate string lives in `state_store_summary`.
                    print(f"  {name}:")
                    for repo_path, repo_state in state.items():
                        if isinstance(repo_state, dict):
                            status = repo_state.get("status", "unknown")
                            error = repo_state.get("error")
                            if error:
                                print(f"    {repo_path}: {status} ({error})")
                            else:
                                print(f"    {repo_path}: {status}")
                        else:
                            print(f"    {repo_path}: {repo_state}")
                else:
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
        result = _pkg.api_request(get_gateway_url(), "/api/v1/health", timeout=5)
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


def cmd_gateway_health(args: argparse.Namespace) -> int:
    """Check gateway health."""
    try:
        result = _pkg.api_request(get_gateway_url(), "/api/v1/health", timeout=5)
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


def cmd_health_alerts(args: argparse.Namespace) -> int:
    """Get active health alerts for a pipeline."""
    pid = _pkg.require_pipeline_id(args)

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/health/alerts")

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
    pid = _pkg.require_pipeline_id(args)
    agent_id = args.agent_id
    alert_type = args.alert_type

    result = _pkg.orch_request(
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
