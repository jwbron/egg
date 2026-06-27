#!/usr/bin/env python3
"""Lint check (advisory): flag net-new SDLC ledger references in docs.

Documentation in egg is meant to be a *snapshot of the current state* of the
codebase, not a *ledger of changes*. SDLC pipeline-process artifacts -- which
``slice-N`` landed a change, which ``TASK-N`` implemented it, which ``cq-N``
HITL iteration resolved a question -- are meaningful while a pipeline runs, but
once the change is on ``main`` they are noise: they do not describe the current
system, they cost a reader implementation-roadmap knowledge to parse, and they
rot as later changes invalidate the narrative. See issue #3288 for the full
rationale and the corpus cleanup; this check is the deferred durability guard
(#3328) that keeps the cleaned corpus from re-accreting the same refs.

This check is **advisory**: it prints warnings and returns ``0`` by default so
it never blocks a PR. The hard part -- distinguishing ledger narration from
live-runtime vocabulary that legitimately contains the same tokens (the
slice-DAG domain model, per-slice Job names, the ``TASK-N`` plan format) -- has
its own tuning cost, so the gate stays soft until the allowlist is dialled in.
Pass ``--strict`` to make net-new refs a hard failure (not wired into CI yet).

Ratchet semantics
-----------------
``scripts/ledger-references-baseline.yaml`` records a per-file count of the
ledger tokens present when the baseline was last taken. The check flags only
*net-new* tokens -- a file whose current count exceeds its baseline -- so the
long pre-existing tail (tracked separately, decaying as docs are touched) does
not fire on every run. Lowering a count is always fine and never warns; run
``--update-baseline`` to re-snapshot after an intentional change.

Tokens detected
---------------
- ``slice-N``     -- per-slice rollout narration ("added in slice-4"). Matched
  case-insensitively (``Slice-4`` at a sentence start is caught too).
- ``TASK-N``      -- plan task ids used as a change-log ("slice-4 TASK-4-5").
  Matched UPPERCASE-only: lowercase ``task-N`` is live runtime vocabulary
  (timestamped run ids, contract task identifiers) and is intentionally ignored.
- ``cq-N``        -- HITL clarifying-question iteration ids. Case-insensitive.

Change-log *prose* ("what was removed", "used to ... now ...") is deliberately
out of scope for v1: it cannot be matched without high false positives against
the issue links that legitimately justify *why* the current system is shaped
the way it is. Tune the token set / allowlist before extending it.

Scope and false-positive controls
----------------------------------
- Scanned: markdown across the repo plus non-test Python under the source
  roots. Every line is scanned (not just docstrings/comments), but in practice
  the hyphenated tokens only land in strings, docstrings, and comments because
  ``-`` is not a Python identifier character. Test files and test directories
  are excluded for *both* markdown and Python -- fixtures use ``slice-N`` /
  ``task-N-N`` ids as live data, not as documentation.
- ``docs/templates/`` is excluded: ``plan.md``'s ``TASK-N`` is the live plan
  format, not a ledger ref.
- ``.egg-state/`` is excluded: pipeline state and BRC transcripts are a ledger
  by design.
- Per-line escape hatch: put ``ledger-ok`` in a comment on the line (e.g. a
  legitimate new slice-DAG error message) to exclude it from the count.

Usage:
    scripts/check-ledger-references.py
    scripts/check-ledger-references.py --strict           # net-new -> exit 1
    scripts/check-ledger-references.py --update-baseline   # re-snapshot counts
    scripts/check-ledger-references.py --list              # rank files by count

Exit codes:
    0  no net-new refs, or advisory mode (default)
    1  net-new refs found and --strict was passed
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "ledger-references-baseline.yaml"

# Non-test Python source roots whose docstrings/comments are scanned.
SOURCE_ROOTS = ("orchestrator", "gateway", "shared", "sandbox")

# Path prefixes (repo-relative, posix) excluded from the scan entirely because
# the tokens there are live machinery / pipeline state, not documentation.
EXCLUDED_PREFIXES = (
    ".egg-state/",  # pipeline state + BRC transcripts -- a ledger by design
    "docs/templates/",  # plan.md TASK-N is the live plan format
    ".venv/",
    ".git/",
    "node_modules/",
)

# Directory names under a source root that are skipped (tests use slice/task
# ids as live fixture data, not as human-facing documentation).
EXCLUDED_DIR_NAMES = frozenset({"tests", "__pycache__"})

# SDLC ledger token classes. Hyphenated forms are chosen deliberately: the live
# runtime vocabulary uses underscores / dotted access (``slice_id``,
# ``contract.slices``, ``EGG_*_SLICES``), so it does not match here.
#
# Case handling is asymmetric on purpose:
# - ``slice-N`` / ``cq-N`` match case-insensitively, so a sentence-initial
#   ``Slice-4`` or an upper-cased ``CQ-2`` is still caught. Capitalising these
#   never collides with live vocabulary (that uses ``slice_id`` / ``contract.slices``).
# - ``TASK-N`` stays UPPERCASE-only. Lowercase ``task-N`` is pervasive live
#   runtime vocabulary -- timestamped run ids (``task-20251129-222239``), example
#   ids in tool docs (``task-123``), and contract task identifiers in handler
#   code -- none of which are ledger narration. Folding case here would flood
#   false positives, so the plan-format casing is the discriminator (parallel to
#   the underscore/dotted-access guards above).
LEDGER_PATTERN = re.compile(
    r"\b(?i:slice)-\d+\b"  # per-slice rollout narration (slice-4 / Slice-4)
    r"|\bTASK-\d+(?:-\d+)?\b"  # plan task ids used as a change-log (uppercase only)
    r"|\b(?i:cq)-\d+\b"  # HITL clarifying-question iteration ids (cq-2 / CQ-2)
)

# Put this token in a comment on a line to exclude it from the count.
SUPPRESS_MARKER = "ledger-ok"


@dataclass(frozen=True)
class FileFindings:
    rel: str
    count: int
    # (line_number, line_text) for each line that contributed a match; used for
    # actionable warning output, not for the ratchet decision.
    lines: tuple[tuple[int, str], ...]


def load_baseline(path: Path | None = None) -> dict[str, int]:
    """Load the per-file ledger-token baseline counts."""
    if path is None:
        path = BASELINE_PATH
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    files_raw: dict[str, Any] = raw.get("files") or {}
    return {str(rel): int(count) for rel, count in files_raw.items()}


def write_baseline(counts: dict[str, int], path: Path | None = None) -> None:
    """Rewrite the baseline file with sorted, non-zero per-file counts."""
    if path is None:
        path = BASELINE_PATH
    header = (
        "# Per-file SDLC ledger-token counts (slice-N / TASK-N / cq-N).\n"
        "# Maintained by scripts/check-ledger-references.py --update-baseline.\n"
        "# The advisory ratchet flags files whose current count EXCEEDS the\n"
        "# value here (net-new refs); lowering a count never warns. See the\n"
        "# script docstring and issue #3328 for the rationale.\n"
    )
    payload = {"files": {rel: counts[rel] for rel in sorted(counts) if counts[rel] > 0}}
    path.write_text(header + yaml.safe_dump(payload, sort_keys=False))


def is_excluded(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def is_test_path(rel: Path) -> bool:
    if rel.name.startswith("test_") or rel.name.endswith("_test.py"):
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in rel.parts)


def _walk_in_scope(root: Path, suffix: str, repo_root: Path) -> list[Path]:
    """Walk ``root`` for files ending in ``suffix``, pruning excluded and test
    directories *during* traversal so we never descend into ``.git`` / ``.venv``
    / ``node_modules`` / ``.egg-state`` / ``docs/templates`` / test dirs."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        # Prune in place: drop subdirs that are excluded by prefix or are test
        # directories, so os.walk does not descend into them at all.
        dirnames[:] = [
            name
            for name in dirnames
            if not is_excluded((here / name).relative_to(repo_root).as_posix() + "/")
            and name not in EXCLUDED_DIR_NAMES
        ]
        for fn in filenames:
            if not fn.endswith(suffix):
                continue
            p = here / fn
            rel = p.relative_to(repo_root)
            if is_excluded(rel.as_posix()) or is_test_path(rel):
                continue
            out.append(p)
    return out


