#!/usr/bin/env python3
"""Self-improvement analysis orchestrator.

This script collects logs from GitHub Actions or local container runs,
analyzes them for issues, and outputs a summary report.

Usage:
    python -m egg_lib.self_improvement.analyze --source gha --since-hours 24
    python -m egg_lib.self_improvement.analyze --source local --output json
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from .collectors.base import RunLog
from .collectors.gha import GHALogCollector
from .collectors.local import LocalLogCollector
from .config import DEFAULT_SINCE_HOURS


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Analyze egg runs for self-improvement insights")
    parser.add_argument(
        "--source",
        choices=["gha", "local", "auto"],
        default="auto",
        help="Log source: gha (GitHub Actions), local (container logs), or auto (detect)",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=DEFAULT_SINCE_HOURS,
        help=f"Analyze runs from the last N hours (default: {DEFAULT_SINCE_HOURS})",
    )
    parser.add_argument(
        "--output",
        choices=["summary", "json", "both"],
        default="summary",
        help="Output format: summary (human-readable), json (structured), or both",
    )
    parser.add_argument(
        "--json-file",
        type=str,
        default=None,
        help="File path to write JSON output (used with --output both)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Repository in owner/repo format (for GHA source, auto-detected if not provided)",
    )
    return parser.parse_args()


def select_collector(
    source: str,
    repo: str | None = None,
) -> GHALogCollector | LocalLogCollector:
    """Select the appropriate log collector based on source.

    Args:
        source: "gha", "local", or "auto"
        repo: Repository for GHA collector

    Returns:
        Configured log collector instance
    """
    if source == "auto":
        # Detect environment: use GHA collector if running in GitHub Actions
        if os.getenv("GITHUB_ACTIONS"):
            return GHALogCollector(repo=repo)
        else:
            return LocalLogCollector()
    elif source == "gha":
        return GHALogCollector(repo=repo)
    else:
        return LocalLogCollector()


def generate_summary(runs: list[RunLog], since: datetime) -> str:
    """Generate a human-readable summary of collected runs.

    Args:
        runs: List of collected run logs
        since: The cutoff time for analysis

    Returns:
        Formatted summary string
    """
    if not runs:
        return f"No runs found since {since.isoformat()}"

    # Calculate statistics
    total = len(runs)
    successful = sum(1 for r in runs if r.status == "success")
    failed = sum(1 for r in runs if r.status == "failure")
    cancelled = sum(1 for r in runs if r.status == "cancelled")
    running = sum(1 for r in runs if r.status == "running")

    success_rate = (successful / total * 100) if total > 0 else 0

    # Group by trigger
    triggers: dict[str, int] = {}
    for run in runs:
        triggers[run.trigger] = triggers.get(run.trigger, 0) + 1

    # Find error patterns (basic)
    error_runs = [r for r in runs if r.status == "failure"]
    error_snippets: list[str] = []
    for run in error_runs[:3]:  # Limit to 3 examples
        # Extract first error-like line
        for line in run.logs.split("\n"):
            if any(p in line.lower() for p in ["error", "failed", "exception"]):
                snippet = line.strip()[:100]
                if snippet:
                    error_snippets.append(f"  - [{run.run_id}] {snippet}")
                    break

    # Build summary
    lines = [
        "=" * 60,
        "Self-Improvement Analysis Report",
        "=" * 60,
        f"Period: {since.isoformat()} to {datetime.now(UTC).isoformat()}",
        f"Source: {runs[0].source if runs else 'N/A'}",
        "",
        "## Summary",
        f"Total runs: {total}",
        f"  - Successful: {successful} ({success_rate:.1f}%)",
        f"  - Failed: {failed}",
        f"  - Cancelled: {cancelled}",
        f"  - Running: {running}",
        "",
        "## Triggers",
    ]

    for trigger, count in sorted(triggers.items(), key=lambda x: -x[1]):
        lines.append(f"  - {trigger}: {count}")

    if error_snippets:
        lines.extend(["", "## Sample Errors"])
        lines.extend(error_snippets)

    lines.extend(["", "## Run Details"])
    for run in runs[:10]:  # Limit to 10 runs
        status_icon = {"success": "✓", "failure": "✗", "cancelled": "○", "running": "→"}
        icon = status_icon.get(run.status, "?")
        lines.append(f"  {icon} {run.run_id}: {run.status} ({run.trigger})")
        if run.metadata.get("html_url"):
            lines.append(f"    URL: {run.metadata['html_url']}")

    if len(runs) > 10:
        lines.append(f"  ... and {len(runs) - 10} more runs")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def generate_json(runs: list[RunLog], since: datetime) -> str:
    """Generate JSON output of collected runs.

    Args:
        runs: List of collected run logs
        since: The cutoff time for analysis

    Returns:
        JSON string with run data
    """
    total = len(runs)
    successful = sum(1 for r in runs if r.status == "success")
    failed = sum(1 for r in runs if r.status == "failure")

    data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "since": since.isoformat(),
        "summary": {
            "total_runs": total,
            "successful_runs": successful,
            "failed_runs": failed,
            "success_rate": (successful / total * 100) if total > 0 else 0,
        },
        "runs": [
            {
                "run_id": r.run_id,
                "source": r.source,
                "status": r.status,
                "trigger": r.trigger,
                "started_at": r.started_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "metadata": r.metadata,
                # Omit full logs in JSON to keep output manageable
                "log_length": len(r.logs),
            }
            for r in runs
        ],
    }

    return json.dumps(data, indent=2)


def main() -> int:
    """Main entry point for the analysis script."""
    args = parse_args()

    # Calculate since timestamp
    since = datetime.now(UTC) - timedelta(hours=args.since_hours)

    # Select and run collector
    collector = select_collector(args.source, args.repo)
    print(f"Collecting logs from {collector.__class__.__name__}...", file=sys.stderr)

    try:
        runs = collector.collect(since)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Error collecting logs: {e}", file=sys.stderr)
        return 1

    print(f"Collected {len(runs)} runs", file=sys.stderr)

    # Generate output
    if args.output == "json":
        print(generate_json(runs, since))
    elif args.output == "both":
        # Output both formats in a single run (avoids duplicate API calls)
        print(generate_summary(runs, since))
        json_output = generate_json(runs, since)
        if args.json_file:
            with open(args.json_file, "w") as f:
                f.write(json_output)
            print(f"\nJSON report written to: {args.json_file}", file=sys.stderr)
        else:
            print("\n--- JSON Output ---\n")
            print(json_output)
    else:
        print(generate_summary(runs, since))

    return 0


if __name__ == "__main__":
    sys.exit(main())
