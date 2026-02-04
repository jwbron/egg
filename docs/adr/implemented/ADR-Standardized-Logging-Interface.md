# ADR: Standardized Logging Interface

**Driver:** James Wiesebron
**Approver:** TBD
**Contributors:** James Wiesebron, Claude (AI Pair Programming)
**Informed:** Engineering teams
**Proposed:** November 2025
**Status:** Implemented (Phases 1-4 Complete)

## Table of Contents

- [Context](#context)
- [Problem Statement](#problem-statement)
- [Decision](#decision)
- [OpenTelemetry Alignment](#opentelemetry-alignment)
- [High-Level Design](#high-level-design)
- [Structured Log Format](#structured-log-format)
- [Tool Wrappers](#tool-wrappers)
- [Model Output Capture](#model-output-capture)
- [GCP Cloud Logging Integration](#gcp-cloud-logging-integration)
- [Consequences](#consequences)
- [Alternatives Considered](#alternatives-considered)
- [Implementation Plan](#implementation-plan)

## Context

### Background

The egg system consists of multiple components across host and container environments:

| Component Type | Location | Examples |
|----------------|----------|----------|
| Host services | `host-services/` | slack-receiver, slack-notifier, context-sync |
| Container scripts | `sandbox/` | egg CLI, github-processor, incoming-processor |
| Utilities | `host-services/utilities/` | egg-logs, service-failure-notify |

Currently, all components use `print()` statements for output with inconsistent formatting:
- No structured data format
- No severity levels
- No correlation between related operations
- No machine-parseable output for debugging
- No capture of Claude Code model output

### Current State

```python
# Current pattern (before migration)
print(f"  gh command failed: {' '.join(args)}")
print(f"  stderr: {e.stderr}")
print(f"  Invoking egg: {task_type} for {context.get('repository', 'unknown')}")
```

This approach has several limitations:
1. **Debugging**: Hard to filter and search logs
2. **Correlation**: No way to trace related operations across services
3. **Monitoring**: Cannot set up alerts on specific events
4. **GCP Integration**: Not compatible with Cloud Logging structured format
5. **Model Output**: No capture of full Claude Code responses

### Scope

**In Scope:**
- Python logging library for host and container scripts
- Structured JSON log format compatible with GCP Cloud Logging
- Tool wrappers for critical commands (bd, git, gh, claude)
- Model output capture mechanism
- Migration path for existing scripts

**Out of Scope:**
- Shell script logging (bash scripts continue using echo)
- Log aggregation infrastructure (handled by GCP)
- Real-time monitoring dashboards (future ADR)

## Problem Statement

**We need consistent, structured logging across all egg components that supports debugging today and GCP Cloud Logging in production.**

Requirements:

1. **Debugging Support**: Filter logs by service, severity, operation type
2. **Structured Format**: Machine-parseable JSON with consistent fields
3. **Tool Visibility**: Capture all invocations of critical tools (bd, git, gh)
4. **Model Output**: Capture full Claude Code model responses
5. **GCP Ready**: Compatible with Cloud Logging structured log format
6. **Low Friction**: Easy to adopt in existing scripts

## Decision

**Implement a shared `egg_logging` Python library that provides structured JSON logging, tool wrappers, and model output capture.**

### Core Principles

1. **Structured by Default**: All logs are JSON with consistent fields
2. **Context Propagation**: Correlation IDs flow through related operations
3. **Tool Transparency**: Wrappers log all critical tool usage
4. **Human Readable**: Development mode with formatted console output
5. **GCP Native**: Direct compatibility with Cloud Logging
6. **OpenTelemetry Aligned**: Compatible with emerging GenAI observability standards

## OpenTelemetry Alignment

### GenAI Semantic Conventions

The OpenTelemetry community has standardized semantic conventions for GenAI/LLM observability. The `egg_logging` library aligns with these conventions to ensure compatibility with the broader observability ecosystem.

**Key Standards:**
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) - Official spec for LLM telemetry
- [Agent-Specific Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/) - Spans for GenAI agent calls
- [Logs Specification](https://opentelemetry.io/docs/concepts/signals/logs/) - Structured log format requirements

### MELT Framework Integration

The logging interface is designed as part of a unified MELT (Metrics, Events, Logs, Traces) observability approach:

| Signal | Purpose | egg_logging Support |
|--------|---------|---------------------|
| **Metrics** | Quantitative measurements (token counts, latencies) | Extracted from structured logs |
| **Events** | Discrete occurrences (task started, PR created) | Logged with event-specific fields |
| **Logs** | Detailed context and debugging | Primary output of this library |
| **Traces** | Request flow across services | Correlation via traceId/spanId |

### Trace Correlation

All log entries include OpenTelemetry trace context for correlation:

```json
{
  "traceId": "0af7651916cd43dd8448eb211c80319c",
  "spanId": "b7ad6b7169203331",
  "traceFlags": "01",
  ...
}
```

This enables:
1. Linking logs to distributed traces
2. Filtering logs for a specific request/task
3. Integration with trace-aware observability platforms (Langfuse, Phoenix, etc.)

### Configuration

The library respects OpenTelemetry configuration patterns:

```bash
# Enable experimental semantic conventions
export OTEL_SEMCONV_STABILITY_OPT_IN=genai

# Configure trace context propagation
export OTEL_PROPAGATORS=tracecontext,baggage

# Set service identification
export OTEL_SERVICE_NAME=egg-slack-receiver
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production
```

### GenAI-Specific Attributes

For LLM operations, logs include standardized GenAI attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| `gen_ai.system` | string | LLM provider ("anthropic") |
| `gen_ai.request.model` | string | Model identifier |
| `gen_ai.usage.input_tokens` | int | Prompt token count |
| `gen_ai.usage.output_tokens` | int | Completion token count |
| `gen_ai.response.finish_reasons` | string[] | Why generation stopped |

Example log entry for Claude interaction:

```json
{
  "timestamp": "2025-11-28T12:34:56.789Z",
  "severity": "INFO",
  "message": "Claude Code response completed",
  "traceId": "0af7651916cd43dd8448eb211c80319c",
  "spanId": "b7ad6b7169203331",
  "gen_ai.system": "anthropic",
  "gen_ai.request.model": "claude-sonnet-4-5-20250929",
  "gen_ai.usage.input_tokens": 1500,
  "gen_ai.usage.output_tokens": 800,
  "gen_ai.response.finish_reasons": ["end_turn"],
  "context": {
    "task_id": "bd-xyz789",
    "task_type": "pr_fix"
  }
}
```

## High-Level Design

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              egg_logging Library                              │
│                                                                               │
│  ┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐  │
│  │    JibLogger        │  │   ToolWrappers      │  │  ModelCapture       │  │
│  │                     │  │                     │  │                     │  │
│  │  - Structured JSON  │  │  - bd wrapper       │  │  - Claude output    │  │
│  │  - Severity levels  │  │  - git wrapper      │  │  - Token usage      │  │
│  │  - Context fields   │  │  - gh wrapper       │  │  - Response time    │  │
│  │  - GCP format       │  │  - claude wrapper   │  │  - Error capture    │  │
│  │                     │  │                     │  │                     │  │
│  └─────────────────────┘  └─────────────────────┘  └─────────────────────┘  │
│                                      │                                        │
│                                      ▼                                        │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         Output Handlers                                │   │
│  │                                                                        │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │   │
│  │  │   Console    │  │    File      │  │    GCP Cloud Logging     │   │   │
│  │  │  (dev mode)  │  │  (local)     │  │    (production)          │   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Library Location

```
egg/
├── shared/
│   └── egg_logging/
│       ├── __init__.py           # Public API
│       ├── logger.py             # JibLogger class
│       ├── formatters.py         # JSON and console formatters
│       ├── context.py            # Context management
│       ├── wrappers/
│       │   ├── __init__.py
│       │   ├── bd.py             # Beads wrapper
│       │   ├── git.py            # Git wrapper
│       │   ├── gh.py             # GitHub CLI wrapper
│       │   └── claude.py         # Claude Code wrapper
│       └── model_capture.py      # Model output capture
```

Both host services and container scripts can import from this shared location.

### Key Implementation Features

#### BoundLogger Pattern

The `BoundLogger` class allows binding context to a logger instance, reducing boilerplate in long-running operations:

```python
from egg_logging import get_logger

logger = get_logger("task-processor")

# Create a bound logger with persistent context
bound = logger.with_context(task_id="bd-abc123", repository="owner/repo")

# All logs from bound logger include the context automatically
bound.info("Starting task processing")    # Includes task_id, repository
bound.info("Downloaded dependencies")     # Includes task_id, repository
bound.info("Task completed successfully") # Includes task_id, repository

# Can nest bound loggers
step_logger = bound.with_context(step="validation")
step_logger.info("Validating inputs")  # Includes task_id, repository, step
```

**Benefits:**
- **Type-safe:** Context fields are validated at runtime
- **Composable:** Chain multiple `.with_context()` calls
- **Readable:** Clear intent in code structure

#### Thread Safety

All logger instances use thread-safe singleton patterns with double-checked locking:
- Safe for concurrent requests in host services
- No race conditions on initialization
- Minimal locking overhead (only on first call)

#### Source Location Tracking

Every log entry includes exact source location for debugging:

```json
{
  "sourceLocation": {
    "file": "/path/to/service.py",
    "line": 142,
    "function": "process_request"
  }
}
```

This enables "jump to source" in GCP Cloud Logging UI.

## Structured Log Format

### Base Log Entry

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
  },

  "labels": {
    "app": "egg",
    "version": "1.0.0"
  }
}
```

**Note:** The `traceId` and `spanId` fields follow the [W3C Trace Context](https://www.w3.org/TR/trace-context/) specification, enabling correlation with distributed traces across services.

### GCP Cloud Logging Compatibility

The format maps directly to [GCP structured logging](https://cloud.google.com/logging/docs/structured-logging):

| egg_logging Field | GCP Field | Purpose |
|-------------------|-----------|----------|
| `severity` | `severity` | Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `message` | `message` | Human-readable text |
| `timestamp` | `timestamp` | ISO 8601 format |
| `traceId` | `logging.googleapis.com/trace` | Distributed trace ID |
| `spanId` | `logging.googleapis.com/spanId` | Span within trace |
| `labels` | `logging.googleapis.com/labels` | Filterable metadata |
| `context.*` | `jsonPayload.*` | Structured data |
| `gen_ai.*` | `jsonPayload.gen_ai.*` | OpenTelemetry GenAI attributes |

### Severity Levels

| Level | Use Case | Example |
|-------|----------|----------|
| DEBUG | Detailed diagnostic info | "Checking PR #123 for failures" |
| INFO | Normal operations | "PR created successfully" |
| WARNING | Recoverable issues | "Rate limit approaching, backing off" |
| ERROR | Failed operations | "GitHub API returned 500" |
| CRITICAL | System-level failures | "Cannot connect to GitHub" |

## Tool Wrappers

### Purpose

Tool wrappers intercept calls to critical commands and:
1. Log the invocation with full arguments
2. Capture stdout/stderr
3. Record timing and exit codes
4. Propagate correlation context

### Wrapped Tools

| Tool | Why Wrap | What to Capture |
|------|----------|------------------|
| `bd` | Track task state changes | Command, task_id, status changes |
| `claude` | Capture model interactions | Prompt (summary), response, tokens, timing |

**Note:** `git` and `gh` wrappers were originally planned but removed in January 2026.
The gateway sidecar provides purpose-built `git_client.py` and `github_client.py` modules
with security-specific validation (path traversal prevention, argument allowlists,
credential management) that the generic logging wrappers couldn't provide. Services
that need git/gh operations use subprocess directly with `get_logger()` for logging.

### Wrapper Implementation Pattern

#### Programmatic Usage (Python)

```python
# Usage in scripts
from egg_logging.wrappers import bd, claude

# bd wrapper for beads task tracking
result = bd.update("bd-abc123", status="done", notes="Completed task")
# Automatically logs:
# {
#   "severity": "INFO",
#   "message": "bd update completed",
#   "tool": "bd",
#   "command": ["bd", "update", "bd-abc123", "--status", "done", "--notes", "..."],
#   "exit_code": 0,
#   "duration_ms": 45,
#   "context": {"task_id": "bd-abc123"}
# }

# claude wrapper for Claude Code invocations
result = claude.run("--print", "-p", "Explain this code")
```

#### CLI Wrapper Binaries (Shell)

For transparent logging of shell commands (including those run by Claude Code agent), use the provided CLI wrappers:

**Location:** `shared/egg_logging/bin/`

**Available Commands:**
- `egg-bd` - Drop-in replacement for `bd`
- `egg-claude` - Drop-in replacement for `claude`

**Setup Option 1: Shell Aliases**

```bash
# In .bashrc or container setup
alias bd='egg-bd'
alias claude='egg-claude'

# Or source the setup script
source ~/repos/egg/shared/egg_logging/bin/setup-aliases.sh
```

**Setup Option 2: PATH Override**

```bash
# Create symlinks earlier in PATH
mkdir -p ~/.local/bin
ln -sf ~/repos/egg/shared/egg_logging/bin/egg-bd ~/.local/bin/bd
ln -sf ~/repos/egg/shared/egg_logging/bin/egg-claude ~/.local/bin/claude
```

**Setup Option 3: Direct Usage**

```bash
# Use egg-* prefix directly
egg-bd --allow-stale list
egg-claude --print -p "Explain this"
```

**Environment Variables:**
- `EGG_LOGGING_PASSTHROUGH=1` - Skip logging, pass through to real tool
- `EGG_LOGGING_QUIET=1` - Suppress wrapper diagnostic messages

**Benefits:**
- **Zero code changes:** Existing scripts work unchanged
- **Transparent to Claude:** Agent doesn't know it's being logged
- **Full compatibility:** All arguments pass through unchanged
- **Same exit codes:** Wrapper returns same code as underlying tool

### bd (Beads) Wrapper

The beads wrapper captures task lifecycle:

```json
{
  "severity": "INFO",
  "message": "Task status updated",
  "tool": "bd",
  "command": ["bd", "update", "bd-abc123", "--status", "done"],
  "task_id": "bd-abc123",
  "previous_status": "in_progress",
  "new_status": "done",
  "duration_ms": 45
}
```


## Model Output Capture

### Purpose

Capture full Claude Code model output for:
1. Debugging agent behavior
2. Cost tracking (token usage)
3. Performance analysis (response time)
4. Quality analysis (conversation patterns)

### Capture Strategy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Model Output Capture                                │
│                                                                               │
│  ┌─────────────────────┐                                                     │
│  │   Claude Code       │                                                     │
│  │   (claude CLI)      │                                                     │
│  └──────────┬──────────┘                                                     │
│             │                                                                 │
│             ▼                                                                 │
│  ┌─────────────────────┐      ┌─────────────────────────────────────────┐  │
│  │   claude wrapper    │──────►  model_output.jsonl                      │  │
│  │                     │      │  (append-only log file)                  │  │
│  │  - Capture stdout   │      │                                          │  │
│  │  - Parse JSON       │      │  {"timestamp": "...", "prompt": "...",   │  │
│  │  - Extract tokens   │      │   "response": "...", "tokens": {...}}    │  │
│  │  - Measure timing   │      │                                          │  │
│  └─────────────────────┘      └─────────────────────────────────────────┘  │
│                                                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Model Output Log Entry

Using OpenTelemetry GenAI semantic conventions for standardized observability:

```json
{
  "timestamp": "2025-11-28T12:34:56.789Z",
  "severity": "INFO",
  "message": "Claude Code response captured",
  "traceId": "0af7651916cd43dd8448eb211c80319c",
  "spanId": "b7ad6b7169203331",

  "gen_ai.system": "anthropic",
  "gen_ai.request.model": "claude-sonnet-4-5-20250929",
  "gen_ai.usage.input_tokens": 1500,
  "gen_ai.usage.output_tokens": 800,
  "gen_ai.response.finish_reasons": ["end_turn"],

  "duration_ms": 4500,
  "context": {
    "task_id": "bd-xyz789",
    "task_type": "pr_fix"
  },
  "output_file": "/var/log/egg/model_output/2025-11-28/0af7651916cd43dd.json"
}
```

### Token Usage Tracking

Detailed token metrics enable cost visibility and optimization:

| Metric | Purpose | Alerting Threshold |
|--------|---------|-------------------|
| `gen_ai.usage.input_tokens` | Prompt size tracking | > 100K per request |
| `gen_ai.usage.output_tokens` | Response size tracking | > 50K per request |
| Daily token totals | Budget monitoring | Configurable per user |
| Cost estimates | Financial tracking | Based on model pricing |

Token data flows to cost dashboards and can trigger alerts when usage exceeds thresholds.

### Full Response Storage

Full model responses are stored separately (not in main log stream) due to size:

```
/var/log/egg/model_output/
├── 2025-11-28/
│   ├── 143056_abc123.json      # Full response with trace_id
│   ├── 143112_def456.json      # Timestamp_traceID.json format
│   └── index.jsonl             # Metadata index for fast search
├── 2025-11-29/
│   ├── ...
│   └── index.jsonl
```

#### Daily Index Format

Each directory contains an `index.jsonl` file (newline-delimited JSON) for fast searches without parsing full responses:

```jsonl
{"timestamp": "2025-11-28T14:30:56.789Z", "filename": "143056_abc123.json", "model": "claude-sonnet-4-5", "input_tokens": 1500, "output_tokens": 800, "trace_id": "abc123...", "task_id": "bd-xyz", "error": false}
{"timestamp": "2025-11-28T14:31:12.456Z", "filename": "143112_def456.json", "model": "claude-sonnet-4-5", "input_tokens": 2000, "output_tokens": 1200, "trace_id": "def456...", "task_id": "bd-xyz", "error": false}
```

**Use Cases:**
- **Cost Analytics:** `jq '.input_tokens + .output_tokens' index.jsonl | awk '{sum+=$1} END {print sum}'`
- **Error Searches:** `grep '"error":true' index.jsonl`
- **Task Correlation:** `jq 'select(.task_id=="bd-xyz")' index.jsonl`
- **Retention Policy:** Delete old directories by date (`rm -rf 2025-10-*`)

**Benefits:**
- Fast searches without parsing 100KB+ response files
- Enables cost dashboard queries
- Structured retention (delete by date)
- Trace-based lookup (`abc123.json` → full response)

## GCP Cloud Logging Integration

### Local Development

In local/container mode, logs go to:
1. Console (human-readable format)
2. File (`/var/log/egg/egg.log` - JSON format)

### GCP Production

In GCP Cloud Run, logs automatically flow to Cloud Logging when written to stdout in the correct format.

```python
# The library auto-detects environment
from egg_logging import get_logger

logger = get_logger("slack-receiver")

# Same code works in both environments:
logger.info("Processing PR", pr_number=123, repository="jwbron/egg")

# Local: Pretty console output
# GCP: Structured JSON to Cloud Logging
```

### Log Router Configuration

```hcl
# terraform/infrastructure/logging.tf

resource "google_logging_project_sink" "egg_logs" {
  name        = "egg-log-sink"
  destination = "bigquery.googleapis.com/projects/${var.project}/datasets/egg_logs"

  filter = <<-EOT
    resource.type="cloud_run_revision"
    labels.app="egg"
  EOT
}
```

### Useful Queries

```sql
-- Find all errors in last hour
SELECT timestamp, severity, message, jsonPayload.context
FROM `project.egg_logs.cloudrun_requests`
WHERE severity = "ERROR"
  AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)

-- Trace a specific operation
SELECT *
FROM `project.egg_logs.cloudrun_requests`
WHERE jsonPayload.correlation_id = "abc123"
ORDER BY timestamp

-- Tool usage summary
SELECT
  jsonPayload.tool,
  COUNT(*) as invocations,
  AVG(jsonPayload.duration_ms) as avg_duration_ms
FROM `project.egg_logs.cloudrun_requests`
WHERE jsonPayload.tool IS NOT NULL
GROUP BY jsonPayload.tool
```

## Consequences

### Positive

1. **Debugging**: Filter and search logs by any field
2. **Tracing**: Follow operations across services via correlation_id
3. **Monitoring**: Set up alerts on error rates, tool failures
4. **Cost Visibility**: Track Claude API token usage
5. **GCP Ready**: Zero changes needed for Cloud Logging
6. **Audit Trail**: Complete record of tool invocations
7. **Thread Safety**: Safe for concurrent requests (double-checked locking)
8. **CLI Transparency**: Drop-in replacements for tools (Claude Code unaware)
9. **Source Location**: Jump directly to code from logs in GCP UI
10. **Daily Index**: Fast cost analytics without parsing full responses

### Negative

1. **Migration Effort**: Existing scripts need updates (✅ MITIGATED: 10+ services migrated, CLI wrappers available)
2. **Disk Space**: JSON logs larger than plain text (acceptable trade-off for structure)
3. **Learning Curve**: Developers need to learn new API (✅ MITIGATED: Clean API, good docs)
4. **Dependency**: New shared library to maintain (✅ MITIGATED: Well-tested, stable)

### Trade-offs

| Aspect | print() Statements | egg_logging |
|--------|-------------------|---------------|
| Setup complexity | None | Import + configure |
| Output format | Unstructured text | Structured JSON |
| Searchability | grep only | Field-based queries |
| GCP integration | Manual parsing | Native |
| Disk usage | Lower | Higher |
| Development speed | Faster initially | Faster long-term |

## Alternatives Considered

### Alternative 1: Python Standard Logging Only

**Approach:** Use Python's built-in `logging` module with JSON formatter

**Pros:**
- No new dependencies
- Familiar to Python developers

**Cons:**
- No tool wrappers
- No model output capture
- No context propagation
- Verbose configuration

**Rejected:** Doesn't address tool visibility or model capture requirements

### Alternative 2: Structured Logging Library (structlog)

**Approach:** Use existing library like `structlog`

**Pros:**
- Battle-tested
- Rich features
- Good documentation

**Cons:**
- Still need custom tool wrappers
- Additional dependency
- May have features we don't need

**Rejected:** Adds dependency without solving tool wrapper needs

### Alternative 3: Log to Database Directly

**Approach:** Write logs directly to PostgreSQL/Firestore

**Pros:**
- Immediately queryable
- No file management

**Cons:**
- Network dependency for logging
- Slower than file writes
- Complex failure handling

**Rejected:** Logging should not fail if database is unavailable

## Implementation Plan

### Phase 1: Core Library ✅ COMPLETE

1. ✅ Create `shared/egg_logging/` directory structure
2. ✅ Implement `JibLogger` with JSON formatting
3. ✅ Add console handler for development
4. ✅ Add file handler for local deployment
5. ✅ Basic tests (`tests/shared/egg_logging/`)

**Enhancements Beyond Spec:**
- ✅ BoundLogger pattern for context binding
- ✅ Thread-safe singleton pattern
- ✅ Source location tracking in all logs
- ✅ Environment auto-detection (GCP/container/host)

### Phase 2: Tool Wrappers ✅ PARTIAL

1. ✅ Implement `bd` wrapper (`shared/egg_logging/wrappers/bd.py`)
2. ❌ ~~Implement `git` wrapper~~ - Removed (see note below)
3. ❌ ~~Implement `gh` wrapper~~ - Removed (see note below)
4. ✅ Implement `claude` wrapper (`shared/egg_logging/wrappers/claude.py`)
5. ✅ Integration tests (for bd, claude)

**Note on git/gh wrappers:** These were implemented but never adopted by any production
code. The gateway sidecar requires purpose-built clients (`git_client.py`, `github_client.py`)
with security-specific validation (path traversal prevention, argument allowlists, credential
management) that generic logging wrappers couldn't provide. The wrappers were removed in
January 2026 to reduce dead code.

**Enhancements Beyond Spec:**
- ✅ CLI wrapper binaries (`shared/egg_logging/bin/egg-bd`, `egg-claude`)
- ✅ Shell alias setup script
- ✅ Passthrough mode via environment variables
- ✅ Base `ToolWrapper` class for extensibility

### Phase 3: Model Capture ✅ COMPLETE

1. ✅ Implement model output capture (`shared/egg_logging/model_capture.py`)
2. ✅ Add token tracking (OpenTelemetry GenAI conventions)
3. ✅ Add response storage with daily directories
4. ✅ Add performance metrics

**Enhancements Beyond Spec:**
- ✅ Daily index (`index.jsonl`) for fast searches
- ✅ Thread-safe singleton pattern
- ✅ Context manager API for clean usage
- ✅ Automatic Claude output parsing

### Phase 4: Migration ✅ EXTENSIVE ADOPTION

**Services Using Standardized Logging:**
1. ✅ slack-receiver (`host-services/slack/slack-receiver/`)
2. ✅ slack-notifier (`host-services/slack/slack-notifier/`)
3. ✅ context-sync (`host-services/sync/context-sync/`)
4. ✅ incoming-processor (`sandbox/egg-tasks/slack/`)
5. ✅ github-processor (`sandbox/egg-tasks/github/`)
6. ✅ gateway token refresher (`gateway/`)

**Documentation:**
- ✅ Code examples in all wrapper modules
- ✅ CLI wrapper README (`shared/egg_logging/bin/README.md`)
- ✅ Docstrings for all public APIs
- ✅ Implementation review document (this ADR companion)

### Phase 5: GCP Cloud Logging Integration ⏳ DEFERRED (AS PLANNED)

GCP-specific functionality (Cloud Logging output handler, log router configuration, BigQuery export) will be implemented as part of the GCP migration per [ADR-GCP-Deployment-Terraform](./ADR-GCP-Deployment-Terraform.md). This ensures:

1. GCP infrastructure is available before adding Cloud Logging integration
2. Logging changes are tested alongside other GCP components
3. Terraform configuration for log sinks can be added atomically

**Deferred to GCP Migration:**
- Cloud Logging output handler activation
- Log router/sink Terraform configuration
- BigQuery export for log analytics
- Production monitoring dashboards

## Related ADRs

| ADR | Relationship |
|-----|---------------|
| [ADR-GCP-Deployment-Terraform](./ADR-GCP-Deployment-Terraform.md) | Defines Cloud Run deployment where logs flow to Cloud Logging |
| [ADR-Internet-Tool-Access-Lockdown](./ADR-Internet-Tool-Access-Lockdown.md) | Tool wrappers complement gateway audit logging |
| [ADR-Autonomous-Software-Engineer](../in-progress/ADR-Autonomous-Software-Engineer.md) | Parent ADR defining debugging and observability needs |
| [ADR-LLM-Inefficiency-Reporting](../implemented/ADR-LLM-Inefficiency-Reporting.md) | Structured logging enables trace collection and inefficiency detection described in that ADR |

---

**Last Updated:** 2026-01-27
**Status:** Implemented (Phases 1, 3-4 Complete; Phase 2 Partial) - See [Implementation Review](./ADR-Standardized-Logging-Interface-IMPLEMENTATION-REVIEW.md)
