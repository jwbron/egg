"""Multi-agent orchestration command handlers.

The ``cmd_agent_*`` functions plus the role/status constants, extracted
verbatim from the monolithic ``contract_cli.py`` (#3312, slice-1). No
behaviour change.
"""

import argparse
import json
import sys
from typing import Any
from urllib.parse import urlencode

from egg_contracts.agent_roles import AgentRole, get_role_definition

from ._config import (
    _container_id_field,
    get_container_id,
    get_contract_identifier,
    get_repo_path,
    validate_commit_sha,
)
from ._gateway import make_gateway_request

VALID_AGENT_ROLES = ["coder", "tester", "documenter"]
VALID_AGENT_STATUSES = ["pending", "running", "complete", "failed", "skipped", "blocked"]


def cmd_agent_status(args: argparse.Namespace) -> int:
    """Show agent execution status for multi-agent orchestration."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    result = make_gateway_request(endpoint)

    if not result.get("success"):
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1

    contract = result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    if args.json:
        # Output JSON format
        output = {
            "agent_executions": agent_executions,
        }
        print(json.dumps(output, indent=2))
    else:
        # Human-readable format
        if not agent_executions:
            print("No agent executions found. Multi-agent mode may not be enabled.")
            return 0

        print(f"Agent Executions for {identifier}:")
        print()

        for execution in agent_executions:
            role = execution.get("role", "unknown")
            status = execution.get("status", "pending")
            commit = execution.get("commit")
            error = execution.get("error")
            retry_count = execution.get("retry_count", 0)

            status_icon = {
                "pending": "○",
                "running": "◐",
                "complete": "●",
                "failed": "✗",
                "skipped": "⊘",
                "blocked": "⊘",
            }.get(status, "?")

            line = f"  {status_icon} {role}: {status}"
            if commit:
                line += f" (commit: {commit[:7]})"
            if retry_count > 0:
                line += f" [retries: {retry_count}]"
            print(line)

            if error:
                print(f"      Error: {error}")

        # Show summary
        print()
        pending = sum(1 for e in agent_executions if e.get("status") == "pending")
        running = sum(1 for e in agent_executions if e.get("status") == "running")
        complete = sum(1 for e in agent_executions if e.get("status") == "complete")
        failed = sum(1 for e in agent_executions if e.get("status") == "failed")

        print(
            f"Summary: {complete} complete, {running} running, {pending} pending, {failed} failed"
        )

    return 0


def cmd_agent_start(args: argparse.Namespace) -> int:
    """Mark an agent as started (running)."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    role = args.role.lower()
    if role not in VALID_AGENT_ROLES:
        print(
            f"Error: Invalid agent role '{role}'. Valid roles: {', '.join(VALID_AGENT_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # Get current contract to find agent execution index
    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    # Find the execution for this role
    exec_idx = None
    for i, execution in enumerate(agent_executions):
        if execution.get("role") == role:
            exec_idx = i
            break

    if exec_idx is None:
        print(f"Error: No agent execution found for role '{role}'", file=sys.stderr)
        return 1

    # Update status to running
    result = make_gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": args.repo_path or get_repo_path(),
            "field_path": f"agent_executions.{exec_idx}.status",
            "new_value": "running",
            "actor": "egg",
            "reason": f"Started {role} agent",
            **_container_id_field(),
        },
    )

    if result.get("success"):
        print(f"Started agent: {role}")
        return 0
    else:
        print(f"Error: {result.get('message')}", file=sys.stderr)
        return 1


