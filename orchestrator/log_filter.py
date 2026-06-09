"""Server-side filtering for the ``get_service_logs`` tool (#3032).

The gateway/orchestrator pods emit one structured JSON log object per line
(see ``shared/egg_logging/formatters.py``): severity lives in the ``severity``
field (GCP strings ``DEBUG``/``INFO``/``WARNING``/``ERROR``/``CRITICAL``) and
the pipeline id is carried in one of three places depending on how the call
site spelled it. The ``JsonFormatter`` only allowlists ``task_id`` /
``repository`` / ``pr_number`` into the nested ``context`` block; any other
kwarg — including ``pipeline_id``, which is the spelling 25+ orchestrator
call sites actually use — lands in the ``extra`` block instead. The
pipeline-id filter therefore checks ``context.task_id`` **and**
``extra.pipeline_id`` **and** ``extra.task_id`` (in that order); matching
any one of them keeps the line.

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


def _extract_pipeline_id(obj: dict | None) -> str:
    """Pull a pipeline/task id from a parsed log object.

    Checks the three places the JSON formatter can land an id (the formatter's
    context allowlist only includes ``task_id``; everything else falls into
    ``extra``). Returns ``""`` when none of them are set so the caller can
    treat ``""`` as "no determinable id".
    """
    if obj is None:
        return ""
    ctx = obj.get("context")
    if isinstance(ctx, dict):
        task_id = ctx.get("task_id")
        if task_id:
            return str(task_id)
    extra = obj.get("extra")
    if isinstance(extra, dict):
        # Production call sites use ``pipeline_id=...``; ``task_id`` is the
        # defensive companion for any caller that picked the other spelling.
        for key in ("pipeline_id", "task_id"):
            value = extra.get(key)
            if value:
                return str(value)
    return ""


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

    * ``pipeline_id`` — keep lines whose pipeline/task id matches. A line's id
      is read from ``context.task_id``, ``extra.pipeline_id`` or
      ``extra.task_id`` (the three places the JSON formatter can land it); a
      line with none of them set fails this filter (dropped).
    * ``min_level`` — keep lines whose ``severity`` rank is ``>=`` the floor. A
      line with no determinable severity fails this filter (dropped). An
      unrecognised ``min_level`` raises ``ValueError`` — silently dropping the
      filter on a deliberately-set parameter is the footgun this guard avoids;
      callers should still validate up front to surface a useful error.
    * ``pattern`` — keep lines the compiled regex finds (``re.search``) anywhere
      in the raw line text.

    When at least one filter is active, only the last ``limit`` *matching* lines
    are returned (the most recent), preserving order. With no active filter the
    input is returned unchanged aside from the ``limit`` tail. ``limit <= 0``
    returns the empty string (Python's ``lines[-0:]`` gotcha would otherwise
    return everything).
    """
    if min_level is not None:
        min_rank = severity_rank(min_level)
        if min_rank is None:
            raise ValueError(f"min_level must be one of {known_severities()}; got {min_level!r}")
    else:
        min_rank = None
    have_filter = bool(pipeline_id) or min_rank is not None or pattern is not None

    def _tail(items: list[str]) -> list[str]:
        if limit is None:
            return items
        if limit <= 0:
            return []
        if len(items) > limit:
            return items[-limit:]
        return items

    lines = raw.splitlines()
    if not have_filter:
        return "\n".join(_tail(lines))

    kept: list[str] = []
    for line in lines:
        if pattern is not None and not pattern.search(line):
            continue
        if pipeline_id or min_rank is not None:
            obj = _parse_json_line(line)
            if pipeline_id and _extract_pipeline_id(obj) != pipeline_id:
                continue
            if min_rank is not None:
                rank = severity_rank(obj.get("severity") if obj else None)
                if rank is None or rank < min_rank:
                    continue
        kept.append(line)

    return "\n".join(_tail(kept))