def iter_scanned_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Yield markdown (repo-wide) + non-test Python (source roots), sorted.

    Test files and test directories are excluded for both file types; excluded
    prefixes (``.egg-state/``, ``docs/templates/``, ``.git/`` …) are pruned
    during traversal rather than walked-then-filtered.
    """
    # Never flag the check itself (its docstring lists the token patterns).
    self_path = Path(__file__).resolve()

    out = _walk_in_scope(repo_root, ".md", repo_root)
    for root_name in SOURCE_ROOTS:
        root = repo_root / root_name
        if root.is_dir():
            out.extend(_walk_in_scope(root, ".py", repo_root))

    out = [p for p in out if p.resolve() != self_path]
    out.sort()
    return out


def scan_file(path: Path, repo_root: Path = REPO_ROOT) -> FileFindings:
    """Count ledger tokens in a file, honouring the per-line suppress marker."""
    rel = path.relative_to(repo_root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    count = 0
    hit_lines: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if SUPPRESS_MARKER in line:
            continue
        matches = LEDGER_PATTERN.findall(line)
        if matches:
            count += len(matches)
            hit_lines.append((lineno, line.strip()))
    return FileFindings(rel=rel, count=count, lines=tuple(hit_lines))


def scan_all(repo_root: Path = REPO_ROOT) -> dict[str, FileFindings]:
    """Scan every in-scope file; return findings keyed by repo-relative path."""
    findings: dict[str, FileFindings] = {}
    for path in iter_scanned_files(repo_root):
        f = scan_file(path, repo_root)
        if f.count:
            findings[f.rel] = f
    return findings


def evaluate(findings: dict[str, FileFindings], baseline: dict[str, int]) -> list[FileFindings]:
    """Return findings for files whose current count exceeds their baseline."""
    net_new: list[FileFindings] = []
    for rel in sorted(findings):
        f = findings[rel]
        if f.count > baseline.get(rel, 0):
            net_new.append(f)
    return net_new


def update_baseline(repo_root: Path = REPO_ROOT, path: Path | None = None) -> int:
    findings = scan_all(repo_root)
    counts = {rel: f.count for rel, f in findings.items()}
    write_baseline(counts, path)
    target = path if path is not None else BASELINE_PATH
    print(f"Wrote {len(counts)} entries to {target.name}")
    return 0


def list_files(repo_root: Path = REPO_ROOT) -> int:
    findings = scan_all(repo_root)
    rows = sorted(((f.count, rel) for rel, f in findings.items()), reverse=True)
    for count, rel in rows:
        print(f"{count:>5}  {rel}")
    print(f"\n{sum(c for c, _ in rows)} tokens across {len(rows)} files")
    return 0


def _print_net_new(net_new: list[FileFindings], baseline: dict[str, int]) -> None:
    total = sum(f.count - baseline.get(f.rel, 0) for f in net_new)
    print(
        f"advisory: {total} net-new SDLC ledger reference(s) "
        f"(slice-N / TASK-N / cq-N) across {len(net_new)} file(s):"
    )
    for f in net_new:
        base = baseline.get(f.rel, 0)
        print(f"  - {f.rel}: {f.count} (baseline {base}, +{f.count - base})")
        # When the file had no prior refs every match is genuinely net-new, so
        # the lines pinpoint the additions. When it was already in the baseline
        # the count is the only signal -- the specific net-new token sits among
        # grandfathered ones, so the PR diff (not this list) is where to look.
        if base == 0:
            for lineno, line in f.lines[:5]:
                snippet = line if len(line) <= 100 else line[:97] + "..."
                print(f"      L{lineno}: {snippet}")
            if len(f.lines) > 5:
                print(f"      ... and {len(f.lines) - 5} more line(s)")
        else:
            print("      (new token(s) among existing refs -- check the PR diff)")
    print(
        "\nDocs should snapshot current state, not narrate the SDLC pipeline "
        "(see #3288).\nIf a token is legitimate live machinery, add `ledger-ok` "
        "in a comment on the\nline. After an intentional change, re-snapshot "
        "with:\n  scripts/check-ledger-references.py --update-baseline"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on net-new refs (default is advisory: warn and exit 0).",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Re-snapshot per-file counts into ledger-references-baseline.yaml.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every in-scope file with its ledger-token count.",
    )
    args = parser.parse_args(argv)

    if args.update_baseline:
        return update_baseline()
    if args.list:
        return list_files()

    findings = scan_all()
    baseline = load_baseline()
    net_new = evaluate(findings, baseline)

    if not net_new:
        return 0

    _print_net_new(net_new, baseline)
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