def cmd_agent_complete(args: argparse.Namespace) -> int:
    """Mark an agent as complete."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    role = args.role.lower()
    if role not in VALID_AGENT_ROLES:
        print(
            f"Error: Invalid agent role '{role}'. Valid roles: {', '.join(VALID_AGENT_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # Get current contract to find agent execution index
    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    # Find the execution for this role
    exec_idx = None
    for i, execution in enumerate(agent_executions):
        if execution.get("role") == role:
            exec_idx = i
            break

    if exec_idx is None:
        print(f"Error: No agent execution found for role '{role}'", file=sys.stderr)
        return 1

    # Build updates
    updates = [
        {
            "field_path": f"agent_executions.{exec_idx}.status",
            "new_value": "complete",
        }
    ]

    # Add commit if provided
    if args.commit:
        try:
            validate_commit_sha(args.commit)
            updates.append(
                {
                    "field_path": f"agent_executions.{exec_idx}.commit",
                    "new_value": args.commit,
                }
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Apply updates
    for update in updates:
        result = make_gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": args.repo_path or get_repo_path(),
                "field_path": update["field_path"],
                "new_value": update["new_value"],
                "actor": "egg",
                "reason": f"Completed {role} agent",
                **_container_id_field(),
            },
        )

        if not result.get("success"):
            print(f"Error: {result.get('message')}", file=sys.stderr)
            return 1

    commit_info = f" (commit: {args.commit[:7]})" if args.commit else ""
    print(f"Completed agent: {role}{commit_info}")
    return 0


def cmd_agent_fail(args: argparse.Namespace) -> int:
    """Mark an agent as failed."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    role = args.role.lower()
    if role not in VALID_AGENT_ROLES:
        print(
            f"Error: Invalid agent role '{role}'. Valid roles: {', '.join(VALID_AGENT_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # Get current contract to find agent execution index
    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    # Find the execution for this role
    exec_idx = None
    for i, execution in enumerate(agent_executions):
        if execution.get("role") == role:
            exec_idx = i
            break

    if exec_idx is None:
        print(f"Error: No agent execution found for role '{role}'", file=sys.stderr)
        return 1

    # Update status and error
    updates = [
        {
            "field_path": f"agent_executions.{exec_idx}.status",
            "new_value": "failed",
        },
        {
            "field_path": f"agent_executions.{exec_idx}.error",
            "new_value": args.error,
        },
    ]

    for update in updates:
        result = make_gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": args.repo_path or get_repo_path(),
                "field_path": update["field_path"],
                "new_value": update["new_value"],
                "actor": "egg",
                "reason": f"Failed {role} agent: {args.error[:50]}",
                **_container_id_field(),
            },
        )

        if not result.get("success"):
            print(f"Error: {result.get('message')}", file=sys.stderr)
            return 1

    print(f"Marked agent as failed: {role}")
    print(f"  Error: {args.error}")
    return 0


def cmd_agent_next(args: argparse.Namespace) -> int:
    """Get the next wave of agents to dispatch."""
    identifier = get_contract_identifier(args)
    if identifier is None:
        print(
            "Error: Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or use --issue/--pipeline-id",
            file=sys.stderr,
        )
        return 1

    endpoint = f"/api/v1/contract/{identifier}"
    params = {}
    if args.repo_path:
        params["repo_path"] = args.repo_path
    container_id = get_container_id()
    if container_id:
        params["container_id"] = container_id
    if params:
        endpoint += "?" + urlencode(params)

    contract_result = make_gateway_request(endpoint)
    if not contract_result.get("success"):
        print(f"Error: {contract_result.get('message')}", file=sys.stderr)
        return 1

    contract = contract_result.get("data", {})
    agent_executions = contract.get("agent_executions", [])

    if not agent_executions:
        print("No agent executions configured. Multi-agent mode may not be enabled.")
        return 0

    # Compute next dispatch locally
    # This mirrors the logic in orchestrator.py but runs client-side
    pending = []
    running = []
    complete = []
    failed = []

    for execution in agent_executions:
        status = execution.get("status", "pending")
        role = execution.get("role")
        if status == "pending":
            pending.append(role)
        elif status == "running":
            running.append(role)
        elif status == "complete":
            complete.append(role)
        elif status == "failed":
            failed.append(role)

    if failed:
        output: dict[str, Any] = {
            "status": "failed",
            "agents": [],
            "reason": f"Agents failed: {', '.join(failed)}",
            "failed_agents": failed,
        }
    elif not pending and not running:
        output = {
            "status": "complete",
            "agents": [],
            "reason": "All agents have completed",
        }
    elif running:
        output = {
            "status": "waiting",
            "agents": [],
            "reason": f"Waiting for running agents: {', '.join(running)}",
            "running_agents": running,
        }
    else:
        # Determine which pending agents can run based on dependencies
        # Use canonical dependency definitions from agent_roles module
        runnable = []
        for role in pending:
            try:
                role_enum = AgentRole(role)
                role_def = get_role_definition(role_enum)
                # Check if all dependencies are complete
                deps_satisfied = all(dep.value in complete for dep in role_def.dependencies)
                if deps_satisfied:
                    runnable.append(role)
            except ValueError, KeyError:
                # Unknown role - skip
                pass

        if runnable:
            output = {
                "status": "dispatch",
                "agents": runnable,
                "parallel": len(runnable) > 1,
                "reason": f"Ready to dispatch: {', '.join(runnable)}",
            }
        else:
            output = {
                "status": "blocked",
                "agents": [],
                "reason": "Pending agents blocked by dependencies",
                "pending_agents": pending,
            }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(f"Status: {output['status']}")
        if output.get("agents"):
            print(f"Agents to dispatch: {', '.join(output['agents'])}")
            if output.get("parallel"):
                print("  (can run in parallel)")
        print(f"Reason: {output['reason']}")

    return 0
