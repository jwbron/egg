"""Predictive PreToolUse caps for built-in Claude Code tools (issue #2876).

These caps are **model-context/cost discipline, not the buffer-crash fix.**
The Agent SDK reader's buffer-overflow crash (#2804/#2884) is prevented by
raising ``max_buffer_size`` in ``client.py`` — see the note there: the
messages that overflow the reader are dominated by *non-model-bound* transcript
metadata (Claude Code attaches the whole original file to every Edit/Write
result), which a per-tool input/output cap cannot and should not police.

What this module *does* police is the volume a tool sends **to the model**.
A whole-file ``Read`` returns the file's content to the model (the ~1.1 MB,
24k-line ``orchestrator/routes/pipelines.py`` ≈ ~275k tokens), and a whole-repo
content ``Grep`` dumps every matching line to the model — both wasteful of
context and cost. Built-in tools (``Read``, ``Grep``, ``Bash``) run inside the
Claude Code CLI; egg cannot wrap their output the way it caps its own MCP
``@tool`` payloads (#2805), so a PreToolUse hook fires **before** the tool runs
and denies calls whose model-bound result is likely to be excessive, returning a
reason that tells the agent how to narrow the call (``offset``/``limit``/
``head_limit``/``files_with_matches``). Because the hook fires before execution
it cannot see the result, so the heuristics are necessarily approximate (false
positives/negatives are expected). Keeping model-bound output small here also
spares the reader buffer from having to absorb it; the raised buffer plus
#2810's fail-fast cover the crash path independently.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    from egg_logging import get_logger

    logger: Any = get_logger("egg-agent")
except ImportError:  # pragma: no cover - egg_logging always present in-sandbox
    logger = logging.getLogger(__name__)

# Default byte threshold above which a whole-file ``Read`` is denied.
# The SDK buffer is 1 MB; a Read result is the file bytes plus per-line
# number prefixes (~7-8 bytes/line) plus JSON-escaping inflation, and it
# shares the 1 MB message with the rest of the turn. 256 KiB leaves ample
# headroom while still letting moderate files through whole. Override with
# EGG_READ_CAP_BYTES.
_DEFAULT_READ_CAP_BYTES = 256 * 1024

# Rough average bytes per source line, used to estimate how many bytes a
# *bounded* Read (``limit`` lines) will return. Deliberately conservative
# (real source averages well under this) so a normal paging limit like
# ``limit=2000`` stays under the 256 KiB default while an absurd
# ``limit=10_000_000`` is still recognised as unbounded.
_EST_BYTES_PER_LINE = 128

# Binary file types ``Read`` returns whole, where line-based ``offset`` /
# ``limit`` paging does not apply. PDFs are the exception — they page via
# ``pages`` (the Read tool caps a request at 20 pages), so a ``pages``-scoped
# PDF read is bounded and allowed. Notebooks are JSON but Read returns every
# cell whole, so they get a ``jq``-oriented remedy rather than the generic one.
_PDF_EXTENSION = ".pdf"
_NOTEBOOK_EXTENSION = ".ipynb"
_NON_PAGEABLE_BINARY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".ico", _NOTEBOOK_EXTENSION}
)

# Raw EGG_READ_CAP_BYTES values we've already warned about, so a misconfigured
# knob logs once per distinct value rather than on every Read in the session.
_warned_cap_values: set[str] = set()


def is_output_cap_disabled() -> bool:
    """True when the predictive cap is switched off via env.

    Mirrors the EGG_MCP_TOOLS kill-switch convention so operators can
    disable the heuristic without a code change if it proves too noisy.
    """
    return os.environ.get("EGG_TOOL_OUTPUT_CAP", "").strip().lower() in (
        "false",
        "0",
        "no",
        "off",
    )


def _read_cap_bytes() -> int:
    """Resolve the Read byte cap from env, falling back to the default.

    ``EGG_READ_CAP_BYTES`` is an operator tuning knob, so a *set-but-invalid*
    value is logged loudly before falling back — an operator who typoed the
    value or used an unsupported suffix (``2mb``, ``0``, a negative) would
    otherwise silently get the default and a different false-positive rate
    than they intended. The unset case stays silent (the default is expected).
    """
    raw = os.environ.get("EGG_READ_CAP_BYTES", "").strip()
    if not raw:
        return _DEFAULT_READ_CAP_BYTES
    try:
        value = int(raw)
    except ValueError:
        _warn_invalid_cap(raw, "is not an integer")
        return _DEFAULT_READ_CAP_BYTES
    if value <= 0:
        _warn_invalid_cap(raw, "must be a positive integer")
        return _DEFAULT_READ_CAP_BYTES
    return value


def _warn_invalid_cap(raw: str, problem: str) -> None:
    """Warn that an invalid EGG_READ_CAP_BYTES is being ignored, once per value.

    The cap is resolved on every ``Read``, so warning unconditionally would emit
    hundreds of identical lines for one misconfiguration. Track the raw values
    already warned about so a fixed-then-re-broken knob still warns on the new
    bad value, but a steady bad value warns only once.
    """
    if raw in _warned_cap_values:
        return
    _warned_cap_values.add(raw)
    logger.warning(
        f"EGG_READ_CAP_BYTES={raw!r} {problem}; ignoring it and using the "
        f"default {_DEFAULT_READ_CAP_BYTES} bytes"
    )


def _resolve_path(file_path: str, cwd: str | None) -> Path:
    """Resolve a possibly-relative tool ``file_path`` against the agent cwd."""
    path = Path(file_path)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    return path


def _coerce_positive_int(value: Any) -> int | None:
    """Return ``value`` as a positive int, or None if it isn't one."""
    try:
        n = int(value)
    except TypeError, ValueError:
        return None
    return n if n > 0 else None


