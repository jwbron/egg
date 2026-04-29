#!/usr/bin/env python3
"""Lint check: cap Python source-file line and byte counts.

Oversize source files force every BRC implement-phase agent into a
Grep-then-paginated-Read workflow because the Read tool's hard limits
(256KB / ~25k tokens) reject the file in one shot. Each paginated read is
another LLM turn, and BRC cycles re-pay the cost from a cleared context.
See issue #2248 for the operational evidence.

This check walks tracked Python sources under the source roots
(orchestrator/, gateway/, shared/, sandbox/, scripts/, config/) and rejects
any file that exceeds the configured caps:

- Hard cap (failure): 1500 lines OR 100,000 bytes (~25k tokens of Python).
- Soft cap (warning): 800 lines OR 60,000 bytes (~15k tokens).

Test files are exempt -- parametrized cases legitimately push line counts
past these caps and decomposing them mechanically would hurt readability.

Files already over the hard cap on day one are listed in
``scripts/file-size-allowlist.yaml``. The lint allows allowlisted files to
stay over the cap regardless of size; per-file size baselines were dropped
because every unrelated PR that touched one of these files needed a
baseline bump in the allowlist, conflicting with every other in-flight PR.
Removing a file from the allowlist (or letting it drop under the cap) is
encouraged as decomposition proceeds.

Usage:
    scripts/check-file-sizes.py
    scripts/check-file-sizes.py --update-allowlist  # add over-cap files
    scripts/check-file-sizes.py --list              # report all files

Exit codes:
    0  no violations
    1  one or more non-allowlisted files exceed the hard cap
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = Path(__file__).resolve().parent / "file-size-allowlist.yaml"

SOURCE_ROOTS = ("orchestrator", "gateway", "shared", "sandbox", "scripts", "config")

# Directories under SOURCE_ROOTS that are excluded -- tests bundle parametrized
# cases that legitimately exceed source-file caps.
EXCLUDED_DIR_NAMES = frozenset({"tests", "__pycache__"})


@dataclass(frozen=True)
class Caps:
    hard_lines: int
    hard_bytes: int
    soft_lines: int
    soft_bytes: int


@dataclass(frozen=True)
class FileStats:
    path: Path
    lines: int
    bytes: int


@dataclass
class Config:
    caps: Caps
    # path -> optional tracking issue. Membership is what gates the lint;
    # the issue field is documentation only.
    allowlist: dict[str, str | None]


def load_config(path: Path | None = None) -> Config:
    # Resolve the default at call time so tests can monkey-patch ALLOWLIST_PATH.
    if path is None:
        path = ALLOWLIST_PATH
    raw = yaml.safe_load(path.read_text()) or {}
    caps_raw = raw.get("caps") or {}
    caps = Caps(
        hard_lines=int(caps_raw["hard_lines"]),
        hard_bytes=int(caps_raw["hard_bytes"]),
        soft_lines=int(caps_raw["soft_lines"]),
        soft_bytes=int(caps_raw["soft_bytes"]),
    )
    files_raw: dict[str, Any] = raw.get("files") or {}
    allowlist: dict[str, str | None] = {}
    for rel, entry in files_raw.items():
        if isinstance(entry, dict):
            issue = entry.get("issue")
            allowlist[rel] = str(issue) if issue is not None else None
        else:
            allowlist[rel] = None
    return Config(caps=caps, allowlist=allowlist)


def is_test_file(rel: Path) -> bool:
    if rel.name.startswith("test_") or rel.name.endswith("_test.py"):
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in rel.parts)


def iter_source_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Yield every tracked Python source file under SOURCE_ROOTS, sorted."""
    out: list[Path] = []
    for root_name in SOURCE_ROOTS:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            rel = p.relative_to(repo_root)
            if is_test_file(rel):
                continue
            out.append(p)
    out.sort()
    return out


def measure(path: Path) -> FileStats:
    data = path.read_bytes()
    # Count physical lines without splitting the entire file twice.
    line_count = data.count(b"\n")
    if data and not data.endswith(b"\n"):
        line_count += 1
    return FileStats(path=path, lines=line_count, bytes=len(data))


