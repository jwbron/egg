#!/usr/bin/env python3
"""Lint check: cap the number of *code* lines in a Python source file.

Oversize source files force every BRC implement-phase agent into a
Grep-then-paginated-Read workflow because the Read tool's hard limits
(256KB / ~25k tokens) reject the file in one shot. Each paginated read is
another LLM turn, and BRC cycles re-pay the cost from a cleared context.
See issue #2248 for the operational evidence.

What is counted
---------------
Only **code lines**: every physical line that is not blank, not a
comment-only line, and not part of a module / class / function docstring.
Docstring spans come from ``ast.get_docstring`` over ``Module``,
``ClassDef``, ``FunctionDef`` and ``AsyncFunctionDef``; comment-only and
blank lines fall out of ``tokenize``.

This is deliberate. Counting raw lines made deleting prose the cheapest
way to pass the check, which is the opposite of what the cap is for -- and
it happened in practice (commit ``68b185ca``, "trim health_monitor.py
docstring under file-size hard cap"). Under code-line counting, removing a
docstring, a comment or a blank line changes the reported number by
exactly zero, so that shortcut no longer exists. The cap bounds how much
*logic* lives in one module; prose density is not what makes a file hard
to change, and this repo deliberately invests in prose (see
``orchestrator/CLAUDE.md``).

Two things are still counted as code on purpose: multi-line string
literals that are not docstrings (an embedded prompt template really does
make a module longer to work through) and everything in a file that fails
to parse (the fallback over-counts rather than under-counts, so a broken
file can never measure smaller than a working one).

Caps
----
- Hard cap (failure): 1000 code lines.
- Soft cap (warning): 500 code lines.

Both were re-baselined against code-line counts in issue #3671. At this
repo's median code density (~0.58 code lines per raw line for files over
200 lines) 1000 code lines is ~1700 raw lines, and at the observed 60-90
bytes per code line it is ~60-90KB -- still inside the ~100KB / 25k-token
Read budget the original caps were justified by. The soft cap sits at half
the hard cap, matching the old 800/1500 shape.

There is no byte cap. Raw bytes have the identical gaming property this
check was rewritten to remove, a "code bytes" variant would be a redundant
proxy for code lines (they correlate almost perfectly here) that no longer
maps to the Read-tool byte limit that justified it, and the old 100,000
byte cap was non-binding in practice: exactly one file exceeded it, and
that file was over the line cap too.

Test files are exempt -- parametrized cases legitimately push line counts
past these caps and decomposing them mechanically would hurt readability.

Files over the hard cap are listed in ``scripts/file-size-allowlist.yaml``.
The lint allows allowlisted files to stay over the cap regardless of size;
per-file size baselines were dropped because every unrelated PR that
touched one of these files needed a baseline bump in the allowlist,
conflicting with every other in-flight PR. Removing a file from the
allowlist (or letting it drop under the cap) is encouraged as
decomposition proceeds.

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
import ast
import io
import sys
import tokenize
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

# AST nodes ast.get_docstring() accepts.
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Caps:
    hard_code_lines: int
    soft_code_lines: int


@dataclass(frozen=True)
class FileStats:
    path: Path
    code_lines: int
    # Raw physical line count. Reported by --list for context only; nothing
    # in the lint gates on it, precisely because prose moves it.
    raw_lines: int


@dataclass
class Config:
    caps: Caps
    # path -> optional tracking issue. Membership is what gates the lint;
    # the issue field is documentation only.
    allowlist: dict[str, str | None]


def _require_cap(caps_raw: dict[str, Any], key: str) -> int:
    """Read a cap key, failing with a rebase hint rather than a KeyError.

    The keys were renamed from ``hard_lines``/``soft_lines`` (raw lines,
    plus byte caps) to ``hard_code_lines``/``soft_code_lines`` in #3671.
    A branch carrying the old schema should be told what to do, not handed
    a traceback -- and must never be silently measured against a cap
    calibrated for a different metric.
    """
    if key not in caps_raw:
        raise SystemExit(
            f"{ALLOWLIST_PATH.name}: missing caps key {key!r}. The caps were "
            "re-baselined onto code-line counts in #3671 "
            "(hard_code_lines / soft_code_lines; the byte caps were dropped). "
            "Rebase this branch onto main to pick up the new allowlist schema."
        )
    return int(caps_raw[key])


def load_config(path: Path | None = None) -> Config:
    # Resolve the default at call time so tests can monkey-patch ALLOWLIST_PATH.
    if path is None:
        path = ALLOWLIST_PATH
    raw = yaml.safe_load(path.read_text()) or {}
    caps_raw = raw.get("caps") or {}
    caps = Caps(
        hard_code_lines=_require_cap(caps_raw, "hard_code_lines"),
        soft_code_lines=_require_cap(caps_raw, "soft_code_lines"),
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


def _comment_only_line_numbers(source: str) -> set[int]:
    """1-based line numbers whose only content is a comment.

    A trailing comment on a code line does not count -- the line still
    carries code. ``#`` inside a string literal is not a COMMENT token, so
    string content is never mistaken for a comment.
    """
    out: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            before = tok.line[: tok.start[1]]
            if not before.strip():
                out.add(tok.start[0])
    except tokenize.TokenError, SyntaxError, ValueError:
        # Unparseable file: fall back to counting everything as code.
        return set()
    return out


def _docstring_line_numbers(source: str) -> set[int]:
    """1-based line numbers spanned by module/class/function docstrings.

    Only the four node types ``ast.get_docstring`` accepts are considered.
    A bare string expression that is *not* in docstring position (e.g. the
    PEP 224 style ``FOO = 1`` followed by ``\"\"\"doc\"\"\"``) is counted as
    code; extending the exclusion there would re-open a way to park
    arbitrary text outside the count.
    """
    out: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError:
        # Unparseable file: fall back to counting everything as code.
        return out
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        if ast.get_docstring(node) is None:
            continue
        doc = node.body[0]
        out.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return out


def count_code_lines(source: str) -> int:
    """Count physical lines that are neither blank, comment, nor docstring.

    This is the number the lint gates on. Deleting a docstring, a comment
    or a blank line leaves it unchanged by construction -- see the module
    docstring for why that property is the point.
    """
    lines = source.splitlines()
    if not lines:
        return 0
    excluded: set[int] = {n for n, text in enumerate(lines, start=1) if not text.strip()}
    excluded |= _comment_only_line_numbers(source)
    excluded |= _docstring_line_numbers(source)
    return sum(1 for n in range(1, len(lines) + 1) if n not in excluded)


def measure(path: Path) -> FileStats:
    source = path.read_text(encoding="utf-8", errors="replace")
    return FileStats(
        path=path,
        code_lines=count_code_lines(source),
        raw_lines=len(source.splitlines()),
    )


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

    if stats.code_lines > caps.hard_code_lines:
        if not in_allowlist:
            errors.append(
                f"{rel}: {stats.code_lines} code lines exceeds hard cap "
                f"({caps.hard_code_lines} code lines). Code lines exclude "
                "docstrings, comments and blank lines, so deleting documentation "
                "will not lower this number. Decompose the file or, if you cannot "
                "in this PR, add it to scripts/file-size-allowlist.yaml with a "
                "tracking issue."
            )
        return errors, warnings

    # Soft warnings are skipped for allowlisted files -- they're already
    # tracked for decomposition.
    if in_allowlist:
        return errors, warnings

    if stats.code_lines > caps.soft_code_lines:
        warnings.append(
            f"{rel}: {stats.code_lines} code lines exceeds soft cap "
            f"({caps.soft_code_lines} code lines). "
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
            "hard_code_lines": config.caps.hard_code_lines,
            "soft_code_lines": config.caps.soft_code_lines,
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
        if stats.code_lines > config.caps.hard_code_lines:
            new_allowlist[rel] = config.allowlist.get(rel)
    write_allowlist(config, new_allowlist)
    print(f"Wrote {len(new_allowlist)} entries to {ALLOWLIST_PATH.name}")
    return 0


def list_files(repo_root: Path = REPO_ROOT) -> int:
    """Print every Python source file with its code-line count.

    Raw lines are shown alongside for context; only the code-line column
    is what the lint gates on.
    """
    rows = []
    for path in iter_source_files(repo_root):
        rel = str(path.relative_to(repo_root))
        stats = measure(path)
        rows.append((stats.code_lines, stats.raw_lines, rel))
    rows.sort(reverse=True)
    print(f"{'code':>6}  {'raw':>6}  path")
    for code_lines, raw_lines, rel in rows:
        print(f"{code_lines:>6}  {raw_lines:>6}  {rel}")
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
        help="List every source file with its code-line count; takes no action.",
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
