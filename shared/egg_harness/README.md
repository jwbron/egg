# egg_harness

Custom coding harness for egg's agent runtime with multi-provider LLM support, context management, and session persistence.

## Overview

`egg_harness` is a self-contained Python package that replaces the Claude Agent SDK and Claude Code CLI as egg's primary agent execution engine. It provides:

- **Provider-abstracted LLM client** — Anthropic (via SDK) and OpenAI-compatible endpoints (via httpx)
- **8 standard tools** — Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch
- **Core agent loop** — streaming, tool execution, turn limits, timeouts, graceful shutdown
- **Context management** — token tracking, threshold-based compaction, compaction-safe retry
- **Session persistence** — JSONL serialization for conversation resume across restarts
- **Event system** — composable callbacks for monitoring and extension
- **Interactive mode** — multi-turn terminal REPL

The package is designed to be **extractable** — it has no imports from `orchestrator/`, `gateway/`, or `sandbox/`. All egg-specific integrations are injected via the separate [`egg_harness_integration`](../egg_harness_integration/README.md) package.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    egg_harness (core)                    │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Provider  │  │   Tool   │  │     Agent Loop       │  │
│  │ Layer     │  │ Registry │  │  (loop.py)           │  │
│  │           │  │          │  │                      │  │
│  │ Anthropic │  │ Bash     │  │  prompt → API call   │  │
│  │ OpenAI    │  │ Read     │  │  → parse response    │  │
│  │           │  │ Write    │  │  → execute tools     │  │
│  └──────────┘  │ Edit     │  │  → feed results back │  │
│                │ Glob     │  │  → repeat             │  │
│  ┌──────────┐  │ Grep     │  └──────────────────────┘  │
│  │ Config   │  │ WebFetch │                             │
│  │ Cost     │  │ WebSearch│  ┌──────────────────────┐  │
│  │ Events   │  └──────────┘  │ Context Management   │  │
│  │ Result   │                │ Session Persistence  │  │
│  │ Prompt   │                └──────────────────────┘  │
│  └──────────┘                                           │
└─────────────────────────────────────────────────────────┘
```

## Usage

### Headless (drop-in replacement for `python3 -m egg_agent`)

```bash
# CLI entry point
python3 -m egg_harness --model opus --max-turns 200 "Fix the authentication bug"

# Read prompt from stdin
echo "Fix the bug" | python3 -m egg_harness --model sonnet --max-turns 50

# With system prompt
python3 -m egg_harness --model opus --max-turns 200 \
  --system-prompt "You are a security reviewer" \
  "Review this code for vulnerabilities"
```

### Programmatic API

```python
from egg_harness.client import run_agent, run_agent_async

# Synchronous
result = run_agent(
    prompt="Fix the authentication bug",
    model="opus",
    max_turns=200,
)
print(result.stdout)
print(f"Cost: ${result.cost_usd:.4f}, Turns: {result.num_turns}")

# Async with streaming callback
async def on_output(text: str) -> None:
    print(text, end="", flush=True)

result = await run_agent_async(
    prompt="Fix the authentication bug",
    model="opus",
    max_turns=200,
    on_output=on_output,
)
```

### Interactive REPL

```bash
python3 -m egg_harness --interactive --model opus
```

## Package Structure

```
egg_harness/
├── __init__.py              # Public API exports
├── __main__.py              # CLI entry point (python3 -m egg_harness)
├── client.py                # High-level run_agent() / run_agent_async()
├── loop.py                  # Core agent loop with compaction support
├── session.py               # Session persistence (JSONL serialize/resume)
├── compaction.py            # Context management / compaction strategy
├── events.py                # Event bus / callback system
├── config.py                # Provider config, model aliases, timeouts
├── prompt.py                # System prompt assembly (generic)
├── cost.py                  # Token cost tracking
├── result.py                # AgentResult dataclass
├── interactive.py           # Interactive terminal mode
├── providers/
│   ├── __init__.py
│   ├── base.py              # Provider interface (ABC) and StreamEvent types
│   ├── anthropic.py         # Anthropic Messages API provider (via SDK)
│   └── openai_compat.py     # OpenAI-compatible endpoint provider (via httpx)
├── tools/
│   ├── __init__.py
│   ├── registry.py          # Tool registration and dispatch
│   ├── bash.py              # Shell command execution
│   ├── read.py              # File reading
│   ├── write.py             # File creation/overwrite
│   ├── edit.py              # String replacement editing
│   ├── glob_tool.py         # File pattern matching
│   ├── grep.py              # Content search (ripgrep)
│   ├── web_fetch.py         # URL content fetching
│   └── web_search.py        # Web search
└── pyproject.toml           # Package metadata and dependencies
```

## Core Components

### Provider Layer

The provider layer abstracts LLM backends behind a common interface:

```python
from egg_harness.providers.base import Provider, StreamEvent

class Provider(ABC):
    async def send_message(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None,
        model: str,
    ) -> AsyncIterator[StreamEvent]:
        ...
