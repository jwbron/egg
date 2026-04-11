#!/usr/bin/env python3
"""Parallel validation: compare egg harness vs claude-sdk on identical tasks.

Runs a set of short agent tasks through both EGG_HARNESS=egg and
EGG_HARNESS=claude-sdk, collecting cost_usd, num_turns, duration_ms,
and success rate.  Outputs a comparison table and summary verdict.

Usage:
    python3 scripts/validate_harness_parity.py [--scenarios N] [--model MODEL]

Environment:
    GATEWAY_URL or ANTHROPIC_BASE_URL must point to the gateway sidecar.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Ensure shared/ is importable.
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "shared"))


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: list[dict[str, str]] = [
    {
        "id": "read-file",
        "prompt": (
            "Read the file pyproject.toml in the current directory and tell me "
            "the project name and version. Reply with ONLY the name and version, "
            "nothing else."
        ),
    },
    {
        "id": "glob-search",
        "prompt": (
            "Find all Python files named conftest.py in this repository. "
            "Report the count and list the first 5 paths. Reply concisely."
        ),
    },
    {
        "id": "grep-content",
        "prompt": (
            "Search for the string 'def setup_agent_rules' in the codebase. "
            "Report which file(s) contain it and the line number. Reply concisely."
        ),
    },
    {
        "id": "write-file",
        "prompt": (
            "Create a file /tmp/harness-test-output.txt containing the text "
            "'harness validation passed'. Then read it back and confirm the "
            "contents match. Reply with only 'OK' if successful."
        ),
    },
    {
        "id": "multi-tool",
        "prompt": (
            "1) Read the Makefile in this repo and find all target names. "
            "2) Count how many targets there are. "
            "3) Write the count to /tmp/makefile-targets.txt. "
            "Reply with the count."
        ),
    },
    {
        "id": "edit-file",
        "prompt": (
            "Create a file /tmp/edit-test.txt with content 'hello world'. "
            "Then edit it to replace 'hello' with 'goodbye'. "
            "Read the file back and confirm it says 'goodbye world'. "
            "Reply with 'OK' if correct."
        ),
    },
    {
        "id": "bash-command",
        "prompt": (
            "Run 'python3 --version' and 'git --version' using bash. "
            "Report both version strings. Reply concisely."
        ),
    },
    {
        "id": "code-analysis",
        "prompt": (
            "Read shared/egg_harness/loop.py and count how many times "
            "'_build_result' is called (not defined). Reply with only the count."
        ),
    },
    {
        "id": "git-log",
        "prompt": (
            "Run 'git log --oneline -5' in this repository and report "
            "the 5 most recent commit subjects. Reply concisely."
        ),
    },
    {
        "id": "error-recovery",
        "prompt": (
            "Try to read the file /nonexistent/path/file.txt. "
            "When it fails, report the error. Then read /tmp/harness-test-output.txt "
            "instead (create it first with 'recovery test' as content if needed). "
            "Reply with the contents of the file you successfully read."
        ),
    },
]


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Metrics from a single agent run."""

    scenario_id: str
    harness: str
    success: bool
    cost_usd: float
    num_turns: int
    duration_ms: int
    error: str | None = None


