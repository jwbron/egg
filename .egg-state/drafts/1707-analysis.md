## Task Analysis

**Problem statement**: Structured context fields (`pipeline_id`, `agent_role`, `phase`, etc.) passed as kwargs to log calls are invisible when filtering logs with `grep` or `docker logs` because they render on separate lines from the main message.

**Source context**: Issue #1707, filed after debugging #1706 — tracing a PR-phase push failure required broad timestamp-based grepping instead of `docker logs egg-orchestrator | grep pipeline_id=issue-1702`.

**System context**: The `egg_logging` package (`shared/egg_logging/`) provides `EggLogger` and `ConsoleFormatter` used by both orchestrator and gateway containers. When `logger.info("message", pipeline_id=pid)` is called, `EggLogger._log()` collects kwargs into an `extra` dict and passes it to Python's `logging.Logger.log(extra=...)`, which injects the fields into the `LogRecord.__dict__`. The `ConsoleFormatter.format()` method then extracts these via `_extract_extra()`.

**Technical root cause**: In `ConsoleFormatter.format()` (`shared/egg_logging/formatters.py:307-321`), extra fields are rendered on **new lines** below the main message:
```
2026-04-13 19:17:39 [INFO] orchestrator.pipelines: message [/app/file.py:42]
  pipeline_id=issue-1702
  total_phases=2
```

Docker's json-file log driver stores each line as a separate log entry. This means `grep pipeline_id=issue-1702` returns disconnected `  pipeline_id=...` lines with no timestamp or message, and `grep _rewrite_brc_history_for_pr` returns the message line without the associated extras. The fields are effectively "lost" for any line-oriented filtering.

Additionally, the hardcoded `show_context` section (lines 278-293) only renders `task_id`, `repository`, `pr_number` in parentheses — the key fields the issue wants (`pipeline_id`, `phase`, `agent_role`, `issue_number`, `container_id`) are not included there and end up only in the multi-line extras.

**Files affected**:
- `shared/egg_logging/formatters.py` — Change `ConsoleFormatter.format()` to render all extra fields inline as `key=value` pairs on the same line
- `tests/shared/egg_logging/test_formatters.py` — Update tests for new inline format

**Risks / edge cases**: Very long values (lists, dicts, multi-line strings) could produce excessively long log lines if rendered inline — need to truncate or abbreviate. Exception tracebacks must still be multi-line (they already are, separate from extras). The ConsoleFormatter's show_extra and show_context flags must continue to work.