```

**StreamEvent types:**

| Event | Description |
|-------|-------------|
| `TextDelta` | Incremental text output from the model |
| `ToolUseStart` | Start of a tool call (tool name, ID) |
| `ToolUseInputDelta` | Incremental JSON input for a tool call |
| `ToolUseEnd` | End of a tool call |
| `ThinkingDelta` | Extended thinking output (Anthropic) |
| `MessageStart` | Start of a new message (model, usage) |
| `MessageDelta` | Message-level metadata updates (stop_reason, usage) |
| `MessageEnd` | End of message |

**Anthropic provider** (`providers/anthropic.py`):
- Uses `anthropic.AsyncAnthropic` SDK with `base_url` pointing to the gateway proxy
- Handles streaming with 8+ SSE event types
- Supports cache control headers (`anthropic-beta: prompt-caching`)
- Extended thinking passthrough
- Gateway URL validation at init (CVE-2026-21852 mitigation)
- Startup assertion that `ANTHROPIC_API_KEY` is NOT in `os.environ`

**OpenAI-compatible provider** (`providers/openai_compat.py`):
- Raw `httpx.AsyncClient` for SSE streaming against `/v1/chat/completions`
- Targets vLLM, Ollama, and any OpenAI-compatible endpoint
- Capability declaration config (tool_choice, streaming, system message support)
- Model passed as-is (no alias mapping)

Both providers include:
- Exponential backoff retry for transient errors (429, 5xx, connection reset)
- Max 3 retries with jitter
- Circuit breaker: 3 consecutive non-retryable failures triggers immediate abort

### Configuration

```python
from egg_harness.config import ProviderConfig, HarnessConfig

# Provider configuration
provider_config = ProviderConfig(
    provider="anthropic",           # or "openai-compatible"
    model="opus",                   # alias or full model ID
    endpoint=None,                  # for openai-compatible: "http://localhost:8000/v1"
    api_key_env=None,               # env var name for API key
)

# Harness configuration
harness_config = HarnessConfig(
    max_turns=200,                  # maximum conversation turns
    timeout=7200,                   # wall-clock timeout in seconds
    cwd="/path/to/working/dir",     # working directory for tools
    disallowed_tools=[],            # tools to block
    compaction_threshold=0.8,       # compact at 80% of context window
    keep_recent_tokens=20000,       # tokens to keep after compaction
)
```

**Model aliases:**

| Alias | Resolves To |
|-------|-------------|
| `opus` | `claude-opus-4-6` |
| `sonnet` | `claude-sonnet-4-5-20250514` |
| `haiku` | `claude-haiku-4-5` |

The `opus[1m]` suffix syntax is supported for backwards compatibility, setting `max_tokens=1000000` with appropriate beta headers.

### Tool System

Tools are registered in a `ToolRegistry` and executed by name:

```python
from egg_harness.tools.registry import ToolRegistry

registry = ToolRegistry()

# Register a custom tool
registry.register(
    tool_def={"name": "my_tool", "description": "...", "input_schema": {...}},
    handler=my_tool_handler,
)

# Execute a tool
result = await registry.execute("my_tool", {"param": "value"})

# Get all tool definitions (for API calls)
definitions = registry.get_definitions()
```

**Permission callbacks** are invoked before each tool execution:

```python
def can_use_tool(name: str, input: dict) -> str | None:
    """Return error string to block, None to allow."""
    if name == "Write" and "/etc/" in input.get("file_path", ""):
        return "Cannot write to /etc/ directory"
    return None

registry.set_permission_callback(can_use_tool)
```

**Standard tools** match Claude Code's JSON schemas and behavior:

| Tool | Key Features |
|------|-------------|
| **Bash** | Subprocess execution, process group timeout, working directory. Never uses `shell=true` (CVE-2026-35022). |
| **Read** | Line numbers (cat -n format), offset/limit, binary detection, image passthrough, PDF page range. |
| **Write** | File create/overwrite, parent directory creation. |
| **Edit** | Exact string replacement, `replace_all` mode, uniqueness validation. |
| **Glob** | File pattern matching via pathlib/fd, sorted by modification time. |
| **Grep** | Content search via ripgrep, regex, file type filter, context lines, output modes. |
| **WebFetch** | URL fetch, HTML-to-markdown conversion, prompt processing. Disabled in private mode. |
| **WebSearch** | Web search query. Disabled in private mode. |

### Context Management / Compaction

Long-running agents hit context limits. The harness tracks token usage per turn and compacts when the context window fills:

1. **Token tracking** — each API response includes token counts; the harness maintains a running total
2. **Threshold trigger** — when total tokens exceed `compaction_threshold` (default 80%) of the model's context window, compaction fires before the next API call
3. **Compaction strategy** — walk backwards from newest messages, keeping `keep_recent_tokens` (default 20,000) of recent history. Never split tool call/result pairs. Generate a structured summary of older messages (goal, progress, decisions, files modified, errors). Clear history and inject summary as the new conversation start.
4. **Loop protection** — if compaction fires twice within 3 turns (configurable), abort with error to prevent infinite compaction loops
5. **Manual compaction** — `loop.compact_now()` for agents that want to compact at natural checkpoints
6. **Event emission** — `on_compaction` callback fires with summary, tokens_before, and tokens_after

### Session Persistence

Conversations are serialized to JSONL for resume across restarts:

```python
from egg_harness.session import Session