def _read_remedy(suffix: str, cap: int) -> str:
    """Build the deny-message remedy clause tailored to the file type.

    Line-based ``offset``/``limit`` paging only makes sense for text files;
    for PDFs the agent should page with ``pages``, for notebooks it should
    pull individual cells out with ``jq``, and for other binaries (images)
    ``Read`` returns the whole file so no paging applies.
    """
    if suffix == _PDF_EXTENSION:
        return (
            "Re-run Read with the 'pages' parameter to read a bounded page "
            "range (e.g. pages='1-5')."
        )
    if suffix == _NOTEBOOK_EXTENSION:
        return (
            "Read returns the whole notebook (every cell and its outputs), so "
            "line paging does not apply. Inspect individual cells with Bash and "
            "jq instead (e.g. jq '.cells[].source' notebook.ipynb)."
        )
    if suffix in _NON_PAGEABLE_BINARY_EXTENSIONS:
        return (
            "This binary file is returned whole and cannot be paged, so it "
            "cannot be read without risking the overflow. Avoid reading it "
            "whole; if you only need metadata, use Bash (e.g. 'file' or 'stat')."
        )
    suggested_limit = max(1, cap // _EST_BYTES_PER_LINE)
    return (
        f"Re-run Read with 'offset' and 'limit' to page through it "
        f"(e.g. offset=1, limit={suggested_limit}), or use Grep with "
        f"output_mode='files_with_matches' / a 'head_limit' to locate the "
        f"lines you need first, then Read that range."
    )


def check_read_output_risk(tool_input: dict[str, Any], cwd: str | None) -> str | None:
    """Return a deny reason if a ``Read`` call is likely to overflow.

    Denies when the target file exceeds the configured byte cap and the read
    is not bounded to a small enough range. A text read is "bounded" when its
    ``limit`` × ~bytes-per-line estimate stays under the cap — a mere ``limit``
    is *not* a free pass, since ``limit=10_000_000`` would still read the whole
    file (the #2810 fail-fast caught that gap). A PDF is "bounded" when a
    non-empty ``pages`` range is given (the analogue of ``limit`` for PDFs, and
    the mechanism the deny remedy points at — capped at 20 pages by the Read
    tool). Other binaries (images, notebooks) ignore ``offset``/``limit``/
    ``pages``, so they are judged on size alone. Missing/unstattable files are
    allowed (let the real tool report the error).
    """
    file_path = tool_input.get("file_path")
    if not file_path:
        return None

    path = _resolve_path(str(file_path), cwd)
    try:
        size = path.stat().st_size
    except OSError:
        # Missing/unreadable — let the real Read tool surface the error.
        return None

    cap = _read_cap_bytes()
    if size <= cap:
        return None

    suffix = path.suffix.lower()
    is_pdf = suffix == _PDF_EXTENSION

    if is_pdf:
        # A `pages`-scoped PDF read is bounded (the Read tool caps it at 20
        # pages), so it must not be denied while the remedy points at `pages`.
        pages = tool_input.get("pages")
        if pages is not None and str(pages).strip():
            return None
    elif suffix not in _NON_PAGEABLE_BINARY_EXTENSIONS:
        # A bounded text read whose estimated payload fits under the cap is safe.
        # (Binary reads ignore offset/limit, so a limit never makes them safe.)
        limit = _coerce_positive_int(tool_input.get("limit"))
        if limit is not None and min(size, limit * _EST_BYTES_PER_LINE) <= cap:
            return None

    approx_kb = size // 1024
    return (
        f"Read denied: '{file_path}' is ~{approx_kb} KB, large enough that "
        f"reading it whole risks overflowing the agent's 1 MB message buffer "
        f"and crashing the session (issue #2804). {_read_remedy(suffix, cap)}"
    )


def check_grep_output_risk(tool_input: dict[str, Any]) -> str | None:
    """Return a deny reason if a ``Grep`` call is likely to overflow.

    Targets the genuinely unbounded case: ``output_mode='content'`` with
    no ``head_limit`` **and** no path/glob narrowing, i.e. dumping every
    matching line across the whole repo. Content greps that are scoped
    (by ``path`` or ``glob``) or capped (by ``head_limit``) are allowed —
    the heuristic deliberately stays narrow to avoid denying the common,
    small content grep.
    """
    if tool_input.get("output_mode") != "content":
        return None
    if tool_input.get("head_limit") is not None:
        return None
    # Scoped to a subtree or file glob → bounded enough; allow.
    if tool_input.get("path") or tool_input.get("glob"):
        return None

    return (
        "Grep denied: output_mode='content' across the whole repo with no "
        "'head_limit' can return an unbounded volume of matching lines and "
        "overflow the agent's 1 MB message buffer (issue #2804). Add a "
        "'head_limit' (e.g. head_limit=100), scope the search with 'path' or "
        "'glob', or use output_mode='files_with_matches' to list files first "
        "and then Read the relevant ranges."
    )


def check_builtin_tool_output_risk(
    tool_name: str, tool_input: dict[str, Any], cwd: str | None
) -> str | None:
    """Dispatch a built-in tool call to its predictive-cap checker.

    Returns a deny reason string, or None when the call is allowed (or the
    cap is disabled via EGG_TOOL_OUTPUT_CAP).
    """
    if is_output_cap_disabled():
        return None
    if tool_name == "Read":
        return check_read_output_risk(tool_input, cwd)
    if tool_name == "Grep":
        return check_grep_output_risk(tool_input)
    return None
