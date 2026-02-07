#!/usr/bin/env python3
"""Data collection for self-improvement analysis.

This module provides a CLI to pre-collect relevant logs and context before
invoking egg for self-improvement analysis. It follows agent-mode design
principles by providing orientation (what to analyze) rather than constraining
the agent's exploration.

The collected data includes:
- Summary of failed runs (metadata, status, trigger)
- Truncated log excerpts to help identify which runs need deeper analysis
- Statistics to help prioritize analysis

The agent can still use `gh run view <id> --log` to get full logs when needed.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Add sandbox to path for imports when running this module directly (e.g., python collect.py).
# When invoked via `python -m egg_lib.self_improvement.collect` with PYTHONPATH=sandbox (as in
# the workflow), this is redundant but harmless.
sandbox_path = Path(__file__).parent.parent.parent
if str(sandbox_path) not in sys.path:
    sys.path.insert(0, str(sandbox_path))

from egg_lib.self_improvement.collectors.gha import GHALogCollector
from egg_lib.self_improvement.config import DEFAULT_SINCE_HOURS, EGG_WORKFLOWS

# Maximum log excerpt length per run (to avoid overwhelming context)
MAX_LOG_EXCERPT_CHARS = 5000
# Maximum total log chars across all runs (per partition)
MAX_TOTAL_LOG_CHARS = 50000
# Maximum failed runs per partition (to ensure egg can process all in one context)
MAX_RUNS_PER_PARTITION = 5


def truncate_logs(logs: str, max_chars: int = MAX_LOG_EXCERPT_CHARS) -> str:
    """Truncate logs to a reasonable excerpt, preserving start and end.

    Args:
        logs: Full log content
        max_chars: Maximum characters to keep

    Returns:
        Truncated logs with indicator if truncated
    """
    if len(logs) <= max_chars:
        return logs

    # Keep first third and last third, with truncation notice in middle
    head_chars = max_chars // 3
    tail_chars = max_chars // 3

    head = logs[:head_chars]
    tail = logs[-tail_chars:]
    truncated_chars = len(logs) - head_chars - tail_chars

    return f"{head}\n\n[... {truncated_chars:,} characters truncated ...]\n\n{tail}"


def partition_runs(
    runs: list[dict[str, Any]], max_runs: int = MAX_RUNS_PER_PARTITION
) -> list[list[dict[str, Any]]]:
    """Partition failed run summaries into batches for separate egg instances.

    Args:
        runs: List of failed run summaries
        max_runs: Maximum runs per partition

    Returns:
        List of partitions, each containing up to max_runs run summaries
    """
    if not runs:
        return []

    partitions = []
    for i in range(0, len(runs), max_runs):
        partitions.append(runs[i : i + max_runs])
    return partitions


def collect_run_summary(collector: GHALogCollector, since: datetime) -> dict[str, Any]:
    """Collect a summary of runs for analysis.

    Args:
        collector: GHA log collector instance
        since: Only include runs after this time

    Returns:
        Dictionary with run summaries and statistics
    """
    runs = collector.collect(since)

    # Separate by status
    failed_runs = [r for r in runs if r.status == "failure"]
    success_runs = [r for r in runs if r.status == "success"]
    other_runs = [r for r in runs if r.status not in ("success", "failure")]

    # Build summaries for failed runs (with log excerpts)
    failed_summaries = []
    total_log_chars = 0

    for run in failed_runs:
        # Truncate individual run logs
        log_excerpt = truncate_logs(run.logs)
        logs_omitted = False

        # Track total and potentially further truncate
        if total_log_chars + len(log_excerpt) > MAX_TOTAL_LOG_CHARS:
            remaining = MAX_TOTAL_LOG_CHARS - total_log_chars
            if remaining > 500:  # Only include if we can fit meaningful content
                log_excerpt = truncate_logs(run.logs, remaining)
            else:
                log_excerpt = "[logs omitted - total context limit reached]"
                logs_omitted = True

        total_log_chars += len(log_excerpt)

        summary = {
            "run_id": run.run_id,
            "workflow": run.metadata.get("workflow", "unknown"),
            "workflow_path": run.metadata.get("workflow_path", ""),
            "trigger": run.trigger,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "branch": run.metadata.get("head_branch", ""),
            "url": run.metadata.get("html_url", ""),
            "run_number": run.metadata.get("run_number", 0),
            "log_excerpt": log_excerpt,
            "logs_omitted": logs_omitted,
        }
        failed_summaries.append(summary)

    # Build lightweight summaries for successful runs (no logs)
    success_summaries = []
    for run in success_runs:
        summary = {
            "run_id": run.run_id,
            "workflow": run.metadata.get("workflow", "unknown"),
            "trigger": run.trigger,
            "started_at": run.started_at.isoformat(),
            "url": run.metadata.get("html_url", ""),
        }
        success_summaries.append(summary)

    return {
        "collected_at": datetime.now(UTC).isoformat(),
        "since": since.isoformat(),
        "repository": collector.repo,
        "statistics": {
            "total_runs": len(runs),
            "failed_runs": len(failed_runs),
            "successful_runs": len(success_runs),
            "other_runs": len(other_runs),
            "workflows_analyzed": EGG_WORKFLOWS,
        },
        "failed_runs": failed_summaries,
        "successful_runs": success_summaries,
    }


def format_markdown_summary(data: dict[str, Any]) -> str:
    """Format collected data as markdown for the prompt.

    Args:
        data: Collected run summary data

    Returns:
        Markdown-formatted summary
    """
    lines = []
    stats = data["statistics"]

    lines.append("## Pre-Collected Run Data")
    lines.append("")
    lines.append(f"**Repository:** {data['repository']}")
    lines.append(f"**Analysis window:** Since {data['since']}")
    lines.append(f"**Collected at:** {data['collected_at']}")
    lines.append("")

    lines.append("### Statistics")
    lines.append("")
    lines.append(f"- Total runs analyzed: {stats['total_runs']}")
    lines.append(f"- Failed runs: {stats['failed_runs']}")
    lines.append(f"- Successful runs: {stats['successful_runs']}")
    lines.append(f"- Other (cancelled/running): {stats['other_runs']}")
    lines.append("")

    if data["failed_runs"]:
        lines.append("### Failed Runs (with log excerpts)")
        lines.append("")
        lines.append(
            "The following runs failed. Log excerpts are included below. "
            "Use `gh run view <run_id> --log` for full logs if needed."
        )
        lines.append("")

        for run in data["failed_runs"]:
            lines.append(f"#### Run {run['run_id']}: {run['workflow']}")
            lines.append("")
            lines.append(f"- **Trigger:** {run['trigger']}")
            lines.append(f"- **Branch:** {run['branch']}")
            lines.append(f"- **Started:** {run['started_at']}")
            lines.append(f"- **URL:** {run['url']}")
            if run.get("logs_omitted"):
                lines.append(
                    "- **⚠️ Logs omitted:** Use `gh run view "
                    f"{run['run_id']} --log` to fetch full logs"
                )
            lines.append("")
            lines.append("<details><summary>Log excerpt</summary>")
            lines.append("")
            lines.append("```")
            lines.append(run["log_excerpt"])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    else:
        lines.append("### No Failed Runs")
        lines.append("")
        lines.append(
            "No failed runs found in the analysis window. All egg workflows completed successfully."
        )
        lines.append("")

    if data["successful_runs"]:
        lines.append("### Successful Runs (for reference)")
        lines.append("")
        lines.append("| Run ID | Workflow | Trigger | Started |")
        lines.append("|--------|----------|---------|---------|")
        for run in data["successful_runs"][:10]:  # Limit to 10
            lines.append(
                f"| {run['run_id']} | {run['workflow']} | "
                f"{run['trigger']} | {run['started_at'][:16]} |"
            )
        if len(data["successful_runs"]) > 10:
            lines.append(f"| ... | ({len(data['successful_runs']) - 10} more) | ... | ... |")
        lines.append("")

    return "\n".join(lines)


def format_partition_markdown(
    partition: list[dict[str, Any]],
    partition_index: int,
    total_partitions: int,
    base_data: dict[str, Any],
) -> str:
    """Format a single partition of failed runs as markdown.

    Args:
        partition: List of failed run summaries for this partition
        partition_index: 0-based index of this partition
        total_partitions: Total number of partitions
        base_data: Original collected data (for metadata and stats)

    Returns:
        Markdown-formatted summary for this partition
    """
    lines = []
    stats = base_data["statistics"]

    lines.append("## Pre-Collected Run Data")
    lines.append("")
    lines.append(f"**Repository:** {base_data['repository']}")
    lines.append(f"**Analysis window:** Since {base_data['since']}")
    lines.append(f"**Collected at:** {base_data['collected_at']}")
    lines.append(
        f"**Partition:** {partition_index + 1} of {total_partitions} "
        f"({len(partition)} runs in this batch)"
    )
    lines.append("")

    lines.append("### Overall Statistics")
    lines.append("")
    lines.append(f"- Total runs analyzed: {stats['total_runs']}")
    lines.append(f"- Total failed runs: {stats['failed_runs']}")
    lines.append(f"- Successful runs: {stats['successful_runs']}")
    lines.append(f"- Other (cancelled/running): {stats['other_runs']}")
    lines.append("")

    lines.append("### Failed Runs in This Batch")
    lines.append("")
    lines.append(
        f"This batch contains {len(partition)} of {stats['failed_runs']} total failed runs. "
        "Log excerpts are included below. "
        "Use `gh run view <run_id> --log` for full logs if needed."
    )
    lines.append("")

    for run in partition:
        lines.append(f"#### Run {run['run_id']}: {run['workflow']}")
        lines.append("")
        lines.append(f"- **Trigger:** {run['trigger']}")
        lines.append(f"- **Branch:** {run['branch']}")
        lines.append(f"- **Started:** {run['started_at']}")
        lines.append(f"- **URL:** {run['url']}")
        if run.get("logs_omitted"):
            lines.append(
                f"- **⚠️ Logs omitted:** Use `gh run view {run['run_id']} --log` to fetch full logs"
            )
        lines.append("")
        lines.append("<details><summary>Log excerpt</summary>")
        lines.append("")
        lines.append("```")
        lines.append(run["log_excerpt"])
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    """CLI entry point for data collection.

    Returns:
        Exit code (0 for success)
    """
    parser = argparse.ArgumentParser(
        description="Pre-collect run data for self-improvement analysis"
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=DEFAULT_SINCE_HOURS,
        help=f"Analyze runs from the last N hours (default: {DEFAULT_SINCE_HOURS})",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Repository in owner/repo format (auto-detected if not specified)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: stdout). For partitioned output, use a "
        "pattern with {partition} placeholder (e.g., 'partition_{partition}.md')",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--partition",
        action="store_true",
        help="Partition failed runs into separate files for parallel egg instances",
    )
    parser.add_argument(
        "--max-runs-per-partition",
        type=int,
        default=MAX_RUNS_PER_PARTITION,
        help=f"Maximum failed runs per partition (default: {MAX_RUNS_PER_PARTITION})",
    )

    args = parser.parse_args()

    # Validate max-runs-per-partition
    if args.max_runs_per_partition < 1:
        print(
            f"Error: --max-runs-per-partition must be at least 1, got {args.max_runs_per_partition}",
            file=sys.stderr,
        )
        print("PARTITION_COUNT=0", file=sys.stderr)
        return 1

    # Calculate since timestamp
    since = datetime.now(UTC) - timedelta(hours=args.since_hours)

    # Collect data
    try:
        collector = GHALogCollector(repo=args.repo)
        data = collect_run_summary(collector, since)
    except Exception as e:
        print(f"Error collecting data: {e}", file=sys.stderr)
        # Ensure PARTITION_COUNT is written so workflow can handle failure gracefully
        print("PARTITION_COUNT=0", file=sys.stderr)
        return 1

    # Handle partitioned output
    if args.partition:
        partitions = partition_runs(data["failed_runs"], args.max_runs_per_partition)

        if not partitions:
            # No failed runs, output summary indicating this
            output = format_markdown_summary(data)
            if args.output:
                # Write a single file with partition count = 0
                output_path = args.output.replace("{partition}", "0")
                Path(output_path).write_text(output)
            else:
                print(output)
            # Output partition count for the workflow
            print("PARTITION_COUNT=0", file=sys.stderr)
            return 0

        # Output partition count for the workflow to parse
        print(f"PARTITION_COUNT={len(partitions)}", file=sys.stderr)

        for i, partition in enumerate(partitions):
            if args.format == "json":
                partition_data = {
                    **data,
                    "partition": {"index": i, "total": len(partitions)},
                    "failed_runs": partition,
                }
                output = json.dumps(partition_data, indent=2)
            else:
                output = format_partition_markdown(partition, i, len(partitions), data)

            if args.output:
                output_path = args.output.replace("{partition}", str(i))
                Path(output_path).write_text(output)
            else:
                print(f"--- Partition {i + 1}/{len(partitions)} ---")
                print(output)
                print()

        return 0

    # Non-partitioned output (original behavior)
    if args.format == "json":
        output = json.dumps(data, indent=2)
    else:
        output = format_markdown_summary(data)

    # Write output
    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