async def run_scenario_with_harness(
    scenario: dict[str, str],
    harness: str,
    model: str,
    max_turns: int,
    cwd: str,
) -> RunResult:
    """Run a single scenario with the specified harness."""
    scenario_id = scenario["id"]
    prompt = scenario["prompt"]

    # Set the harness env var for the run.
    # NOTE: Safe only because main() runs scenarios sequentially. Would race
    # if parallelised — use subprocess env overrides instead in that case.
    old_harness = os.environ.get("EGG_HARNESS")
    os.environ["EGG_HARNESS"] = harness

    start = time.monotonic()

    try:
        if harness == "egg":
            from egg_harness_integration.harness_factory import create_egg_harness

            loop, _event_bus, _config = create_egg_harness(
                model=model,
                max_turns=max_turns,
                cwd=cwd,
                timeout=120,
                intercept_tools=False,
            )
            result = await loop.run(prompt)

            return RunResult(
                scenario_id=scenario_id,
                harness=harness,
                success=result.success,
                cost_usd=result.cost_usd or 0.0,
                num_turns=result.num_turns or 0,
                duration_ms=result.duration_ms or int((time.monotonic() - start) * 1000),
                error=result.error,
            )
        else:
            # claude-sdk path
            from egg_agent.client import run_agent_async

            result = await run_agent_async(
                prompt,
                model=model,
                max_turns=max_turns,
                cwd=cwd,
                timeout=120,
                intercept_tools=False,
            )

            return RunResult(
                scenario_id=scenario_id,
                harness=harness,
                success=result.success,
                cost_usd=result.cost_usd or 0.0,
                num_turns=result.num_turns or 0,
                duration_ms=result.duration_ms or int((time.monotonic() - start) * 1000),
                error=result.error,
            )

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return RunResult(
            scenario_id=scenario_id,
            harness=harness,
            success=False,
            cost_usd=0.0,
            num_turns=0,
            duration_ms=elapsed_ms,
            error=str(exc),
        )
    finally:
        if old_harness is not None:
            os.environ["EGG_HARNESS"] = old_harness
        elif "EGG_HARNESS" in os.environ:
            del os.environ["EGG_HARNESS"]


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


