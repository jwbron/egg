"""Server-side filtering for the ``get_service_logs`` tool (#3032).

The gateway/orchestrator pods emit one structured JSON log object per line
(see ``shared/egg_logging/formatters.py``): severity lives in the ``severity``
field (GCP strings ``DEBUG``/``INFO``/``WARNING``/``ERROR``/``CRITICAL``) and
the pipeline id is nested at ``context.task_id`` — there is no top-level
``pipeline_id`` field.

``filter_log_lines`` lets an operator scope a noisy multi-pipeline pod tail to
the lines they actually want ("WARNING+ for pipeline X in the last 5 min")
*before* the response is truncated to the MCP token cap, instead of fetching a
raw tail that is mostly health-check noise and watching the one line they need
scroll out of the window they can afford to fetch.
"""

from __future__ import annotations

import json
import re

# Python ``logging`` numeric levels keyed by the GCP severity strings the JSON
# formatter emits. Backs the ``level`` (minimum-severity) filter.
_SEVERITY_RANK: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def severity_rank(name: str | None) -> int | None:
    """Return the numeric rank for a GCP severity string, or ``None`` if unknown.

    Case-insensitive. ``None`` doubles as the "not a recognised severity"
    signal callers use to reject an invalid ``level`` param.
    """
    if not name:
        return None
    return _SEVERITY_RANK.get(name.strip().upper())


def known_severities() -> list[str]:
    """Return the recognised severity names (for error messages / schema enums)."""
    return sorted(_SEVERITY_RANK, key=lambda s: _SEVERITY_RANK[s])


def _parse_json_line(line: str) -> dict | None:
    """Parse a single log line as a JSON object, or ``None`` if it isn't one."""
    stripped = line.strip()
    if not stripped or stripped[0] != "{":
        return None
    try:
        obj = json.loads(stripped)
    except ValueError, TypeError:
        return None
    return obj if isinstance(obj, dict) else None


def filter_log_lines(
    raw: str,
    *,
    pipeline_id: str | None = None,
    min_level: str | None = None,
    pattern: re.Pattern[str] | None = None,
    limit: int | None = None,
) -> str:
    """Filter a pod's raw log tail to the lines an operator asked for.

    Each line is parsed as a structured JSON log object. Filters are ANDed:

    * ``pipeline_id`` — keep lines whose ``context.task_id`` equals it. A line
      with no determinable task id fails this filter (dropped).
    * ``min_level`` — keep lines whose ``severity`` rank is ``>=`` the floor. A
      line with no determinable severity fails this filter (dropped). An
      unrecognised ``min_level`` is treated as no severity filter; callers
      should validate it up front.
    * ``pattern`` — keep lines the compiled regex finds (``re.search``) anywhere
      in the raw line text.

    When at least one filter is active, only the last ``limit`` *matching* lines
    are returned (the most recent), preserving order. With no active filter the
    input is returned unchanged aside from the ``limit`` tail.
    """
    min_rank = severity_rank(min_level)
    have_filter = bool(pipeline_id) or min_rank is not None or pattern is not None

    lines = raw.splitlines()
    if not have_filter:
        if limit is not None and len(lines) > limit:
            lines = lines[-limit:]
        return "\n".join(lines)

    kept: list[str] = []
    for line in lines:
        if pattern is not None and not pattern.search(line):
            continue
        if pipeline_id or min_rank is not None:
            obj = _parse_json_line(line)
            if pipeline_id:
                task_id = ""
                if obj is not None:
                    ctx = obj.get("context")
                    if isinstance(ctx, dict):
                        task_id = str(ctx.get("task_id") or "")
                if task_id != pipeline_id:
                    continue
            if min_rank is not None:
                rank = severity_rank(obj.get("severity") if obj else None)
                if rank is None or rank < min_rank:
                    continue
        kept.append(line)

    if limit is not None and len(kept) > limit:
        kept = kept[-limit:]
    return "\n".join(kept)
