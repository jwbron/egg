#!/usr/bin/env python3
"""PostToolUse hook: bound tool result size to prevent SDK buffer overflows.

Issue #2804. The Claude Agent SDK message reader has a hard-coded JSON
buffer cap (1 MB default; we raise it via ``max_buffer_size`` but it is
still finite). Tool results that exceed the buffer kill the agent
process with exit code 255, which the consensus-wrapper then wastes its
restart budget retrying — a deterministic failure that no retry can
recover.

This hook runs inside the Claude Code CLI subprocess after each tool
call. It checks the serialized size of the tool response and, when it
exceeds ``EGG_TOOL_RESULT_CAP_BYTES`` (default 200 KB), returns a
``decision: block`` response that suppresses the oversized payload and
feeds the agent a structured retry prompt instead. The agent sees
truncation guidance and re-issues the tool call with a narrower scope.

Why CLI-side rather than programmatic SDK hooks: SDK hook callbacks
travel back to the Python process through the same buffered JSON stream
that has the cap. An oversized ``tool_response`` would crash the buffer
*before* the callback could see it. A CLI hook subprocess runs entirely
inside the CLI's process boundary and gets the tool response without
touching the SDK channel.

Edit / Write / MultiEdit are handled with a specific reason — the file
mutation already happened, so the agent must NOT retry the edit; it
just needs to know the response snippet was suppressed.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

DEFAULT_CAP_BYTES = 200_000


def _cap_bytes() -> int:
    raw = os.environ.get("EGG_TOOL_RESULT_CAP_BYTES", "").strip()
    if not raw:
        return DEFAULT_CAP_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CAP_BYTES
    return value if value > 0 else DEFAULT_CAP_BYTES


def _serialized_size(value: Any) -> int:
    """Return the JSON-serialized byte length of ``value``.

    Matches what the SDK reader sees on the wire. Falls back to ``str()``
    for objects that aren't JSON-serializable.
    """
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except TypeError, ValueError:
        return len(str(value).encode("utf-8"))


# Tools where the model should NOT retry the call — the side effect
# already happened on disk. The reason text tells the agent the
# mutation succeeded and the snippet was suppressed.
_MUTATING_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Tool-specific guidance for tools that are safe to retry with a
# narrower scope.
_RETRY_GUIDANCE = {
    "Read": (
        "Re-issue Read with the `offset` and `limit` parameters scoped to the "
        "specific lines you need. Default limit is 2000 lines."
    ),
    "Bash": (
        "Re-issue the command with narrower scope: pipe through `head`, "
        "`tail`, `wc -l`, or filter with `grep`. For long-running output, "
        "redirect to a file and Read targeted chunks."
    ),
    "Grep": (
        "Re-issue Grep with a more specific pattern, a smaller `path`, "
        "`--head-limit`, or output_mode='count' first to gauge result size."
    ),
    "Glob": ("Re-issue Glob with a tighter pattern or a smaller `path` scope."),
}

_FALLBACK_RETRY_GUIDANCE = (
    "Re-issue the call with narrower input (smaller path, lower limit, more specific pattern)."
)


def _build_reason(tool_name: str, size: int, cap: int) -> str:
    base = (
        f"Tool result for `{tool_name}` was {size} bytes, exceeding the "
        f"{cap}-byte cap (issue #2804). The payload was suppressed to "
        "prevent the Claude Agent SDK buffer overflow that would otherwise "
        "kill the agent process."
    )
    if tool_name in _MUTATING_TOOLS:
        return (
            f"{base} The file mutation itself succeeded — do NOT retry the "
            "tool call. If you need to verify the result, use Read with "
            "narrow `offset`/`limit` parameters."
        )
    guidance = _RETRY_GUIDANCE.get(tool_name, _FALLBACK_RETRY_GUIDANCE)
    return f"{base} {guidance}"


def evaluate(event: dict[str, Any], cap: int) -> dict[str, Any] | None:
    """Inspect a PostToolUse event and return the hook output, or None to allow.

    Pure function exposed for testing — the CLI entrypoint wires it to
    stdin/stdout.
    """
    tool_response = event.get("tool_response")
    if tool_response is None:
        return None
    size = _serialized_size(tool_response)
    if size <= cap:
        return None
    tool_name = str(event.get("tool_name") or "unknown")
    return {
        "decision": "block",
        "reason": _build_reason(tool_name, size, cap),
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw.strip():
        return 0
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        # Unparseable input — allow the tool result through; the buffer
        # bump and clean-error path in shared/egg_agent/client.py will
        # catch any oversize payload as a structured failure.
        return 0
    if not isinstance(event, dict):
        return 0
    output = evaluate(event, _cap_bytes())
    if output is not None:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