def evaluate(
    stats: FileStats,
    rel: str,
    config: Config,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a single file."""
    errors: list[str] = []
    warnings: list[str] = []
    caps = config.caps
    in_allowlist = rel in config.allowlist

    over_hard_lines = stats.lines > caps.hard_lines
    over_hard_bytes = stats.bytes > caps.hard_bytes

    if over_hard_lines or over_hard_bytes:
        if not in_allowlist:
            errors.append(
                f"{rel}: {stats.lines} lines / {stats.bytes} bytes exceeds hard cap "
                f"({caps.hard_lines} lines / {caps.hard_bytes} bytes). "
                "Decompose the file or, if you cannot in this PR, add it to "
                "scripts/file-size-allowlist.yaml with a tracking issue."
            )
        return errors, warnings

    # Soft warnings are skipped for allowlisted files -- they're already
    # tracked for decomposition.
    if in_allowlist:
        return errors, warnings

    if stats.lines > caps.soft_lines:
        warnings.append(
            f"{rel}: {stats.lines} lines exceeds soft cap ({caps.soft_lines}). "
            "Consider decomposing before it hits the hard cap."
        )
    if stats.bytes > caps.soft_bytes:
        warnings.append(
            f"{rel}: {stats.bytes} bytes exceeds soft cap ({caps.soft_bytes}). "
            "Consider decomposing before it hits the hard cap."
        )
    return errors, warnings


def check_all(repo_root: Path = REPO_ROOT) -> tuple[list[str], list[str], list[str]]:
    """Run the full check. Returns (errors, warnings, stale_allowlist_entries)."""
    config = load_config()
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for path in iter_source_files(repo_root):
        rel = str(path.relative_to(repo_root))
        seen.add(rel)
        stats = measure(path)
        file_errors, file_warnings = evaluate(stats, rel, config)
        errors.extend(file_errors)
        warnings.extend(file_warnings)

    stale = sorted(rel for rel in config.allowlist if rel not in seen)
    return errors, warnings, stale


def write_allowlist(config: Config, allowlist: dict[str, str | None]) -> None:
    """Rewrite the allowlist file preserving caps + sorted entries."""

    def _entry(issue: str | None) -> Any:
        if issue is None:
            return None
        return {"issue": issue}

    payload: dict[str, Any] = {
        "caps": {
            "hard_lines": config.caps.hard_lines,
            "hard_bytes": config.caps.hard_bytes,
            "soft_lines": config.caps.soft_lines,
            "soft_bytes": config.caps.soft_bytes,
        },
        "files": {rel: _entry(issue) for rel, issue in sorted(allowlist.items())},
    }
    ALLOWLIST_PATH.write_text(yaml.safe_dump(payload, sort_keys=False))


def update_allowlist(repo_root: Path = REPO_ROOT) -> int:
    """Sync the allowlist with the current set of over-cap files.

    Adds over-cap files not yet listed (with no issue link) and drops
    entries for files that have shrunk under the cap or no longer exist.
    The ``issue:`` tracking field on each existing entry is carried
    forward; losing it would drop the link between the file and its
    decomposition follow-up.
    """
    config = load_config()
    new_allowlist: dict[str, str | None] = {}
    for path in iter_source_files(repo_root):
        rel = str(path.relative_to(repo_root))
        stats = measure(path)
        if stats.lines > config.caps.hard_lines or stats.bytes > config.caps.hard_bytes:
            new_allowlist[rel] = config.allowlist.get(rel)
    write_allowlist(config, new_allowlist)
    print(f"Wrote {len(new_allowlist)} entries to {ALLOWLIST_PATH.name}")
    return 0


def list_files(repo_root: Path = REPO_ROOT) -> int:
    """Print every Python source file with its line and byte counts."""
    rows = []
    for path in iter_source_files(repo_root):
        rel = str(path.relative_to(repo_root))
        stats = measure(path)
        rows.append((stats.lines, stats.bytes, rel))
    rows.sort(reverse=True)
    for lines, bts, rel in rows:
        print(f"{lines:>6}  {bts:>8}  {rel}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument(
        "--update-allowlist",
        action="store_true",
        help="Sync scripts/file-size-allowlist.yaml with current over-cap files.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every source file with its size; takes no action.",
    )
    args = parser.parse_args(argv)

    if args.update_allowlist:
        return update_allowlist()
    if args.list:
        return list_files()

    errors, warnings, stale = check_all()

    for w in warnings:
        print(f"warning: {w}")

    for s in stale:
        print(
            f"warning: stale allowlist entry: {s} no longer exists. "
            "Remove it from scripts/file-size-allowlist.yaml."
        )

    if errors:
        print()
        print("ERROR: file-size lint failed")
        print("=" * 76)
        for e in errors:
            print(f"  - {e}")
        print()
        print("Run `scripts/check-file-sizes.py --list` to see all files ranked by size.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
