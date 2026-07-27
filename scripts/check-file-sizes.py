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

Two things are still counted as code on purpose:

- Multi-line string literals that are not docstrings. An embedded prompt
  template really does make a module longer to work through, and without
  this the gaming vector would just move to ``_DOC = \"\"\"...\"\"\"``. The
  mirror-image hole is a payload parked in *docstring* position and read
  back through ``__doc__``; the byte backstop below is what bounds that,
  since by construction the code-line metric cannot.
- Files that fail to parse. A ``SyntaxError`` only ever removes
  exclusions -- ``ast.parse`` failing gives up the docstring spans, and
  ``tokenize`` failing gives up the comment-only lines, each independently
  -- so a broken file can never measure smaller than a working one.

Caps
----
- Hard cap (failure): 1000 code lines.
- Soft cap (warning): 500 code lines.
- Byte backstop (failure): 150,000 raw bytes.

The line caps were re-baselined against code-line counts in issue #3671.
At this repo's median code density (~0.58 code lines per raw line for
files over 200 lines) 1000 code lines is ~1700 raw lines; per-code-line
byte density runs ~65 B/line at p50 and ~93 B/line at p95, with a
worst-observed ~156 B/line, so a file at the cap lands anywhere from
~65KB to ~156KB. The soft cap sits at half the hard cap, matching the old
800/1500 shape.

The byte cap is a *pathology backstop*, not a design constraint, and the
two caps do different jobs:

- **Code lines** bound how much logic lives in one module. This is the
  design cap, and deleting prose never moves it -- the point of #3671.
- **Raw bytes** bound what a Read of the file actually costs. This is the
  only metric that sees unbounded prose, which the primary metric now
  ignores by design. Left unbounded, a 544KB module with a padded
  docstring passes clean; ``routes/pipelines.py`` genuinely reached
  30,520 lines / 1.44MB in this repo's history.

150,000 is set deliberately loose -- roughly 1.5x the largest file in the
tree (101KB) and above the ~156KB-worst-case projection only for files
that are also far over the line cap. That looseness is what keeps the
prose-gaming property closed: the 100,000-byte cap it replaces sat close
enough to real files to make trimming a docstring a rational move, while
nobody trims prose to get from 155KB to 149KB. Only a file that has gone
genuinely wrong ever sees it. There is no soft byte cap, for the same
reason -- a warning that near real files would re-create the pressure.

Test files are exempt -- parametrized cases legitimately push line counts
past these caps and decomposing them mechanically would hurt readability.

Files over the hard cap are listed in ``scripts/file-size-allowlist.yaml``.
The lint allows allowlisted files to stay over both hard caps regardless
of size; per-file size baselines were dropped because every unrelated PR
that touched one of these files needed a baseline bump in the allowlist,
conflicting with every other in-flight PR. The ratchet only turns one way:
an allowlisted file that no longer exists, or that has dropped back under
*both* hard caps, is reported as a stale entry and fails the lint until
the exemption is removed.

Usage:
    scripts/check-file-sizes.py
    scripts/check-file-sizes.py --update-allowlist  # add over-cap files
    scripts/check-file-sizes.py --list              # report all files

Exit codes:
    0  no violations
    1  one or more non-allowlisted files exceed a hard cap, or the
       allowlist carries an entry that no longer needs an exemption
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

