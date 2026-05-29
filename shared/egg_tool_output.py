"""Tool-output size caps for egg-owned MCP tools (issue #2805).

The Claude Agent SDK message reader has a hard 1 MB JSON buffer; a tool
result that exceeds it kills the agent with exit 255 (#2804). #2810 made
that crash observable and terminal, but the *prevention* — never producing
an oversized payload in the first place — lives here.

This module is the shared helper referenced by #2805's "consistent across
tools" requirement. It is deliberately a flat, stdlib-only module (no
package ``__init__`` side effects, no ``claude_agent_sdk`` import) so both
sides of the boundary can import it once ``shared/`` is on ``sys.path``:

* the **orchestrator** MCP server (operator-facing tools in
  ``orchestrator/mcp_tools.py``), and
* the **sandbox** agent ``@tool`` wrappers
  (``sandbox/egg_agent_tools/tools/_common.py``).

Two strategies are exposed:

* :func:`cap_result_dict` / :func:`cap_text` — *truncate + structured
  marker*. The tail is replaced with a JSON object that names what was cut
  and how to narrow the call. Simple; the right fit for paginated or
  structured tools where a head preview plus a "narrow it" hint is enough.
* :func:`spill_to_file` — *write full result to disk + return a preview*.
  Mirrors Claude Code's own ``Bash`` tool (inline preview, full output
  spilled to a file the model can re-``Read`` / ``grep``). The right fit
  for large, unpaginated raw content (e.g. a full checkpoint transcript)
  where the tail matters and truncation would lose it. Only usable when
  the caller and the reader share a filesystem (in-sandbox agent tools),
  so the orchestrator MCP server uses truncation only.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Any

# Default cap, well under the SDK's 1 MB reader buffer so the serialized
# result still has headroom for the surrounding SDK/MCP framing. Override
# with ``EGG_TOOL_OUTPUT_CAP_BYTES`` (issue #2805 "configurable").
DEFAULT_CAP_BYTES = 100 * 1024

# Reserve for the non-preview marker fields so the assembled marker still
# fits under the cap; the preview is then shrunk to fit if needed.
_MARKER_RESERVE_BYTES = 2048

# Sentinel keys so callers/tests can recognize a capped payload.
TRUNCATION_KEY = "_egg_truncated"
SPILL_KEY = "_egg_output_spilled"


def cap_bytes_from_env(default: int = DEFAULT_CAP_BYTES) -> int:
    """Resolve the configured cap, falling back to ``default`` on bad input."""
    raw = os.environ.get("EGG_TOOL_OUTPUT_CAP_BYTES")
    if not raw:
        return default
    try:
        value = int(raw)
    except TypeError, ValueError:
        return default
    return value if value > 0 else default


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncation_marker(
    text: str,
    *,
    cap_bytes: int,
    tool: str | None,
    narrow_hint: str | None,
) -> dict[str, Any]:
    """Build a structured marker dict that fits under ``cap_bytes`` serialized.

    The preview is the head of ``text`` shrunk (by bytes, re-checking the
    serialized size) until the whole marker fits — robust to JSON escape
    expansion of the preview.
    """
    original_bytes = _utf8_len(text)
    hint = narrow_hint or (
        "narrow the call (e.g. a smaller limit/lines window, a more "
        "specific filter, or pagination) and retry"
    )
    note = (
        f"Result was {original_bytes} bytes; it exceeded the {cap_bytes}-byte "
        "tool-output cap and was truncated to avoid the Agent SDK 1 MB "
        "message-buffer crash (#2804/#2805). Only the head is shown below — "
        f"to see the rest, {hint}."
    )
    budget = max(cap_bytes - _MARKER_RESERVE_BYTES, 256)
    preview = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    marker: dict[str, Any] = {
        TRUNCATION_KEY: True,
        "tool": tool,
        "original_bytes": original_bytes,
        "cap_bytes": cap_bytes,
        "note": note,
        "preview": preview,
    }
    # Shrink the preview until the serialized marker fits. JSON-escaping can
    # expand the preview past the byte budget, so check the real size.
    while _utf8_len(json.dumps(marker, default=str)) > cap_bytes and preview:
        preview = preview[: len(preview) // 2]
        marker["preview"] = preview
    return marker


def cap_text(
    text: str,
    *,
    tool: str | None = None,
    narrow_hint: str | None = None,
    cap_bytes: int | None = None,
) -> str:
    """Cap an already-serialized text payload.

    Returns ``text`` unchanged when it is within the cap, otherwise a JSON
    string holding the truncation marker (head preview + how to narrow).
    """
    limit = cap_bytes if cap_bytes is not None else cap_bytes_from_env()
    if _utf8_len(text) <= limit:
        return text
    marker = _truncation_marker(text, cap_bytes=limit, tool=tool, narrow_hint=narrow_hint)
    return json.dumps(marker, default=str)


def cap_result_dict(
    result: dict[str, Any],
    *,
    tool: str | None = None,
    narrow_hint: str | None = None,
    cap_bytes: int | None = None,
) -> dict[str, Any]:
    """Cap a result *dict* by its serialized size.

    Returns ``result`` unchanged when within the cap, otherwise the
    truncation marker dict (a small, well-formed replacement). Used at the
    orchestrator MCP chokepoint, where the result is re-serialized by the
    server; the marker dict keeps that serialization small.
    """
    limit = cap_bytes if cap_bytes is not None else cap_bytes_from_env()
    try:
        text = json.dumps(result, default=str)
    except TypeError, ValueError:
        text = str(result)
    if _utf8_len(text) <= limit:
        return result
    return _truncation_marker(text, cap_bytes=limit, tool=tool, narrow_hint=narrow_hint)


def spill_to_file(
    text: str,
    *,
    tool: str,
    preview_lines: int = 50,
    cap_bytes: int | None = None,
    spill_dir: str | None = None,
) -> dict[str, Any] | None:
    """Spill an oversized payload to a file and return a preview descriptor.

    Returns ``None`` when ``text`` is within the cap (caller should use the
    payload as-is). Otherwise writes the full ``text`` to a file the agent
    can re-read with ``Read`` (offset/limit) or ``grep`` via ``Bash``, and
    returns ``{output_path, total_bytes, preview, ...}``.

    Only appropriate when the caller and the agent share a filesystem
    (in-sandbox tools). On any write failure, returns ``None`` so the
    caller falls back to truncation rather than failing the tool call.
    """
    limit = cap_bytes if cap_bytes is not None else cap_bytes_from_env()
    total_bytes = _utf8_len(text)
    if total_bytes <= limit:
        return None

    target_dir = spill_dir or tempfile.gettempdir()
    path = os.path.join(target_dir, f"egg-tool-out-{tool}-{uuid.uuid4().hex}.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None

    preview = "\n".join(text.splitlines()[:preview_lines])
    # Keep the preview itself under the cap (a single huge line can blow it).
    preview = cap_text(preview, tool=tool, cap_bytes=limit)
    return {
        SPILL_KEY: True,
        "tool": tool,
        "output_path": path,
        "total_bytes": total_bytes,
        "cap_bytes": limit,
        "note": (
            f"Result was {total_bytes} bytes (over the {limit}-byte tool-output "
            "cap), so the full output was written to `output_path` to avoid the "
            "Agent SDK 1 MB buffer crash (#2804/#2805). Read it with the `Read` "
            "tool (use `offset`/`limit`) or `grep` it via `Bash`. Only the first "
            f"{preview_lines} lines are inlined below."
        ),
        "preview": preview,
    }
