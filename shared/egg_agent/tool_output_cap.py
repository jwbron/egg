"""Predictive PreToolUse caps for built-in Claude Code tools (issue #2876).

Built-in tools (``Read``, ``Grep``, ``Edit``, ``Write``, ``Bash``) run
inside the Claude Code CLI; egg cannot wrap their output the way it caps
its own MCP ``@tool`` payloads (#2805). A tool result that exceeds the
Agent SDK's 1 MB JSON message buffer kills the agent with exit 255
(#2804); #2810 made that a clean fail-fast but does **not** prevent it.

This module supplies *predictive* heuristics for a PreToolUse hook: the
hook fires **before** the tool runs and denies calls whose result is
likely to overflow, returning a reason that tells the agent exactly how
to narrow the call (``offset``/``limit``/``head_limit``/
``files_with_matches``). Because the hook fires before execution it
cannot see the result, so the heuristics are necessarily approximate
(false positives/negatives are expected); #2810's fail-fast remains the
backstop when a prediction misses.

The load-bearing case is ``Read`` of a very large source file — e.g. the
24k-line ``orchestrator/routes/pipelines.py`` (~1.1 MB) that crashed the
#2777 slice-1 coder. Reading it whole produces a tool result larger than
the 1 MB buffer; redirecting the agent to ``offset``/``limit`` keeps each
page bounded.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Default byte threshold above which a whole-file ``Read`` is denied.
# The SDK buffer is 1 MB; a Read result is the file bytes plus per-line
# number prefixes (~7-8 bytes/line) plus JSON-escaping inflation, and it
# shares the 1 MB message with the rest of the turn. 256 KiB leaves ample
# headroom while still letting moderate files through whole. Override with
# EGG_READ_CAP_BYTES.
_DEFAULT_READ_CAP_BYTES = 256 * 1024


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
    """Resolve the Read byte cap from env, falling back to the default."""
    raw = os.environ.get("EGG_READ_CAP_BYTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_READ_CAP_BYTES


def _resolve_path(file_path: str, cwd: str | None) -> Path:
    """Resolve a possibly-relative tool ``file_path`` against the agent cwd."""
    path = Path(file_path)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    return path


def check_read_output_risk(tool_input: dict[str, Any], cwd: str | None) -> str | None:
    """Return a deny reason if a ``Read`` call is likely to overflow.

    Denies only when the agent supplied **no** ``limit`` (so the read is
    unbounded) and the target file exceeds the configured byte cap. A
    ``limit`` means the agent is already paging, so it is always allowed.
    Missing/unstattable files are allowed (let the real tool report the
    error).
    """
    # An explicit limit means the agent is already bounding the read.
    if tool_input.get("limit") is not None:
        return None

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

    approx_kb = size // 1024
    return (
        f"Read denied: '{file_path}' is ~{approx_kb} KB, large enough that "
        f"reading it whole risks overflowing the agent's 1 MB message buffer "
        f"and crashing the session (issue #2804). Re-run Read with 'offset' "
        f"and 'limit' to page through it (e.g. offset=1, limit=2000), or use "
        f"Grep with output_mode='files_with_matches' / a 'head_limit' to "
        f"locate the lines you need first, then Read that range."
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
