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
import logging
import os
import tempfile
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Default cap, well under the SDK's 1 MB reader buffer so the serialized
# result still has headroom for the surrounding SDK/MCP framing. Override
# with ``EGG_TOOL_OUTPUT_CAP_BYTES`` (issue #2805 "configurable").
DEFAULT_CAP_BYTES = 100 * 1024

# Reserve for the non-preview marker fields so the assembled marker still
# fits under the cap; the preview is then shrunk to fit if needed.
_MARKER_RESERVE_BYTES = 2048

# Inline preview budget for a spill descriptor — small and fixed, so the
# descriptor stays tiny regardless of the configured cap. The full content
# lives in the spilled file; the preview is just a head sample.
_SPILL_PREVIEW_BYTES = 4 * 1024

# Best-effort age threshold for pruning stale spill files. Old enough that
# the agent has almost certainly finished reading any earlier spill.
_SPILL_TTL_SECONDS = 60 * 60

# Sentinel keys so callers/tests can recognize a capped payload.
TRUNCATION_KEY = "_egg_truncated"
SPILL_KEY = "_egg_output_spilled"


def cap_bytes_from_env(default: int = DEFAULT_CAP_BYTES) -> int:
    """Resolve the configured cap, falling back to ``default`` on bad input.

    ``EGG_TOOL_OUTPUT_CAP_BYTES`` is operator-facing config; if it is set to
    something we cannot honor we warn rather than silently swallowing it, so
    the operator isn't left believing a cap is in effect when it isn't.
    """
    raw = os.environ.get("EGG_TOOL_OUTPUT_CAP_BYTES")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        # ``raw`` is a non-empty str here, so int() can only raise ValueError.
        logger.warning(
            "Ignoring invalid EGG_TOOL_OUTPUT_CAP_BYTES=%r (not an integer); "
            "falling back to the %d-byte default cap.",
            raw,
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            "Ignoring non-positive EGG_TOOL_OUTPUT_CAP_BYTES=%r; "
            "falling back to the %d-byte default cap.",
            raw,
            default,
        )
        return default
    return value


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncation_marker(
    text: str,
    *,
    cap_bytes: int,
    tool: str | None,
    narrow_hint: str | None,
    indent: int | None = None,
) -> dict[str, Any]:
    """Build a structured marker dict that fits under ``cap_bytes`` serialized.

    The preview is the head of ``text`` shrunk (by bytes, re-checking the
    serialized size) until the whole marker fits — robust to JSON escape
    expansion of the preview. ``indent`` must match the indent the caller
    will re-serialize the marker with (the orchestrator ships ``indent=2``),
    so the fit check measures the real on-wire size.
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

    def _serialized_len() -> int:
        return _utf8_len(json.dumps(marker, default=str, indent=indent))

    # Shrink the preview until the serialized marker fits. JSON-escaping (and
    # any indent the caller re-serializes with) can expand the preview past
    # the byte budget, so check the real size.
    while _serialized_len() > cap_bytes and preview:
        preview = preview[: len(preview) // 2]
        marker["preview"] = preview
    # Pathological tiny cap: even an empty preview leaves the fixed fields
    # over the cap. Drop the preview entirely so the marker is as small as it
    # can be — still sub-KB, so it can never threaten the 1 MB SDK buffer the
    # cap exists to protect.
    if _serialized_len() > cap_bytes:
        marker.pop("preview", None)
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
    indent: int | None = None,
) -> dict[str, Any]:
    """Cap a result *dict* by its serialized size.

    Returns ``result`` unchanged when within the cap, otherwise the
    truncation marker dict (a small, well-formed replacement). Used at the
    orchestrator MCP chokepoint, where the result is re-serialized by the
    server; the marker dict keeps that serialization small.

    Pass the same ``indent`` the caller serializes with so the measurement
    matches the real on-wire size — the orchestrator ships ``indent=2``,
    which roughly doubles a nested payload's size versus compact JSON.
    """
    limit = cap_bytes if cap_bytes is not None else cap_bytes_from_env()
    try:
        text = json.dumps(result, default=str, indent=indent)
    except Exception:
        # Mirror _success_payload: any serialization failure (e.g. a
        # non-str dict key) falls back to str() rather than crashing.
        text = str(result)
    if _utf8_len(text) <= limit:
        return result
    return _truncation_marker(
        text, cap_bytes=limit, tool=tool, narrow_hint=narrow_hint, indent=indent
    )


def _prune_old_spills(target_dir: str) -> None:
    """Best-effort removal of stale ``egg-tool-out-*`` spill files.

    Bounds accumulation over a long session. Files newer than the TTL are
    kept (the agent may still be reading them). Never raises — pruning is a
    courtesy, not a correctness requirement.
    """
    cutoff = time.time() - _SPILL_TTL_SECONDS
    try:
        entries = os.listdir(target_dir)
    except OSError:
        return
    for name in entries:
        if not (name.startswith("egg-tool-out-") and name.endswith(".txt")):
            continue
        candidate = os.path.join(target_dir, name)
        try:
            if os.path.getmtime(candidate) < cutoff:
                os.remove(candidate)
        except OSError:
            continue


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

    For ``Read``'s line-based ``offset``/``limit`` to be useful the spilled
    text must have real line breaks, so callers should serialize with
    ``indent=2`` (or spill raw multi-line content) rather than compact JSON.
    The inline ``preview`` is a small head sample bounded by
    ``min(_SPILL_PREVIEW_BYTES, cap)``, so the descriptor stays tiny and
    shrinks proportionally under a pathologically small cap.

    Only appropriate when the caller and the agent share a filesystem
    (in-sandbox tools). On any write failure, returns ``None`` so the
    caller falls back to truncation rather than failing the tool call.
    """
    limit = cap_bytes if cap_bytes is not None else cap_bytes_from_env()
    total_bytes = _utf8_len(text)
    if total_bytes <= limit:
        return None

    target_dir = spill_dir or tempfile.gettempdir()
    _prune_old_spills(target_dir)
    path = os.path.join(target_dir, f"egg-tool-out-{tool}-{uuid.uuid4().hex}.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None

    preview = "\n".join(text.splitlines()[:preview_lines])
    # Bound the inline preview to a small fixed budget (a single huge line can
    # otherwise blow it). Also clamp to the cap itself: under a pathologically
    # small cap, a fixed 4 KB preview would dominate the descriptor so the
    # caller's outer cap_text re-truncates it and drops output_path — the one
    # field that makes the spill useful. Scaling the preview with the cap keeps
    # the descriptor proportional. The full content is on disk; this is a sample.
    preview_budget = min(_SPILL_PREVIEW_BYTES, limit)
    if _utf8_len(preview) > preview_budget:
        preview = preview.encode("utf-8")[:preview_budget].decode("utf-8", errors="ignore")
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
            "tool (use `offset`/`limit`) or `grep` it via `Bash`. A head sample "
            "of the output is inlined below as `preview`."
        ),
        "preview": preview,
    }
