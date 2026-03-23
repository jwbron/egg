# Standardized Logging Interface

The `egg_logging` library provides structured JSON logging, tool wrappers, and model output capture across all egg components.

## Core Principles

1. **Structured by Default**: All logs are JSON with consistent fields
2. **Context Propagation**: Correlation IDs flow through related operations
3. **Tool Transparency**: Wrappers log all critical tool usage
4. **Human Readable**: Development mode with formatted console output
5. **GCP Native**: Direct compatibility with Cloud Logging
6. **OpenTelemetry Aligned**: Compatible with GenAI observability standards

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              egg_logging Library                              │
│                                                                               │
│  ┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐  │
│  │    JibLogger        │  │   ToolWrappers      │  │  ModelCapture       │  │
│  │                     │  │                     │  │                     │  │
│  │  - Structured JSON  │  │  - bd wrapper       │  │  - Claude output    │  │
│  │  - Severity levels  │  │  - claude wrapper   │  │  - Token usage      │  │
│  │  - Context fields   │  │                     │  │  - Response time    │  │
│  │  - GCP format       │  │                     │  │  - Error capture    │  │
│  └─────────────────────┘  └─────────────────────┘  └─────────────────────┘  │
│                                      │                                        │
│                                      ▼                                        │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         Output Handlers                                │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │   │
│  │  │   Console    │  │    File      │  │    GCP Cloud Logging     │   │   │
│  │  │  (dev mode)  │  │  (local)     │  │    (production)          │   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Library Location

```
shared/
└── egg_logging/
    ├── __init__.py           # Public API
    ├── logger.py             # JibLogger class
    ├── formatters.py         # JSON and console formatters
    ├── context.py            # Context management
    ├── wrappers/
    │   ├── __init__.py
    │   └── claude.py         # Claude Code wrapper
    └── model_capture.py      # Model output capture
```

## Structured Log Format

All log entries include these fields:

```json
{
  "timestamp": "2025-11-28T12:34:56.789Z",
  "severity": "INFO",
  "message": "Human-readable message",
  "service": "slack-receiver",
  "component": "message_handler",
  "environment": "container",
  "traceId": "0af7651916cd43dd8448eb211c80319c",
  "spanId": "b7ad6b7169203331",
  "traceFlags": "01",
  "context": {
    "task_id": "bd-xyz789",
    "repository": "jwbron/egg",
    "pr_number": 123
  }
}
```

### GCP Cloud Logging Compatibility

| egg_logging Field | GCP Field | Purpose |
|-------------------|-----------|----------|
| `severity` | `severity` | Log level |
| `message` | `message` | Human-readable text |
| `timestamp` | `timestamp` | ISO 8601 format |
| `traceId` | `logging.googleapis.com/trace` | Distributed trace ID |
| `spanId` | `logging.googleapis.com/spanId` | Span within trace |
| `labels` | `logging.googleapis.com/labels` | Filterable metadata |
| `gen_ai.*` | `jsonPayload.gen_ai.*` | OpenTelemetry GenAI attributes |

## OpenTelemetry GenAI Alignment

For LLM operations, logs include standardized GenAI attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| `gen_ai.system` | string | LLM provider ("anthropic") |
| `gen_ai.request.model` | string | Model identifier |
| `gen_ai.usage.input_tokens` | int | Prompt token count |
| `gen_ai.usage.output_tokens` | int | Completion token count |
| `gen_ai.response.finish_reasons` | string[] | Why generation stopped |

## Key Features

### BoundLogger Pattern

```python
from egg_logging import get_logger

logger = get_logger("task-processor")
bound = logger.with_context(task_id="bd-abc123", repository="owner/repo")
bound.info("Starting task processing")    # Includes task_id, repository
```

### Tool Wrappers

Wrappers intercept calls to critical commands and log invocation, stdout/stderr, timing, and exit codes.

| Tool | What to Capture |
|------|------------------|
| `bd` | Command, task_id, status changes |
| `claude` | Prompt (summary), response, tokens, timing |

**Note:** `git` and `gh` wrappers were originally planned but removed in January 2026. The gateway sidecar provides purpose-built `git_client.py` and `github_client.py` modules with security-specific validation that generic logging wrappers couldn't provide.

CLI wrapper binaries are available in `shared/egg_logging/bin/` as drop-in replacements.

### Agent SDK Structured Events

The `egg_agent` client (`shared/egg_agent/client.py`) emits structured log events for every tool call and result during an agent run. These are emitted at INFO level with an `event_type` field:

| `event_type` | When emitted | Key fields |
|---|---|---|
| `tool_use` | Agent invokes a tool | `tool_name`, `tool_use_id`, `input` |
| `tool_result` | Tool returns a result | `tool_use_id`, `is_error`, `content` |
| `assistant` | Agent emits a text block | `event_subtype: "text"`, `text` |

Tool input, output, and assistant text content is truncated to 2000 characters in log events to avoid log bloat; a `... (N chars)` suffix is appended when truncation occurs.

### Model Output Capture

Full Claude Code model output is captured for debugging, cost tracking, and quality analysis. Responses are stored in daily directories with a `index.jsonl` for fast searches:

```
/var/log/egg/model_output/
├── 2025-11-28/
│   ├── 143056_abc123.json
│   └── index.jsonl
```

## Adoption Status

Services using standardized logging:
- slack-receiver, slack-notifier, context-sync
- incoming-processor, github-processor
- gateway token refresher

GCP Cloud Logging integration is deferred to the GCP migration.

## Related Documentation

- [Architecture Overview](README.md) — System design
- [Network Isolation](network-isolation.md) — Tool wrappers complement gateway audit logging