# Prose for the generated allowlist file. It lives here, not in the YAML,
# because --update-allowlist rewrites that file from scratch and would
# otherwise delete it -- see write_allowlist.
_ALLOWLIST_HEADER = """\
Allowlist for scripts/check-file-sizes.py.

GENERATED HEADER: scripts/check-file-sizes.py --update-allowlist rewrites
this file from _ALLOWLIST_HEADER / _FILES_HEADER in that script. Edit the
prose there, not here, or the next run will drop it. Per-entry rationale
belongs in an entry's `note:` field for the same reason; the per-entry
measurement comments are recomputed on every write.

Each entry grandfathers a Python source file so the lint allows it to
remain over the global caps. Allowlisted files may grow freely -- the
allowlist's only role is to say "this file is exempt from the global size
caps." Decompose listed files in follow-up PRs; the lint reports an entry
as stale, and fails, once its file drops back under the caps or stops
existing, so an exemption cannot outlive the condition it was granted for.

Per-file size baselines were dropped because every unrelated PR that
touched one of these files needed a baseline bump here, which conflicted
with every other in-flight PR.

The line caps count CODE lines only: blank lines, comment-only lines and
module/class/function docstrings are excluded (#3671). Deleting prose
therefore does not move a file's number at all. Re-baselined from
1500/800 raw lines; at this repo's median density (~0.58 code lines per
raw line) 1000 code lines is ~1700 raw lines.

hard_bytes is a pathology backstop, not a design constraint. Code lines
are the cap that shapes the codebase; raw bytes are the only metric that
still sees unbounded prose, so they bound what reading the file costs.
150,000 is set far above where real files sit (the largest is ~101KB) --
that looseness is deliberate, because a byte cap close to real files is
exactly what made trimming a docstring a rational move before #3671.
There is no soft byte cap for the same reason.

Schema:
  caps: { hard_code_lines, soft_code_lines, hard_bytes }
  files: { <repo-relative path>: null | { issue: str, note: str } }\
"""

_FILES_HEADER = """\
The file-size decomposition program (#3312, continued by #3450/#3447) is
COMPLETE: every giant it targeted has been decomposed into a sub-package
whose barrel + submodules are all under the global cap. New entries should
only be added, with a tracking issue, for files awaiting their own
decomposition.\
"""


@dataclass(frozen=True)
class Caps:
    hard_code_lines: int
    soft_code_lines: int
    # Pathology backstop on raw file size. Deliberately far above where
    # real files sit -- see the module docstring for why the looseness is
    # the point rather than a compromise.
    hard_bytes: int


@dataclass(frozen=True)
class FileStats:
    path: Path
    code_lines: int
    # Raw physical line count. Reported by --list for context only; the
    # line caps do not gate on it, precisely because prose moves it.
    raw_lines: int
    size_bytes: int


@dataclass(frozen=True)
class AllowlistEntry:
    """Documentation attached to one allowlisted path.

    Membership in the allowlist is what gates the lint; both fields are
    documentation. They live in the structured entry rather than in YAML
    comments so ``--update-allowlist`` cannot destroy them -- see
    ``write_allowlist``.
    """

    # Tracking issue for the file's decomposition, e.g. "3498".
    issue: str | None = None
    # One-line rationale for why this file is exempt.
    note: str | None = None


@dataclass
class Config:
    caps: Caps
    allowlist: dict[str, AllowlistEntry]


def _require_cap(caps_raw: dict[str, Any], key: str) -> int:
    """Read a cap key, failing with a rebase hint rather than a KeyError.

    The keys were renamed from ``hard_lines``/``soft_lines`` (raw lines)
    to ``hard_code_lines``/``soft_code_lines`` in #3671, and ``soft_bytes``
    was dropped. A branch carrying the old schema should be told what to
    do, not handed a traceback -- and must never be silently measured
    against a cap calibrated for a different metric.

    The value is validated too: a non-integer or non-positive cap is a
    typo in a config file that gates CI, so it gets the same friendly
    ``SystemExit`` rather than a raw ``ValueError`` from ``int()`` or a
    silently vacuous cap.
    """
    if key not in caps_raw:
        raise SystemExit(
            f"{ALLOWLIST_PATH.name}: missing caps key {key!r}. The caps were "
            "re-baselined onto code-line counts in #3671 "
            "(hard_code_lines / soft_code_lines / hard_bytes; soft_bytes was "
            "dropped). Rebase this branch onto main to pick up the new "
            "allowlist schema."
        )
    raw = caps_raw[key]
    try:
        value = int(raw)
    except TypeError, ValueError:
        raise SystemExit(
            f"{ALLOWLIST_PATH.name}: caps key {key!r} must be an integer, got {raw!r}."
        ) from None
    if value <= 0:
        raise SystemExit(
            f"{ALLOWLIST_PATH.name}: caps key {key!r} must be positive, got {value}. "
            "A zero or negative cap would silently reject (or never gate) every file."
        )
    return value


