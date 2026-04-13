# egg_logging

Structured logging library for egg components. Provides JSON output for production/GCP Cloud Logging and grep-friendly console output for development and Docker environments.

## Quick Start

```python
from egg_logging import get_logger

logger = get_logger("orchestrator")
logger.info("Processing request", pipeline_id="issue-1702", agent_role="coder")
```

**Console output** (development/Docker):
```
2026-04-13 19:17:39 [INFO    ] orchestrator: Processing request  pipeline_id=issue-1702 agent_role=coder [/app/main.py:42]
```

**JSON output** (production/GCP):
```json
{"timestamp": "2026-04-13T19:17:39.123Z", "severity": "INFO", "message": "Processing request", "service": "orchestrator", "extra": {"pipeline_id": "issue-1702", "agent_role": "coder"}}
```

## Features

- **Structured JSON** for GCP Cloud Logging compatibility
- **Inline console format**: all structured context fields render as `key=value` pairs on the same log line, making logs fully grep-friendly in Docker environments
- **Context propagation**: trace IDs and context fields flow through `ContextScope` and `BoundLogger`
- **OpenTelemetry aligned**: GenAI attributes for LLM observability
- **Automatic environment detection**: GCP, container, or host

## Console Format

The `ConsoleFormatter` renders all structured fields **inline** on the same log line. This is critical for Docker log filtering where each line is a separate log entry.

### Format

```
TIMESTAMP [LEVEL   ] SERVICE: MESSAGE  key1=value1 key2=value2 [FILE:LINE]
```

### Filtering with grep

```bash
# Filter by pipeline
docker logs egg-orchestrator | grep pipeline_id=issue-1702

# Filter by agent role within a pipeline
docker logs egg-orchestrator | grep pipeline_id=issue-1702 | grep agent_role=coder

# Filter by phase
docker logs egg-orchestrator | grep phase=implement
```

### Value formatting

| Condition | Behavior | Example |
|-----------|----------|---------|
| Simple value | `key=value` | `pipeline_id=issue-1702` |
| Value with spaces | `key="value with spaces"` | `description="my task"` |
| Value > 80 chars | Truncated with `...` | `long_field=abcdef...` |
| `None` value | `key=` | `optional_field=` |

Exception tracebacks remain multi-line (separate from inline fields).

### Configuration

```python
from egg_logging import ConsoleFormatter

formatter = ConsoleFormatter(
    service="orchestrator",
    use_colors=True,          # ANSI colors (auto-detected; respects NO_COLOR)
    show_context=True,        # Show context fields (task_id, repository, pr_number)
    show_source_location=True,  # Show [file:line] at end
    show_extra=True,          # Show extra keyword arguments inline
)
```

## Context Propagation

### Keyword arguments (per-call)

```python
logger.info("Pipeline started", pipeline_id="issue-1702", phase="implement")
```

### Bound logger (per-instance)

```python
bound = logger.with_context(pipeline_id="issue-1702", agent_role="coder")
bound.info("Starting task")    # Includes pipeline_id and agent_role
bound.info("Task completed")   # Same context on every call
```

### Context scope (block-scoped)

```python
from egg_logging import ContextScope

with ContextScope(task_id="task-123", repository="owner/repo"):
    logger.info("Inside scope")   # Includes task_id and repository
    logger.info("Still in scope")
```

## JSON Formatter

The `JsonFormatter` produces structured JSON compatible with GCP Cloud Logging. Used automatically in GCP environments and for file handlers.

```python
from egg_logging import JsonFormatter

formatter = JsonFormatter(
    service="orchestrator",
    component="pipeline_engine",
    environment="container",
    include_extra=True,
)
```

### GCP Cloud Logging field mapping

| egg_logging Field | GCP Field | Purpose |
|-------------------|-----------|---------|
| `severity` | `severity` | Log level |
| `message` | `message` | Human-readable text |
| `timestamp` | `timestamp` | ISO 8601 format |
| `traceId` | `logging.googleapis.com/trace` | Distributed trace ID |
| `spanId` | `logging.googleapis.com/spanId` | Span within trace |

## API Reference

### `get_logger(name, level=INFO, component=None)`

Primary entry point. Returns a cached `EggLogger` instance.

### `EggLogger`

- `info(msg, **kwargs)` / `debug()` / `warning()` / `error()` / `critical()` / `exception()`
- `with_context(**kwargs)` -> `BoundLogger`
- `add_file_handler(log_file, level, max_bytes, backup_count)`

### `BoundLogger`

Same log methods as `EggLogger`, with bound context fields included in every call.

### `configure_root_logging(level=WARNING, json_format=False)`

Configures the root logger for third-party libraries.

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | Public API exports |
| `logger.py` | `EggLogger`, `BoundLogger`, `get_logger()`, `configure_root_logging()` |
| `formatters.py` | `JsonFormatter` (GCP-compatible JSON), `ConsoleFormatter` (inline key=value) |
| `context.py` | `LogContext`, `ContextScope`, context propagation |
| `signatures.py` | Function signature capture for debug logging |
| `cli.py` | CLI logging utilities |

## Related Documentation

- [Logging Architecture](../../docs/architecture/logging.md) -- design decisions and full format specification
- [Shared Libraries](../README.md) -- all shared packages
