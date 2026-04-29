#!/usr/bin/env python3
"""Telemetry: did the tester scaffold-first while waiting for the coder?

Background — issue #2249
------------------------
The implement-phase BRC roster runs coder, tester, and documenter in
parallel. Tester is structurally downstream of coder: it cannot finalize
tests for code that does not exist yet. The tester prompt (both the
reviewer-preparation block and, since #2249, the producer-orientation
block) tells tester to draft test scaffolding from the plan while the
coder is producing — file paths from ``tasks[].files``, signatures from
acceptance criteria, fixture imports, and mocked-input scenarios — and
to defer the actual ``wait-loop`` on coder's CONSENSUS_PROPOSE until the
scaffolds are drafted.

Whether the prompt is being followed in practice is observable. This
script answers: across the BRC histories under ``.egg-state/brc-history/``,
in what fraction of implement phases did the tester emit a heartbeat
mentioning scaffold work *before* coder's first CONSENSUS_PROPOSE?

Caveats
-------
This is a proxy signal, not a direct tool-call audit. We cannot see the
tester's Edit/Write tool calls from BRC history alone, so we match
keywords against tester heartbeat bodies in the wait window
(scaffold/test file/drafted {test,scaffold,fixture}/prepared test/
fixture/test signature/stub — see ``_SCAFFOLD_KEYWORDS``). False
negatives are possible — a tester that scaffolded silently without
emitting a heartbeat that named the work will look like it skipped.
False positives are unlikely (the keyword set is specific to test-prep
language). Treat the aggregate fraction as a coarse compliance signal,
not a proof of behavior.

Usage
-----
::

    python scripts/scaffold_first_telemetry.py
    python scripts/scaffold_first_telemetry.py --brc-dir .egg-state/brc-history
    python scripts/scaffold_first_telemetry.py --json > out.jsonl
    python scripts/scaffold_first_telemetry.py --verbose

Exit code is always 0; this is reporting, not gating.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import glob
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

# Heartbeat-body language that suggests the tester drafted test
# scaffolding while waiting. Matched case-insensitively against the
# whole body; word boundaries on the keys keep "fixture" from matching
# inside unrelated identifiers. ``drafted`` and ``signature`` are
# qualified with test-context anchors so unrelated phrasing like
# "drafted plan" or "method signature changed in dep" does not fire.
_SCAFFOLD_KEYWORDS = (
    r"\bscaffold",  # scaffold, scaffolds, scaffolding, scaffolded
    r"\btest file",  # test file, test files
    r"\bdrafted (test|scaffold|fixture)",
    r"\bprepared test",  # prepared test, prepared tests, prepared testing
    r"\bfixture",
    r"\btest signature",
    r"\bstub",
)
_SCAFFOLD_RE = re.compile("|".join(_SCAFFOLD_KEYWORDS), re.IGNORECASE)


@dataclasses.dataclass
class PipelineRow:
    """One implement-phase BRC log's scaffold-first signal."""

    pipeline_id: str
    source_file: str
    has_tester: bool
    has_upstream: bool
    upstream_propose_ts: str | None
    tester_propose_ts: str | None
    tester_wait_minutes: float | None
    tester_heartbeats_before_upstream: int
    tester_scaffold_signal: bool
    tester_heartbeat_excerpts: list[str]


def _parse_ts(value: str) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp; return None if unparseable."""
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError, TypeError:
        return None


def _first_propose_ts(messages: list[dict[str, Any]], role: str) -> str | None:
    """Return the timestamp of the first CONSENSUS_PROPOSE from ``role``."""
    for msg in messages:
        if msg.get("from_role") == role and msg.get("message_type") == "CONSENSUS_PROPOSE":
            ts = msg.get("timestamp")
            if isinstance(ts, str):
                return ts
    return None


def _heartbeats_before(
    messages: list[dict[str, Any]], role: str, cutoff_ts: str
) -> list[dict[str, Any]]:
    """Return ``role``'s heartbeats with timestamp < ``cutoff_ts``.

    Compares parsed datetimes rather than raw strings so mixed offset
    forms (``+00:00`` vs. ``Z``) sort correctly. Heartbeats with
    unparseable timestamps are dropped — there is no defensible way to
    place them in the wait window.
    """
    cutoff = _parse_ts(cutoff_ts)
    if cutoff is None:
        return []
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("from_role") != role or msg.get("message_type") != "HEARTBEAT":
            continue
        ts_value = msg.get("timestamp")
        if not isinstance(ts_value, str):
            continue
        ts = _parse_ts(ts_value)
        if ts is not None and ts < cutoff:
            out.append(msg)
    return out


def _scaffold_signal(heartbeats: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Detect scaffold language in heartbeat bodies.

    Returns (any_match, list_of_matched_excerpts). The excerpts are the
    matched heartbeat bodies trimmed to ~140 chars so the verbose output
    is readable but bounded.
    """
    excerpts: list[str] = []
    for hb in heartbeats:
        body = hb.get("body") or ""
        if not body:
            continue
        if _SCAFFOLD_RE.search(body):
            excerpts.append(body[:140])
    return bool(excerpts), excerpts