def load_config(path: Path | None = None) -> Config:
    # Resolve the default at call time so tests can monkey-patch ALLOWLIST_PATH.
    if path is None:
        path = ALLOWLIST_PATH
    raw = yaml.safe_load(path.read_text()) or {}
    caps_raw = raw.get("caps") or {}
    caps = Caps(
        hard_code_lines=_require_cap(caps_raw, "hard_code_lines"),
        soft_code_lines=_require_cap(caps_raw, "soft_code_lines"),
        hard_bytes=_require_cap(caps_raw, "hard_bytes"),
    )
    files_raw: dict[str, Any] = raw.get("files") or {}
    allowlist: dict[str, AllowlistEntry] = {}
    for rel, entry in files_raw.items():
        if isinstance(entry, dict):
            issue = entry.get("issue")
            note = entry.get("note")
            allowlist[rel] = AllowlistEntry(
                issue=str(issue) if issue is not None else None,
                note=str(note) if note is not None else None,
            )
        else:
            allowlist[rel] = AllowlistEntry()
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


def _physical_lines(source: str) -> list[str]:
    """Split ``source`` the way the tokenizer does.

    ``str.splitlines()`` is *not* usable here: it also breaks on
    ``\\x0b \\x0c \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029``, which Python's
    tokenizer treats as ordinary characters. A single such character
    inside a string literal would shift every subsequent index by one and
    land the comment/docstring exclusions on the wrong physical lines.
    ``readlines()`` uses the tokenizer's own line model, so the 1-based
    numbers from ``tokenize`` and ``ast`` index straight into this list.
    """
    return io.StringIO(source).readlines()


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


def _shares_a_line_with_code(doc: ast.stmt, lines: list[str]) -> bool:
    """True if the docstring's first or last physical line also holds code.

    ``def f(): \"\"\"doc\"\"\"`` and ``\"\"\"doc\"\"\"; x = 1`` are both legal, and
    excluding their line ranges wholesale would delete real code from the
    count. A trailing comment after the closing quotes is fine -- that
    part of the line is not code either.
    """
    first = lines[doc.lineno - 1] if doc.lineno - 1 < len(lines) else ""
    if first[: doc.col_offset].strip():
        return True
    end_lineno = doc.end_lineno or doc.lineno
    last = lines[end_lineno - 1] if end_lineno - 1 < len(lines) else ""
    after = last[doc.end_col_offset :].strip() if doc.end_col_offset is not None else ""
    return bool(after) and not after.startswith("#")


def _docstring_line_numbers(source: str, lines: list[str]) -> set[int]:
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
        if _shares_a_line_with_code(doc, lines):
            continue
        out.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return out


def count_code_lines(source: str) -> int:
    """Count physical lines that are neither blank, comment, nor docstring.

    This is the number the lint gates on. Deleting a docstring, a comment
    or a blank line leaves it unchanged by construction -- see the module
    docstring for why that property is the point.
    """
    lines = _physical_lines(source)
    if not lines:
        return 0
    excluded: set[int] = {n for n, text in enumerate(lines, start=1) if not text.strip()}
    excluded |= _comment_only_line_numbers(source)
    excluded |= _docstring_line_numbers(source, lines)
    return sum(1 for n in range(1, len(lines) + 1) if n not in excluded)


