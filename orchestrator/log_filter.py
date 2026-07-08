"""Server-side filtering for the ``get_service_logs`` tool (#3032, #3547).

The gateway/orchestrator pods emit one log record per *event*, in one of the
two formats ``shared/egg_logging/formatters.py`` can produce:

* **JSON** (``JsonFormatter``, only when the environment detects as GCP):
  one JSON object per line; severity in the ``severity`` field and the
  pipeline id in ``context.task_id`` / ``extra.pipeline_id`` /
  ``extra.task_id`` (the formatter's context allowlist only includes
  ``task_id``; every other kwarg; including ``pipeline_id``, the spelling
  25+ orchestrator call sites use; lands in ``extra``).
* **Console** (``ConsoleFormatter``, everywhere else; including the k8s
  pods this endpoint actually tails, which detect as ``container``):
  ``YYYY-MM-DD HH:MM:SS [LEVEL   ] service: message key=value ...`` with
  structured kwargs rendered inline as ``key=value`` pairs, and exception
  tracebacks appended as real newlines below the record line.

Filtering therefore works on **logical records, not physical lines**: a new
record starts at a line that looks like a record head (JSON object or the
console timestamp prefix); anything else; traceback frames, multi-line
payloads; is a continuation attached to the preceding record. A ``pattern``
that matches an exception message returns the whole traceback with it
instead of one orphaned frame (#3547).

``filter_log_lines`` lets an operator scope a noisy multi-pipeline pod tail
to the records they actually want ("WARNING+ for pipeline X in the last
5 min") *before* the response is truncated to the MCP token cap, instead of
fetching a raw tail that is mostly health-check noise and watching the one
line they need scroll out of the window they can afford to fetch.
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

# Head of a ConsoleFormatter record: ``YYYY-MM-DD HH:MM:SS [LEVEL``. The level
# group feeds the min_level filter; padding inside the brackets is optional so
# both the padded (``[INFO    ]``) and unpadded forms match. Head/field matching
# runs on an ANSI-stripped copy (see ``_strip_ansi``), so the bare form here also
# covers the colorized ``[\x1b[32mINFO    \x1b[0m]`` the formatter emits on a TTY.
_CONSOLE_HEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[([A-Za-z]+)\s*\]")

# Inline ``pipeline_id=...`` / ``task_id=...`` pair as ConsoleFormatter renders
# it. Ids are slugs (``issue-3523``), never quoted, so a bare token capture is
# exact; the lookbehind stops ``sub_task_id=`` from matching as ``task_id=``.
_CONSOLE_ID_RE = re.compile(r"(?<![\w.-])(?:pipeline_id|task_id)=([\w][\w./-]*)")

# ANSI SGR escape (``\x1b[..m``). The ConsoleFormatter wraps the level, inline
# kwargs, and source location in these when ``use_colors=True``. Production k8s
# pods are non-TTY so colors are off, but stripping them before head detection /
# field extraction keeps the filter working if a colorized capture is ever fed
# in — otherwise the anchored head regex misses every colorized line and the
# ``m`` closing each escape defeats the id lookbehind (#3566 review).
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI SGR escapes so head/field regexes see the plain record text."""
    return _ANSI_SGR_RE.sub("", text)


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


def _extract_pipeline_id(obj: dict | None, head: str) -> str:
    """Pull a pipeline/task id from a record's parsed head or its raw text.

    JSON records: checks the three places the JSON formatter can land an id
    (the formatter's context allowlist only includes ``task_id``; everything
    else falls into ``extra``). Console records (``obj is None``): matches the
    inline ``pipeline_id=`` / ``task_id=`` pair the ConsoleFormatter renders
    (#3547; the k8s pods emit console format, so the structured lookup alone
    dropped every production line). Returns ``""`` when no id is determinable.
    """
    if obj is not None:
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
    # Structured kwargs are appended at the END of a console line, so the
    # record's own id is the LAST ``pipeline_id=``/``task_id=`` token. A
    # leftmost match (``re.search``) could otherwise pick up an id embedded in
    # the message body — e.g. a logged URL/command containing ``?pipeline_id=``
    # — and silently surface or hide the wrong records (#3566 review).
    matches = _CONSOLE_ID_RE.findall(_strip_ansi(head))
    return matches[-1] if matches else ""