def _pipeline_id_from_filename(path: Path) -> str:
    """Extract the pipeline slug from a brc-history filename.

    Files are named ``<pipeline_id>-<phase>.json`` per orchestrator
    convention. The phase suffix can be ``implement|plan|refine|pr``,
    so we strip the longest matching suffix.
    """
    stem = path.stem
    for phase in ("-implement", "-plan", "-refine", "-pr"):
        if stem.endswith(phase):
            return stem[: -len(phase)]
    return stem


def analyze_file(path: Path, *, upstream: str = "coder") -> PipelineRow:
    """Analyze one BRC history JSON file and return a row."""
    pipeline_id = _pipeline_id_from_filename(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            messages = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        print(f"warn: skipping {path}: {err}", file=sys.stderr)
        return PipelineRow(
            pipeline_id=pipeline_id,
            source_file=str(path),
            has_tester=False,
            has_upstream=False,
            upstream_propose_ts=None,
            tester_propose_ts=None,
            tester_wait_minutes=None,
            tester_heartbeats_before_upstream=0,
            tester_scaffold_signal=False,
            tester_heartbeat_excerpts=[],
        )

    if not isinstance(messages, list):
        return PipelineRow(
            pipeline_id=pipeline_id,
            source_file=str(path),
            has_tester=False,
            has_upstream=False,
            upstream_propose_ts=None,
            tester_propose_ts=None,
            tester_wait_minutes=None,
            tester_heartbeats_before_upstream=0,
            tester_scaffold_signal=False,
            tester_heartbeat_excerpts=[],
        )

    # Sort defensively — most files are already chronological but
    # ``brc-history`` is an append log and out-of-order entries can
    # appear in tests that synthesize messages.
    messages.sort(key=lambda m: m.get("timestamp") or "")

    tester_propose_ts = _first_propose_ts(messages, "tester")
    upstream_propose_ts = _first_propose_ts(messages, upstream)

    has_tester = any(m.get("from_role") == "tester" for m in messages)
    has_upstream = any(m.get("from_role") == upstream for m in messages)

    tester_heartbeats: list[dict[str, Any]] = []
    if has_tester and upstream_propose_ts is not None:
        tester_heartbeats = _heartbeats_before(messages, "tester", upstream_propose_ts)
    scaffold_signal, excerpts = _scaffold_signal(tester_heartbeats)

    wait_minutes: float | None = None
    if upstream_propose_ts and tester_propose_ts:
        upstream_dt = _parse_ts(upstream_propose_ts)
        tester_dt = _parse_ts(tester_propose_ts)
        if upstream_dt and tester_dt:
            wait_minutes = (tester_dt - upstream_dt).total_seconds() / 60.0

    return PipelineRow(
        pipeline_id=pipeline_id,
        source_file=str(path),
        has_tester=has_tester,
        has_upstream=has_upstream,
        upstream_propose_ts=upstream_propose_ts,
        tester_propose_ts=tester_propose_ts,
        tester_wait_minutes=wait_minutes,
        tester_heartbeats_before_upstream=len(tester_heartbeats),
        tester_scaffold_signal=scaffold_signal,
        tester_heartbeat_excerpts=excerpts,
    )


def _eligible(row: PipelineRow) -> bool:
    """A pipeline counts toward the aggregate only when both tester and
    upstream actually ran and the upstream actually proposed.

    Pipelines where coder never reached CONSENSUS_PROPOSE (e.g. force-killed
    early, or implement phase aborted) cannot tell us whether tester
    scaffold-first'd — there is no wait window to evaluate.
    """
    return row.has_tester and row.has_upstream and row.upstream_propose_ts is not None


def _summarize(rows: list[PipelineRow]) -> dict[str, Any]:
    eligible = [r for r in rows if _eligible(r)]
    with_signal = [r for r in eligible if r.tester_scaffold_signal]
    waits = [r.tester_wait_minutes for r in eligible if r.tester_wait_minutes is not None]
    summary: dict[str, Any] = {
        "total_files": len(rows),
        "eligible_pipelines": len(eligible),
        "ineligible_pipelines": len(rows) - len(eligible),
        "scaffold_signal_count": len(with_signal),
        "scaffold_signal_fraction": (len(with_signal) / len(eligible)) if eligible else None,
    }
    if waits:
        summary["wait_minutes_min"] = round(min(waits), 2)
        summary["wait_minutes_max"] = round(max(waits), 2)
        summary["wait_minutes_median"] = round(statistics.median(waits), 2)
        summary["wait_minutes_mean"] = round(sum(waits) / len(waits), 2)
    return summary


def _format_text(rows: list[PipelineRow], summary: dict[str, Any], *, verbose: bool) -> str:
    lines: list[str] = []
    lines.append(
        "pipeline_id                       eligible  scaffold  hb_count  wait_min  upstream_propose"
    )
    lines.append("-" * 96)
    for r in rows:
        elig = "yes" if _eligible(r) else "no"
        sig = "yes" if r.tester_scaffold_signal else "no "
        wait = f"{r.tester_wait_minutes:7.2f}" if r.tester_wait_minutes is not None else "    n/a"
        upstream = (r.upstream_propose_ts or "")[:19]
        lines.append(
            f"{r.pipeline_id:33s} {elig:8s} {sig:8s} "
            f"{r.tester_heartbeats_before_upstream:8d} {wait:>9s} {upstream}"
        )
        if verbose and r.tester_heartbeat_excerpts:
            for ex in r.tester_heartbeat_excerpts:
                lines.append(f"    matched: {ex}")
    lines.append("")
    lines.append("=" * 96)
    lines.append(f"total files scanned:        {summary['total_files']}")
    lines.append(f"eligible pipelines:         {summary['eligible_pipelines']}")
    lines.append(f"ineligible (skipped):       {summary['ineligible_pipelines']}")
    if summary["scaffold_signal_fraction"] is None:
        lines.append("scaffold-first fraction:    n/a (no eligible pipelines)")
    else:
        pct = summary["scaffold_signal_fraction"] * 100
        lines.append(
            f"scaffold-first fraction:    {summary['scaffold_signal_count']}/"
            f"{summary['eligible_pipelines']} ({pct:.1f}%)"
        )
    if "wait_minutes_median" in summary:
        lines.append(
            f"tester wait minutes (after upstream propose): "
            f"min={summary['wait_minutes_min']} "
            f"median={summary['wait_minutes_median']} "
            f"max={summary['wait_minutes_max']} "
            f"mean={summary['wait_minutes_mean']}"
        )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--brc-dir",
        default=".egg-state/brc-history",
        help="Directory containing BRC history JSON files (default: %(default)s).",
    )
    parser.add_argument(
        "--pattern",
        default="*-implement.json",
        help=(
            "Glob pattern within --brc-dir to scan. Default '%(default)s' "
            "limits to implement phases (the only phase where tester runs)."
        ),
    )
    parser.add_argument(
        "--upstream",
        default="coder",
        help=(
            "Upstream producer that tester depends on. Default 'coder' "
            "matches the standard implement BRC roster wiring; override if "
            "evaluating a custom roster."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit one JSON record per pipeline plus a final summary record "
            "(JSON Lines). Suitable for piping into jq or a notebook."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="In text mode, also print the matched heartbeat excerpts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    brc_dir = Path(args.brc_dir)
    if not brc_dir.is_dir():
        print(f"error: --brc-dir {brc_dir} is not a directory", file=sys.stderr)
        return 1

    paths = sorted(Path(p) for p in glob.glob(str(brc_dir / args.pattern)))
    rows = [analyze_file(p, upstream=args.upstream) for p in paths]
    summary = _summarize(rows)

    if args.json:
        for r in rows:
            print(json.dumps(dataclasses.asdict(r)))
        print(json.dumps({"summary": summary}))
    else:
        print(_format_text(rows, summary, verbose=args.verbose))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
