# Custom Coding Harness Architecture

> Design document for egg's custom coding harness (`egg_harness`) — a provider-abstracted agent runtime with context management, multi-provider support, and session persistence.

## Motivation

egg currently depends on two Anthropic-maintained runtimes:

| Mode | Runtime | Limitation |
|------|---------|------------|
| **Headless** | Claude Agent SDK (`claude_agent_sdk.query()`) | Opaque execution, no context management control, Anthropic-only |
| **Interactive** | Claude Code CLI (`claude --dangerously-skip-permissions`) | Opaque compaction, `curl \| bash` install, Anthropic-only |

Both are external dependencies that evolve on Anthropic's release schedule and can break egg on update. Neither supports non-Anthropic models. Context window exhaustion is handled opaquely with no integration into egg's anchor mechanism (#1032).

The custom harness addresses these limitations while retaining the existing runtimes as supported alternatives for users with Anthropic subscriptions.

### Goals

1. **Full control over agent execution** — tool definitions, permission enforcement, streaming, context management, and restart semantics owned by egg
2. **Context management / compaction** — threshold-based compaction with anchor persistence, replacing opaque runtime compaction
3. **Multi-provider support** — Anthropic and OpenAI-compatible endpoints from a single abstraction
4. **Reduced external dependencies** — eliminate Claude Code `curl | bash` install and opaque SDK behavior
5. **Extractability** — core harness structured as an isolated package with clean interfaces

### Non-Goals (MVP)

- Additional providers beyond Anthropic and OpenAI-compatible (add incrementally)
- MCP server support
- Agent-to-agent delegation
- Slash commands / skills system
- Rich TUI (syntax highlighting, markdown rendering)

## Architectural Approach

**Hybrid Anthropic SDK + Raw httpx** (Approach D from v2 architecture analysis):

| Provider | Client | Rationale |
|----------|--------|-----------|
| Anthropic | `anthropic.AsyncAnthropic` SDK | Anthropic streaming has 8+ SSE event types; the SDK handles accumulation, partial JSON, and error recovery reliably |
| OpenAI-compatible | Raw `httpx.AsyncClient` | OpenAI SSE format is simpler; no heavy SDK dependency needed |

This approach was selected over four alternatives:

| Approach | Verdict | Why |
|----------|---------|-----|
| A. Anthropic SDK only | Rejected | Can't call OpenAI-compatible `/v1/chat/completions` endpoints |
| B. Raw httpx only | Rejected | Too much risk reimplementing Anthropic streaming (8+ event types, partial JSON) |
| C. LiteLLM | Rejected | 50+ transitive deps, conflicts with gateway credential model |
| **D. Hybrid** | **Selected** | Best reliability for critical Anthropic path, manageable complexity for simpler OpenAI format |

## Two-Package Design

The harness is split into two packages to support the extractability requirement:

```
shared/egg_harness/              ← Core (no egg imports, extractable)
shared/egg_harness_integration/  ← Egg-specific wiring
```

### Core Harness (`egg_harness`)

Self-contained Python package with its own `pyproject.toml`. Dependencies: `anthropic`, `httpx`, standard library. No imports from `orchestrator/`, `gateway/`, or `sandbox/`.

**Subsystems:**

| Subsystem | Module(s) | Responsibility |
|-----------|-----------|----------------|
| Providers | `providers/base.py`, `anthropic.py`, `openai_compat.py` | LLM backend abstraction, streaming, retry |
| Tools | `tools/registry.py`, `bash.py`, `read.py`, etc. | Tool registration, permission checks, execution |
| Agent Loop | `loop.py` | Core prompt→tool→result cycle, turn limits, timeout, SIGTERM |
| Context Mgmt | `compaction.py` | Token tracking, threshold-based compaction, loop protection |
| Session | `session.py` | JSONL persistence, resume from file |
| Events | `events.py` | Composable callback system |
| Config | `config.py`, `cost.py`, `prompt.py`, `result.py` | Configuration, cost tracking, prompt assembly, result type |
| CLI | `__main__.py`, `client.py`, `interactive.py` | Entry points for headless, programmatic, and interactive use |

### Egg Integration Layer (`egg_harness_integration`)

Thin wiring package that connects the core harness to egg's infrastructure:

| Module | Responsibility |
|--------|----------------|
| `egg_tools.py` | Registers 5 egg-native tools (EggOrch, EggContract, EggCheckpoint, GitOps, GhCli) via CLI shell-out |
| `egg_prompt.py` | CLAUDE.md rule-merging replicating `setup_agent_rules()` |
| `egg_permissions.py` | Adapts `egg_restrictions` for `can_use_tool` callback |
| `egg_compaction.py` | Anchor-based compaction (#1032) — persist/recover via `.egg-state/agent-anchors/` |
| `harness_factory.py` | Factory function wiring all integrations into a configured `AgentLoop` |

## Interface Design

All interfaces are defined as protocols/ABCs in the core harness. The integration layer implements them.

### Provider Interface

```python
class Provider(ABC):
    async def send_message(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None,
        model: str,
    ) -> AsyncIterator[StreamEvent]: ...
```

The `StreamEvent` union type provides 8 event types for composable stream processing:

```
StreamEvent = TextDelta | ToolUseStart | ToolUseInputDelta | ToolUseEnd
            | ThinkingDelta | MessageStart | MessageDelta | MessageEnd
```

### Tool Interface

```python
class ToolRegistry:
    def register(self, tool_def: dict, handler: Callable) -> None: ...
    async def execute(self, name: str, input: dict) -> ToolResult: ...
    def get_definitions(self) -> list[dict]: ...
    def set_permission_callback(self, callback: PermissionCallback) -> None: ...
```

### Permission Callback

```python
# Return error string to block, None to allow
PermissionCallback = Callable[[str, dict], str | None]
```

### Event Callbacks

```python
class EventBus:
    def on_output(self, callback: Callable[[str], None]) -> None: ...
    def on_tool_call(self, callback: Callable[[str, dict], None]) -> None: ...
    def on_tool_result(self, callback: Callable[[str, str], None]) -> None: ...
    def on_compaction(self, callback: Callable[[str, int, int], None]) -> None: ...
    def on_error(self, callback: Callable[[Exception], None]) -> None: ...
    def on_turn_complete(self, callback: Callable[[int, dict], None]) -> None: ...
```

## Security Architecture

The harness maintains egg's zero-credential sandbox model:

```
┌──────────────────────────┐      ┌──────────────────────────┐
│   Sandbox Container      │      │   Gateway Sidecar        │
│   (untrusted)            │      │   (trusted)              │
│                          │      │                          │
│  egg_harness             │      │  - Injects API keys      │
│   ├── AnthropicProvider  │─────→│  - Validates requests    │
│   │   base_url=gateway   │ HTTP │  - Captures transcripts  │
│   │   NO API key         │      │  - Enforces branch/phase │
│   │                      │      │    policies              │
│   ├── ToolRegistry       │      │                          │
│   │   ├── Bash (no shell=true)  └──────────────────────────┘
│   │   ├── Read/Write/Edit
│   │   └── Permission callback
│   │       └── egg_restrictions
│   │
│   └── ANTHROPIC_API_KEY
│       asserted ABSENT
│
└──────────────────────────┘
```

### Mandatory Security Controls

| Control | Implementation | Mitigates |
|---------|---------------|-----------|
| API key absence assertion | Provider init checks `ANTHROPIC_API_KEY not in os.environ` | Credential leakage |
| Gateway URL validation | Validate `base_url` against expected pattern | CVE-2026-21852 (redirect-based key exfiltration) |
| No `shell=true` | Bash tool uses `["bash", "-c", cmd]` | CVE-2026-35022 (command injection) |
| Permission callbacks | `can_use_tool` invoked before every tool execution | Role-based file access enforcement |
| Tool disallow list | `disallowed_tools` in config blocks tools by name | Private mode tool restriction |
| Gateway proxy routing | All API traffic goes through gateway | Credential injection, transcript capture |

## Context Management / Compaction

Long-running agents (especially in BRC consensus loops) fill their context window. The harness owns compaction rather than delegating to an opaque runtime:

```
Turn 1: [system] + [user] + [assistant] + [tool_result] ...
Turn N: Context at 85% capacity → COMPACTION TRIGGER
        1. Walk backwards from newest message
        2. Keep 20K tokens of recent history
        3. Never split tool_call / tool_result pairs
        4. Summarize older messages (goal, progress, decisions, files, errors)
        5. Persist summary to agent anchor (#1032)
        6. Clear history → inject summary as new start
        7. Emit on_compaction event
Turn N+1: [system] + [summary] + [recent_messages] ...
```

**Integration with anchors (#1032):** On compaction, the agent's state is written to `.egg-state/agent-anchors/<agent-id>.json` using the existing `egg_anchor` package. Post-compaction, the agent reads the anchor and polls the message bus for any BRC messages missed during compaction. This aligns with the anchor recovery protocol documented in [Anchor Recovery](../guides/anchor-recovery.md).

**Safety:** If compaction fires twice within 3 turns (configurable), the agent aborts with an error to prevent infinite compaction loops.

## Harness Selection

Three harness options, selectable via `EGG_HARNESS` environment variable or `PipelineConfig`:

| Value | Runtime | Use Case |
|-------|---------|----------|
| `egg` | egg_harness | Multi-provider, full context management control |
| `claude-sdk` (default) | Claude Agent SDK | Anthropic subscription users (headless) |
| `claude-code` | Claude Code CLI | Anthropic subscription users (interactive) |

The default is `claude-sdk` during the transition period. The egg harness becomes the default only after parallel validation demonstrates metric parity (cost, turns, duration, task success rate).

**Rollback:** Setting `EGG_HARNESS=claude-sdk` (or unsetting it) immediately rolls back to the existing SDK path with zero code changes.

## Component Interaction Flow

```
1. Entry: python3 -m egg_harness --model opus --max-turns 200 'prompt'
   └── or: egg_agent/client.py routes here when EGG_HARNESS=egg

2. harness_factory.create_egg_harness()
   ├── ProviderConfig(provider="anthropic", model="opus", base_url=GATEWAY_URL)
   ├── ToolRegistry: 8 standard tools + 5 egg-native tools
   ├── Permission callback: egg_restrictions.check_agent_file_access()
   ├── System prompt: CLAUDE.md rule-merging + additional prompt
   ├── Compaction handler: anchor-based (#1032)
   └── EventBus: on_output, on_tool_call, on_compaction, ...

3. AgentLoop.run(prompt)
   ├── Build messages [system, user(prompt)]
   ├── Loop:
   │   ├── Provider.send_message(messages, tools, system, model)
   │   ├── Consume StreamEvents → accumulate text + tool calls
   │   ├── For each tool call: ToolRegistry.execute()
   │   │   ├── Permission callback check
   │   │   ├── Tool handler execution
   │   │   └── Output truncation
   │   ├── Append tool results as user messages
   │   ├── Check: token budget → trigger compaction if needed
   │   ├── Check: turn limit, timeout, circuit breaker
   │   └── Continue or stop based on stop_reason
   └── Return AgentResult(cost_usd, num_turns, duration_ms, compaction_count, ...)

4. Gateway proxy (unchanged):
   └── Intercepts /v1/messages → injects credentials → captures transcripts
```

## Risk Assessment Summary

The v2 risk analyst identified 12 risks (3 CRITICAL, 4 HIGH). Key mitigations are integrated into the architecture:

| Risk Level | Count | Key Concerns |
|------------|-------|-------------|
| CRITICAL | 3 | Credential handling, tool behavioral parity, agent loop reliability |
| HIGH | 4 | OpenAI provider variance, transcript capture, CLAUDE.md loading, three harness options |
| MEDIUM | 3 | Extended thinking, interactive UX, SDK version coupling |
| LOW | 2 | Egg-native tool latency, scope creep |

**Five mandatory human review gates** before production use:
1. Credential flow — verify zero-credential sandbox preserved
2. Agent loop implementation — turn counting, context management, SIGTERM
3. Tool behavioral parity — especially Edit tool semantics
4. System prompt assembly — compare output against Claude Code
5. Parallel validation results — metric parity before default switch

## Design Decisions

| ID | Decision | Rationale |
|----|----------|-----------|
| DD-1 | Anthropic SDK + httpx hybrid | SDK handles complex Anthropic streaming; httpx suffices for simpler OpenAI format |
| DD-2 | Two-package split (core + integration) | Modularity requirement — core must be extractable |
| DD-3 | Shell out to CLIs for egg-native tools | HITL #3 — lower risk, faster MVP |
| DD-4 | `AsyncIterator[StreamEvent]` interface | Composable stream processing via async generators |
| DD-5 | Replicate exact CLAUDE.md rule-merging | HITL #9 — simplifying risks behavioral differences |
| DD-6 | Keep transcript capture in gateway | HITL #6 — gateway already captures; no duplication |
| DD-7 | Default `claude-sdk`, opt-in `egg` | HITL #4 — safe transition with runtime rollback |
| DD-8 | Hardcoded per-model cost rates | HITL #7 — simple, no external pricing API dependency |
| DD-9 | Pin `anthropic>=0.50,<1.0` | Risk analyst recommendation — stability within known range |
| DD-10 | `haiku` maps to `claude-haiku-4-5` | Haiku 3 retires 2026-04-19; must use current model |

## Related

- [egg_harness README](../../shared/egg_harness/README.md) — package documentation
- [egg_harness_integration README](../../shared/egg_harness_integration/README.md) — integration layer docs
- [Credential Injection](credential-injection.md) — gateway credential model
- [Anchor Recovery](../guides/anchor-recovery.md) — compaction + anchor integration
- Issue [#1570](https://github.com/jwbron/egg/issues/1570) — build custom coding harness
- Issue [#1571](https://github.com/jwbron/egg/issues/1571) — open-model hierarchy
- Issue [#1032](https://github.com/jwbron/egg/issues/1032) — agent anchor mechanism
