"""Decision-queue subcommands (list/create/resolve/status).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import sys
from typing import Any

from egg_lib import orch_cli as _pkg

from ._http import (
    print_json,
    require_pipeline_id,
    validate_id,
)


def cmd_decision_list(args: argparse.Namespace) -> int:
    """List decisions for a pipeline."""
    pid = require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/decisions")

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

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/decisions", method="POST", data=data)

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

    result = _pkg.orch_request(
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
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/decisions/status")

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
