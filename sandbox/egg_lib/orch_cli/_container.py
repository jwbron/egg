"""Container subcommands (list/spawn/get/stop/logs).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import sys
from typing import Any
from urllib.parse import urlencode

from egg_lib import orch_cli as _pkg

from ._http import (
    print_json,
    validate_id,
)


def cmd_container_list(args: argparse.Namespace) -> int:
    """List containers for a pipeline."""
    pid = _pkg.require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/containers")

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
    pid = _pkg.require_pipeline_id(args)
    data: dict[str, Any] = {
        "agent_role": args.role,
    }
    if args.issue:
        data["issue_number"] = args.issue
    if args.private:
        data["private_mode"] = True

    result = _pkg.orch_request(
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
    pid = _pkg.require_pipeline_id(args)
    cid = validate_id(args.container_id, "container_id")
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/containers/{cid}")

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
    pid = _pkg.require_pipeline_id(args)
    cid = validate_id(args.container_id, "container_id")
    result = _pkg.orch_request(
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
    pid = _pkg.require_pipeline_id(args)
    cid = validate_id(args.container_id, "container_id")
    params: dict[str, str] = {}
    if args.lines:
        params["tail"] = str(args.lines)

    endpoint = f"/api/v1/pipelines/{pid}/containers/{cid}/logs"
    if params:
        endpoint += "?" + urlencode(params)

    result = _pkg.orch_request(endpoint)

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