def measure(path: Path) -> FileStats:
    data = path.read_bytes()
    source = data.decode("utf-8", errors="replace")
    return FileStats(
        path=path,
        code_lines=count_code_lines(source),
        raw_lines=len(_physical_lines(source)),
        size_bytes=len(data),
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

    if stats.code_lines > caps.hard_code_lines or stats.size_bytes > caps.hard_bytes:
        if in_allowlist:
            return errors, warnings
        if stats.code_lines > caps.hard_code_lines:
            errors.append(
                f"{rel}: {stats.code_lines} code lines exceeds hard cap "
                f"({caps.hard_code_lines} code lines). Code lines exclude "
                "docstrings, comments and blank lines, so deleting documentation "
                "will not lower this number. Decompose the file or, if you cannot "
                "in this PR, add it to scripts/file-size-allowlist.yaml with a "
                "tracking issue."
            )
        if stats.size_bytes > caps.hard_bytes:
            errors.append(
                f"{rel}: {stats.size_bytes} bytes exceeds the byte backstop "
                f"({caps.hard_bytes} bytes). This cap is a pathology check on what "
                "reading the file actually costs, not a prose budget -- it sits far "
                "above any healthy file, so trimming documentation is neither the "
                "intended fix nor enough of one. Decompose the file or, if you "
                "cannot in this PR, add it to scripts/file-size-allowlist.yaml with "
                "a tracking issue."
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


def _is_over_a_hard_cap(stats: FileStats, caps: Caps) -> bool:
    return stats.code_lines > caps.hard_code_lines or stats.size_bytes > caps.hard_bytes


def find_stale_entries(measured: dict[str, FileStats], config: Config) -> list[str]:
    """Report allowlist entries that no longer need to exist.

    An entry is stale when its file is gone *or* when the file has dropped
    back under every hard cap. Only checking existence would let an
    exemption outlive the condition it was granted for: the #3671
    re-baseline alone left five entries sitting comfortably under the cap,
    and ``make lint`` said nothing about any of them -- they were caught
    only by re-measuring by hand. Since an allowlisted file may grow
    without limit, a stale entry is an open-ended exemption, so this is
    reported at the lint layer (and as an error, not a warning) rather
    than left to a unit test the author of an unrelated PR never runs.
    """
    caps = config.caps
    stale: list[str] = []
    for rel in sorted(config.allowlist):
        stats = measured.get(rel)
        if stats is None:
            stale.append(
                f"{rel}: file no longer exists. Remove the entry from {ALLOWLIST_PATH.name}."
            )
        elif not _is_over_a_hard_cap(stats, caps):
            stale.append(
                f"{rel}: now under the caps ({stats.code_lines} code lines / "
                f"{stats.size_bytes} bytes, caps are {caps.hard_code_lines} / "
                f"{caps.hard_bytes}). The exemption is no longer needed -- remove "
                f"the entry from {ALLOWLIST_PATH.name} so the file is held to the "
                "cap from here on."
            )
    return stale


def check_all(repo_root: Path | None = None) -> tuple[list[str], list[str], list[str]]:
    """Run the full check. Returns (errors, warnings, stale_allowlist_entries)."""
    # Resolve the default at call time, matching load_config, so tests can
    # monkey-patch REPO_ROOT.
    if repo_root is None:
        repo_root = REPO_ROOT
    config = load_config()
    errors: list[str] = []
    warnings: list[str] = []
    measured: dict[str, FileStats] = {}

    for path in iter_source_files(repo_root):
        rel = str(path.relative_to(repo_root))
        stats = measure(path)
        measured[rel] = stats
        file_errors, file_warnings = evaluate(stats, rel, config)
        errors.extend(file_errors)
        warnings.extend(file_warnings)

    return errors, warnings, find_stale_entries(measured, config)


def _comment_block(text: str) -> str:
    """Render a paragraph block as YAML comment lines."""
    return "".join(f"#{' ' + line if line else ''}\n" for line in text.splitlines())


def _dump_entry(rel: str, entry: AllowlistEntry) -> str:
    """Render one ``files:`` entry, indented two spaces."""
    body: dict[str, Any] = {}
    if entry.issue is not None:
        body["issue"] = entry.issue
    if entry.note is not None:
        body["note"] = entry.note
    dumped = yaml.safe_dump({rel: body or None}, sort_keys=False, width=10_000)
    return "".join(f"  {line}\n" for line in dumped.splitlines())


def write_allowlist(
    config: Config,
    allowlist: dict[str, AllowlistEntry],
    measured: dict[str, FileStats] | None = None,
) -> None:
    """Rewrite the allowlist file: header, caps, sorted entries.

    The header and the per-entry measurement comments are *generated*,
    not preserved. That is the point: a plain ``yaml.safe_dump`` of the
    parsed document silently deleted every comment in the file, which for
    this file is most of its substance -- the schema, the cap
    calibration, and each entry's rationale. So nothing load-bearing is
    allowed to live in a hand-written comment: the prose that explains the
    file lives here in the script, each entry's rationale lives in its
    structured ``note:`` field, and the measurements are recomputed on
    every write rather than hand-maintained (which also means they cannot
    go stale). Editing the header means editing this constant.
    """
    parts = [_comment_block(_ALLOWLIST_HEADER), "caps:\n"]
    for key, value in (
        ("hard_code_lines", config.caps.hard_code_lines),
        ("soft_code_lines", config.caps.soft_code_lines),
        ("hard_bytes", config.caps.hard_bytes),
    ):
        parts.append(f"  {key}: {value}\n")
    parts.append("\n")
    parts.append(_comment_block(_FILES_HEADER))
    parts.append("files:" + ("\n" if allowlist else " {}\n"))
    for rel, entry in sorted(allowlist.items()):
        stats = (measured or {}).get(rel)
        if stats is not None:
            parts.append(
                f"  # {stats.code_lines} code lines of {stats.raw_lines} raw, "
                f"{stats.size_bytes / 1000:.0f}KB.\n"
            )
        parts.append(_dump_entry(rel, entry))
    ALLOWLIST_PATH.write_text("".join(parts))


def update_allowlist(repo_root: Path = REPO_ROOT) -> int:
    """Sync the allowlist with the current set of over-cap files.

    Adds over-cap files not yet listed (with no issue link) and drops
    entries for files that have dropped back under the caps or no longer
    exist. The ``issue:`` and ``note:`` fields on each existing entry are
    carried forward; losing them would drop the link between the file and
    its decomposition follow-up.
    """
    config = load_config()
    new_allowlist: dict[str, AllowlistEntry] = {}
    measured: dict[str, FileStats] = {}
    for path in iter_source_files(repo_root):
        rel = str(path.relative_to(repo_root))
        stats = measure(path)
        if _is_over_a_hard_cap(stats, config.caps):
            new_allowlist[rel] = config.allowlist.get(rel, AllowlistEntry())
            measured[rel] = stats
    write_allowlist(config, new_allowlist, measured)
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
        rows.append((stats.code_lines, stats.raw_lines, stats.size_bytes, rel))
    rows.sort(reverse=True)
    print(f"{'code':>6}  {'raw':>6}  {'bytes':>8}  path")
    for code_lines, raw_lines, size_bytes, rel in rows:
        print(f"{code_lines:>6}  {raw_lines:>6}  {size_bytes:>8}  {rel}")
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

    # Stale entries fail the lint rather than warn: an unneeded exemption
    # is an unbounded one, and a warning is exactly what let five of them
    # sit unnoticed through the #3671 re-baseline.
    failures = errors + [f"stale allowlist entry: {s}" for s in stale]

    if failures:
        print()
        print("ERROR: file-size lint failed")
        print("=" * 76)
        for f in failures:
            print(f"  - {f}")
        print()
        print("Run `scripts/check-file-sizes.py --list` to see all files ranked by size.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