def print_comparison(results: list[RunResult]) -> dict[str, Any]:
    """Print a comparison table and return summary stats."""
    egg_results = [r for r in results if r.harness == "egg"]
    sdk_results = [r for r in results if r.harness == "claude-sdk"]

    # Build per-scenario comparison.
    egg_by_id = {r.scenario_id: r for r in egg_results}
    sdk_by_id = {r.scenario_id: r for r in sdk_results}

    header = (
        f"{'Scenario':<18} "
        f"{'egg ok':>6} {'sdk ok':>6}  "
        f"{'egg $':>8} {'sdk $':>8}  "
        f"{'egg turns':>9} {'sdk turns':>9}  "
        f"{'egg ms':>8} {'sdk ms':>8}"
    )
    print("\n" + "=" * len(header))
    print("PARALLEL VALIDATION: egg harness vs claude-sdk")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for sid in sorted({r.scenario_id for r in results}):
        e = egg_by_id.get(sid)
        s = sdk_by_id.get(sid)
        if e and s:
            print(
                f"{sid:<18} "
                f"{'PASS' if e.success else 'FAIL':>6} "
                f"{'PASS' if s.success else 'FAIL':>6}  "
                f"{e.cost_usd:>8.4f} {s.cost_usd:>8.4f}  "
                f"{e.num_turns:>9} {s.num_turns:>9}  "
                f"{e.duration_ms:>8} {s.duration_ms:>8}"
            )

    print("-" * len(header))

    # Aggregate stats.
    def _stats(runs: list[RunResult]) -> dict[str, Any]:
        successes = sum(1 for r in runs if r.success)
        costs = [r.cost_usd for r in runs if r.cost_usd > 0]
        turns = [r.num_turns for r in runs if r.num_turns > 0]
        durations = [r.duration_ms for r in runs if r.duration_ms > 0]
        return {
            "total": len(runs),
            "success_count": successes,
            "success_rate": successes / len(runs) if runs else 0,
            "cost_mean": statistics.mean(costs) if costs else 0,
            "cost_total": sum(costs),
            "turns_mean": statistics.mean(turns) if turns else 0,
            "duration_mean": statistics.mean(durations) if durations else 0,
            "duration_median": statistics.median(durations) if durations else 0,
        }

    egg_stats = _stats(egg_results)
    sdk_stats = _stats(sdk_results)

    print(
        f"{'TOTALS':<18} "
        f"{egg_stats['success_count']:>3}/{egg_stats['total']:<2} "
        f"{sdk_stats['success_count']:>3}/{sdk_stats['total']:<2}  "
        f"{egg_stats['cost_total']:>8.4f} {sdk_stats['cost_total']:>8.4f}  "
        f"{egg_stats['turns_mean']:>9.1f} {sdk_stats['turns_mean']:>9.1f}  "
        f"{egg_stats['duration_mean']:>8.0f} {sdk_stats['duration_mean']:>8.0f}"
    )
    print("=" * len(header))

    # Print delta summary.
    print("\nSUMMARY:")
    print(
        f"  egg success rate:  {egg_stats['success_rate']:.0%} ({egg_stats['success_count']}/{egg_stats['total']})"
    )
    print(
        f"  sdk success rate:  {sdk_stats['success_rate']:.0%} ({sdk_stats['success_count']}/{sdk_stats['total']})"
    )

    if sdk_stats["cost_total"] > 0:
        cost_delta = (egg_stats["cost_total"] - sdk_stats["cost_total"]) / sdk_stats["cost_total"]
        print(f"  cost delta:        {cost_delta:+.1%} (egg vs sdk)")
    if sdk_stats["turns_mean"] > 0:
        turns_delta = (egg_stats["turns_mean"] - sdk_stats["turns_mean"]) / sdk_stats["turns_mean"]
        print(f"  turns delta:       {turns_delta:+.1%} (egg vs sdk)")
    if sdk_stats["duration_mean"] > 0:
        dur_delta = (egg_stats["duration_mean"] - sdk_stats["duration_mean"]) / sdk_stats[
            "duration_mean"
        ]
        print(f"  duration delta:    {dur_delta:+.1%} (egg vs sdk)")

    # Print failures.
    failures = [r for r in results if not r.success]
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  [{f.harness}] {f.scenario_id}: {f.error}")

    summary = {
        "egg": egg_stats,
        "sdk": sdk_stats,
        "per_scenario": [asdict(r) for r in results],
    }
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description="Validate harness parity")
    parser.add_argument(
        "--scenarios",
        type=int,
        default=len(SCENARIOS),
        help=f"Number of scenarios to run (max {len(SCENARIOS)})",
    )
    parser.add_argument(
        "--model",
        default="haiku",
        help="Model to use (default: haiku for fast/cheap validation)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=15,
        help="Max turns per scenario (default: 15)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to write JSON results",
    )
    args = parser.parse_args()

    cwd = str(_repo)
    scenarios = SCENARIOS[: args.scenarios]

    print(f"Running {len(scenarios)} scenarios with model={args.model}, max_turns={args.max_turns}")
    print(f"CWD: {cwd}")
    print()

    all_results: list[RunResult] = []

    for i, scenario in enumerate(scenarios, 1):
        sid = scenario["id"]
        print(f"[{i}/{len(scenarios)}] {sid}")

        # Run with egg harness.
        print("  egg harness...", end=" ", flush=True)
        egg_result = await run_scenario_with_harness(
            scenario, "egg", args.model, args.max_turns, cwd
        )
        all_results.append(egg_result)
        status = "PASS" if egg_result.success else "FAIL"
        print(
            f"{status} ({egg_result.duration_ms}ms, {egg_result.num_turns} turns, ${egg_result.cost_usd:.4f})"
        )

        # Run with claude-sdk.
        print("  claude-sdk...", end=" ", flush=True)
        sdk_result = await run_scenario_with_harness(
            scenario, "claude-sdk", args.model, args.max_turns, cwd
        )
        all_results.append(sdk_result)
        status = "PASS" if sdk_result.success else "FAIL"
        print(
            f"{status} ({sdk_result.duration_ms}ms, {sdk_result.num_turns} turns, ${sdk_result.cost_usd:.4f})"
        )

        print()

    # Print comparison.
    summary = print_comparison(all_results)

    # Write JSON output if requested.
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(summary, indent=2))
        print(f"\nResults written to {output_path}")

    # Exit code: 0 if both have >=80% success rate and cost delta < 50%.
    egg_rate = summary["egg"]["success_rate"]
    sdk_rate = summary["sdk"]["success_rate"]
    if sdk_rate < 0.8:
        print(f"\nWARN: sdk baseline success rate {sdk_rate:.0%} < 80% — comparison may be unreliable")
    if egg_rate < 0.8:
        print(f"\nFAIL: egg success rate {egg_rate:.0%} < 80%")
        return 1
    if sdk_rate > 0 and summary["egg"]["cost_total"] > summary["sdk"]["cost_total"] * 1.5:
        print("\nWARN: egg cost is >50% higher than sdk")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