def _extract_severity(obj: dict | None, head: str) -> str | None:
    """Pull the severity string from a record's parsed head or its raw text."""
    if obj is not None:
        severity = obj.get("severity")
        return severity if isinstance(severity, str) else None
    match = _CONSOLE_HEAD_RE.match(_strip_ansi(head))
    return match.group(1) if match else None


def _group_records(lines: list[str]) -> list[list[str]]:
    """Group physical lines into logical records.

    A record starts at a JSON object line or a console-format head
    (timestamp + level). Every other line; traceback frames, wrapped
    payloads; is a continuation of the preceding record. A leading run of
    continuation lines (tail cut mid-record) forms its own headless record so
    no input is silently dropped.
    """
    records: list[list[str]] = []
    for line in lines:
        stripped = _strip_ansi(line.strip())
        is_head = bool(stripped) and (
            stripped[0] == "{" or _CONSOLE_HEAD_RE.match(stripped) is not None
        )
        if is_head or not records:
            records.append([line])
        else:
            records[-1].append(line)
    return records


def filter_log_lines(
    raw: str,
    *,
    pipeline_id: str | None = None,
    min_level: str | None = None,
    pattern: re.Pattern[str] | None = None,
    limit: int | None = None,
) -> str:
    """Filter a pod's raw log tail to the records an operator asked for.

    Physical lines are grouped into logical records first (a traceback stays
    attached to the log line that raised it; see :func:`_group_records`), and
    filters are ANDed per record:

    * ``pipeline_id``; keep records whose pipeline/task id matches. For a
      JSON record the id is read from ``context.task_id``,
      ``extra.pipeline_id`` or ``extra.task_id``; for a console record it is
      matched from the inline ``pipeline_id=`` / ``task_id=`` pair. A record
      with no determinable id fails this filter (dropped).
    * ``min_level``; keep records whose severity rank is ``>=`` the floor
      (JSON ``severity`` field, or the console ``[LEVEL]`` bracket). A record
      with no determinable severity fails this filter (dropped). An
      unrecognised ``min_level`` raises ``ValueError`` — silently dropping the
      filter on a deliberately-set parameter is the footgun this guard avoids;
      callers should still validate up front to surface a useful error.
    * ``pattern``; keep records the compiled regex finds (``re.search``)
      anywhere in the record's raw text, continuation lines included, so a
      match inside a traceback returns the whole traceback.

    When at least one filter is active, only the last ``limit`` *matching*
    records are returned (the most recent), preserving order. With no active
    filter the input is returned unchanged aside from the ``limit`` tail
    (counted in physical lines, matching the raw-tail semantics callers
    expect). ``limit <= 0`` returns the empty string (Python's ``lines[-0:]``
    gotcha would otherwise return everything).
    """
    if min_level is not None:
        min_rank = severity_rank(min_level)
        if min_rank is None:
            raise ValueError(f"min_level must be one of {known_severities()}; got {min_level!r}")
    else:
        min_rank = None
    have_filter = bool(pipeline_id) or min_rank is not None or pattern is not None

    def _tail(items: list) -> list:
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
    for record in _group_records(lines):
        head = record[0]
        text = "\n".join(record)
        if pattern is not None and not pattern.search(text):
            continue
        if pipeline_id or min_rank is not None:
            obj = _parse_json_line(head)
            if pipeline_id and _extract_pipeline_id(obj, head) != pipeline_id:
                continue
            if min_rank is not None:
                rank = severity_rank(_extract_severity(obj, head))
                if rank is None or rank < min_rank:
                    continue
        kept.append(text)

    return "\n".join(_tail(kept))