# Auto-save on compaction and at configured intervals
session = Session(save_path="/tmp/egg-sessions/session-123.jsonl")

# Resume from file
session = Session.resume("/tmp/egg-sessions/session-123.jsonl")
```

Session metadata includes: `session_id`, `model`, `total_cost`, `turn_count`, `duration_ms`, `compaction_count`, `created_at`, `updated_at`.

### Event System

Composable callbacks for monitoring and extension:

```python
from egg_harness.events import EventBus

bus = EventBus()

# Register callbacks
bus.on_output(lambda text: print(text, end=""))
bus.on_tool_call(lambda name, input: log.info(f"Tool: {name}"))
bus.on_compaction(lambda summary, before, after: log.info(f"Compacted: {before} -> {after} tokens"))
bus.on_error(lambda error: log.error(f"Error: {error}"))
bus.on_turn_complete(lambda turn, usage: log.info(f"Turn {turn}: {usage}"))

# Multiple callbacks per event type are supported
# Callback exceptions are caught and logged, not propagated
```

### Agent Loop

The core loop ties providers and tools together:

```
prompt → API call → parse StreamEvents → accumulate text + tool calls
    → execute tools via ToolRegistry → append results → repeat
```

- **Turn limits** — configurable max turns (default 200)
- **Timeout** — wall-clock timeout (default 2 hours)
- **Stop reasons** — `end_turn` (done), `max_tokens` (continue), `stop_sequence` (done), `tool_use` (execute tools)
- **Circuit breaker** — 3 consecutive tool failures triggers abort
- **SIGTERM** — graceful shutdown with 30s grace period for in-flight tool execution
- **Streaming** — text emitted via `on_output` callback as it arrives
- **Compaction** — token tracking per turn, automatic compaction at threshold

### Result Type

```python
from egg_harness.result import AgentResult

# All existing fields preserved (backward-compatible)
result.success          # bool — did the agent complete successfully?
result.stdout           # str — accumulated text output
result.stderr           # str — error output
result.returncode       # int — exit code
result.cost_usd         # float — total cost in USD
result.num_turns        # int — number of conversation turns
result.duration_ms      # int — wall-clock duration in milliseconds
result.session_id       # str — unique session identifier

# New field
result.compaction_count # int | None — number of times context was compacted
```

## Security Model

The harness enforces a strict security model:

1. **Zero-credential sandbox** — `ANTHROPIC_API_KEY` must NOT be in `os.environ` (asserted at provider init). All API calls route through the gateway proxy which injects credentials.
2. **Gateway URL validation** — the Anthropic provider validates the base URL against expected patterns to prevent redirect-based key exfiltration (CVE-2026-21852 mitigation).
3. **No `shell=true`** — the Bash tool uses `["bash", "-c", command]` pattern, never `subprocess(shell=True)` (CVE-2026-35022 mitigation).
4. **Permission callbacks** — the tool registry invokes a permission callback before every tool execution. The integration layer wires this to egg's role-based file restriction system.
5. **Tool disallow list** — tools can be blocked by name (e.g., WebFetch/WebSearch in private network mode).
6. **Defense in depth** — tool filtering at both the harness level (permission callbacks) and the gateway level (private mode tool stripping).

## Configuration Reference

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--model` | `opus` | Model alias or full model ID |
| `--max-turns` | `200` | Maximum conversation turns |
| `--timeout` | `7200` | Wall-clock timeout in seconds |
| `--system-prompt` | None | System prompt text or file path |
| `--interactive` | false | Launch interactive REPL |
| `prompt` | stdin | Prompt text (positional arg or stdin) |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_BASE_URL` | Gateway proxy URL for Anthropic API calls |
| `EGG_NETWORK_MODE` | `public` or `private` — controls WebFetch/WebSearch availability |

### Model Context Windows

The harness maintains a lookup table of context window sizes per model for compaction threshold calculations. See `config.py` for the current table.

## Dependencies

Core harness dependencies are minimal:

- `anthropic>=0.50,<1.0` — Anthropic Python SDK
- `httpx` — HTTP client for OpenAI-compatible endpoints
- Python standard library

## Related

- [`egg_harness_integration`](../egg_harness_integration/README.md) — egg-specific integration layer
- [Custom Harness Architecture](../../docs/architecture/custom-harness.md) — design decisions and security model
- [Anchor Recovery Guide](../../docs/guides/anchor-recovery.md) — compaction + anchor integration
- Issue [#1570](https://github.com/jwbron/egg/issues/1570) — build custom coding harness
- Issue [#1571](https://github.com/jwbron/egg/issues/1571) — open-model hierarchy (depends on provider abstraction)
- Issue [#1032](https://github.com/jwbron/egg/issues/1032) — agent anchor mechanism (compaction integration)
