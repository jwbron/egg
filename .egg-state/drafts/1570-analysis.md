# Analysis: Build Custom Coding Harness with Multi-Provider Support

> Issue: #1570 | Phase: refine

## Problem Statement

egg currently depends on two Anthropic-controlled runtimes — the Claude Agent SDK (`claude_agent_sdk.query()`) for headless pipelines and the Claude Code CLI for interactive sessions. Both are opaque, evolve on their own release schedule, and can break egg without warning. The issue proposes building a custom coding harness (`egg_harness`) that owns the full agent execution loop — tool definitions, permission enforcement, streaming, context management, and LLM provider abstraction — while retaining Claude Code and the Agent SDK as supported alternatives.

**Current state:** All agent execution routes through either `shared/egg_agent/client.py` (SDK wrapper) or `sandbox/llm/runner.py` (CLI exec). Both ultimately hit the gateway's `/v1/messages` proxy for credential injection.

**Desired outcome:** A new `shared/egg_harness/` package that provides a drop-in replacement for `run_agent_async()` with pluggable LLM backends (Anthropic first, then OpenAI-compatible), a fully owned tool system, and an interactive terminal mode.

## Current Behavior

### Agent SDK Path (Headless)

`shared/egg_agent/client.py` wraps `claude_agent_sdk.query()`:

- **Options**: `ClaudeAgentOptions` with `permission_mode="bypassPermissions"`, model, cwd, env, `setting_sources=["project", "user"]`, disallowed_tools, max_turns, system_prompt
- **Streaming**: Async iterator yielding `AssistantMessage` (TextBlock, ToolUseBlock), `UserMessage` (ToolResultBlock), `SystemMessage`, `ResultMessage`
- **Tool interception**: `can_use_tool` callback delegates to `tool_interceptor.py` for role-based file write blocking
- **Result**: `AgentResult(success, stdout, stderr, returncode, cost_usd, num_turns, duration_ms, session_id)`
- **Consumers**: Orchestrator (via `build_agent_command()` + subprocess), consensus wrapper (restart-on-exit), babysit fixer/reviewer

### Claude Code CLI Path (Interactive)

`sandbox/llm/runner.py` execs `claude --dangerously-skip-permissions`:

- **Setup**: `sandbox/entrypoint.py:setup_claude()` configures `settings.json`, `.claude.json`, `~/.claude/CLAUDE.md`, commands, and skills
- **Model**: Hardcoded `opus[1m]`
- **Execution**: `os.execvpe()` replaces the process entirely

### Gateway Proxy

`gateway/gateway.py` at `/v1/messages`:

- Proxies all Anthropic API traffic, injecting credentials (container has no `ANTHROPIC_API_KEY`)
- Supports both streaming (SSE chunked) and non-streaming responses
- Captures transcripts to per-session JSONL buffers via `transcript_buffer.py`
- Strips `WebFetch`/`WebSearch` tool definitions in private mode

### Key Dependencies

- `claude-agent-sdk` (requires `anyio`, `mcp`) — version unpinned in `pyproject.toml`, currently installed as 0.1.56 in the container image
- No `anthropic` Python package installed (all API calls go through the SDK or gateway proxy)
- No usage of extended thinking or prompt caching features currently

## Constraints

### Technical

- **Gateway must remain the trust boundary**: Credentials never reach the sandbox process. The harness must route API calls through the gateway proxy (`ANTHROPIC_BASE_URL`), not directly to Anthropic.
- **Tool interception must be preserved**: Role-based file write blocking (`shared/egg_restrictions/`) is a security-critical feature enforced at both the harness level (pre-execution) and gateway level (push validation).
- **Streaming is required**: The consensus wrapper and babysit agents depend on real-time stdout streaming for heartbeats and progress monitoring. Non-streaming would cause timeout kills.
- **Same CLI interface**: `python3 -m egg_agent` (or `python3 -m egg_harness`) must accept `--model`, `--max-turns`, `--system-prompt`, `--timeout` with identical semantics to avoid changes in orchestrator, consensus wrapper, and babysit agents.
- **Container image size**: Adding the `anthropic` Python SDK (~5MB) is minor, but the tool implementations (especially Bash with timeout, Grep with ripgrep) need system-level binaries already present in the sandbox image.

### Business

- **Migration risk**: This is the core execution path for every agent. A bug in the harness breaks all pipelines. Parallel running (both harnesses) is essential during transition.
- **Multi-provider is the end goal, but Anthropic parity is the blocker**: The harness cannot ship until it matches Claude Agent SDK behavior for all egg use cases.
- **Related issue**: #1571 (open-model hierarchy) depends on this for multi-provider routing.

### Dependencies

- `ripgrep` (`rg`) binary for Grep tool
- System `bash` for Bash tool
- `httpx` or `aiohttp` for direct API calls (neither currently installed for sandbox use — the Agent SDK handles API transport today)
- Gateway `/v1/messages` endpoint compatibility (already stable, used by Agent SDK)

## Options Considered

### Option A: Direct Anthropic SDK Client

**Approach**: Use the official `anthropic` Python SDK (`anthropic.AsyncAnthropic`) to call `/v1/messages` through the gateway proxy. Implement tool execution, the agent loop, and the OpenAI-compatible provider as separate modules.

**Pros**:
- Battle-tested HTTP client with proper retry logic, rate limiting, SSE parsing
- Handles streaming edge cases (partial chunks, reconnection, content block assembly)
- Type-safe response models (`Message`, `ContentBlock`, `ToolUseBlock`, etc.)
- Maintained by Anthropic — API changes get SDK updates quickly
- `base_url` parameter makes gateway proxy routing trivial
- Extended thinking and prompt caching support built-in for future use

**Cons**:
- Adds a dependency on Anthropic's SDK (albeit a smaller/simpler one than the Agent SDK)
- SDK's response types differ from Agent SDK's message types — mapping code needed
- OpenAI-compatible provider still needs a separate client (e.g., `openai` SDK or raw `httpx`)

### Option B: Raw HTTP Client (httpx/aiohttp)

**Approach**: Build the API client from scratch using `httpx` for both Anthropic and OpenAI-compatible endpoints. Parse SSE streams manually.

**Pros**:
- Zero external dependencies on LLM vendor SDKs
- Full control over HTTP behavior (timeouts, retries, connection pooling)
- Single HTTP library for all providers
- Easier to add custom headers, request/response logging

**Cons**:
- SSE parsing is non-trivial — partial chunks, multiple events per chunk, keep-alive comments
- Must maintain own retry logic, rate limit handling, error type mapping
- Must maintain own type definitions for API request/response shapes
- Higher risk of subtle bugs in streaming edge cases
- Significantly more code to write and maintain

### Option C: LiteLLM as Abstraction Layer

**Approach**: Use LiteLLM (`litellm` package) as a unified interface for all providers. It translates OpenAI-format calls to provider-native APIs.

**Pros**:
- Single API for Anthropic, OpenAI-compatible, Google, and 100+ providers
- Tool calling translation already handled
- Active community, frequent updates

**Cons**:
- Heavy dependency (~50+ transitive deps) — bloats container image
- Abstraction hides provider-specific features (cache control, extended thinking)
- Another external dependency with its own release cadence and breakage risk
- Streaming translation can introduce latency and lose provider-specific events
- Overkill for two providers (Anthropic + OpenAI-compatible)
- Gateway already handles credential injection — LiteLLM's key management conflicts

### Option D: Hybrid — Anthropic SDK + Raw httpx for OpenAI-Compatible

**Approach**: Use the `anthropic` SDK for the Anthropic provider (leveraging its streaming, types, and retry logic) and `httpx` for the OpenAI-compatible provider (simpler API surface, standard SSE).

**Pros**:
- Best-in-class client for each provider
- Anthropic SDK handles all Anthropic API complexity (streaming, types, retries)
- OpenAI `/v1/chat/completions` is simpler to implement from scratch (well-documented, widely tested format)
- Avoids vendor lock-in on the OpenAI side (no `openai` SDK dependency)
- Adds only `anthropic` + `httpx` as dependencies (httpx is already a transitive dep of `anthropic`)

**Cons**:
- Two different HTTP patterns to maintain
- Provider interface must abstract over different response shapes
- `anthropic` SDK still an external dependency

## Recommended Approach

**Option D: Hybrid — Anthropic SDK + Raw httpx for OpenAI-Compatible**

This is the pragmatic choice. The Anthropic Messages API has significant streaming complexity (SSE with content block deltas, tool use events, rate limit headers) that the official SDK handles correctly. Reimplementing this (Option B) would be high-risk for egg's most critical code path. LiteLLM (Option C) is too heavy and conflicts with the gateway's credential model. Using the Anthropic SDK for its own API while using httpx for the simpler OpenAI-compatible format gives the best balance of reliability, control, and minimal dependencies.

The `anthropic` SDK's `base_url` parameter routes seamlessly through the gateway proxy, preserving the existing credential injection and transcript capture without changes. The OpenAI-compatible provider is straightforward — `/v1/chat/completions` with `tool_choice` is a well-documented, stable API.

### Key Design Decisions

1. **Tool system**: Each tool as a Python class with `definition()` (JSON schema) and `execute()` methods. Register in a `ToolRegistry`. The Bash tool wraps subprocess with timeout; Read/Write/Edit operate on the filesystem directly; Grep shells out to `rg`; Glob uses Python's `pathlib.glob`.

2. **Egg-native tools**: `EggOrch`, `EggContract`, `EggCheckpoint`, `GitOps`, `GhCli` call Python APIs directly instead of shelling out to CLIs. This reduces latency and token waste but requires importing the respective packages.

3. **Agent loop**: Standard `while turns < max_turns` loop — send messages to API, parse response, execute tool calls, append results, repeat until `stop_reason == "end_turn"` or max turns reached.

4. **Provider abstraction**: `Provider.send_message(messages, tools, system, model) → AsyncIterator[Event]` where `Event` is a union type covering text deltas, tool use, stop signals, and usage metadata.

5. **Harness selection**: `EGG_HARNESS=egg|claude-sdk|claude-code` env var, defaulting to `claude-sdk` initially (switchable to `egg` once validated).

## Complexity Assessment

**High**. This is a new subsystem (`shared/egg_harness/`) with 15+ files across provider abstraction, tool implementations, agent loop, config, prompt loading, and CLI entry point. It touches the most critical execution path in the system and requires careful parallel validation. Multiple independent work streams (provider clients, tool implementations, agent loop, CLI, interactive mode) could be parallelized.

## Open Questions

All questions are registered as HITL decisions via the orchestrator API. Each must be resolved before implementation planning.

### Decisions (multiple-choice)

**decision-1**: Which LLM client approach should the harness use for the Anthropic provider?
- [ ] anthropic Python SDK (handles streaming, types, retries)
- [ ] Raw httpx (full control, no vendor SDK)
- [ ] LiteLLM (multi-provider abstraction, heavy dependency)
- [ ] Other (explain in reply)

**decision-2**: Should the MVP include the interactive terminal mode, or should it focus solely on headless (pipeline) mode first?
- [ ] Headless only for MVP (interactive later)
- [ ] Both headless and interactive in MVP
- [ ] Other (explain in reply)

**decision-3**: Should egg-native tools (EggOrch, EggContract, EggCheckpoint, GitOps, GhCli) be included in the MVP, or should the harness shell out to CLIs like the current Agent SDK does?
- [ ] Shell out to CLIs initially (lower risk, faster MVP)
- [ ] Native Python tool implementations from the start
- [ ] Other (explain in reply)

**decision-4**: What should the default harness be during the transition period?
- [ ] Default to claude-sdk, opt-in to egg harness
- [ ] Default to egg harness, opt-out to claude-sdk
- [ ] Other (explain in reply)

### Open-ended Questions (feedback)

1. **Tool parity scope**: The issue lists NotebookEdit, WebFetch, and WebSearch as MVP tools. Are any of these actually optional for the MVP? Are there other Claude Code tools (e.g., TodoWrite, Agent tool for sub-agents) that agents rely on that aren't listed?

2. **Gateway transcript capture**: Should transcript capture move from the gateway proxy to the harness, or should both locations capture (for redundancy)? Moving it to the harness would simplify the gateway but add complexity to the harness.

3. **Token budget / cost tracking**: The current Agent SDK provides `total_cost_usd` in `ResultMessage`. With direct API calls, the harness must calculate cost from usage tokens. Should cost tracking be exact (using Anthropic's pricing API) or approximate (hardcoded per-model rates)?

4. **Error recovery semantics**: The consensus wrapper currently restarts agents on clean exit (up to 2 restarts) and implements exponential backoff for transient crashes. Should the harness internalize any of this retry logic, or should it remain external in the consensus wrapper?

5. **CLAUDE.md loading**: The issue says to "load and combine rule files from `~/.claude/CLAUDE.md` and project-level `CLAUDE.md`." The current setup also merges rules from `sandbox/agent-config/rules/` into the system prompt. Should the harness replicate this exact rule-merging behavior, or should it use a simpler single-file system prompt?

6. **Model alias mapping**: The issue proposes `opus`, `sonnet`, `haiku` aliases for Anthropic and pass-through for OpenAI-compatible. The current code uses `opus[1m]` (with max token suffix). Should the harness support the `[1m]` suffix syntax, or should max tokens be configured separately?

7. **Parallel validation plan**: The issue mentions "run both in parallel on test pipelines, compare results." What metrics should be compared? (cost, turns, wall time, task success rate, tool call patterns?) Is there an existing test pipeline that can be used for A/B comparison?

8. **Dependency on #1571**: The issue references #1571 (open-model hierarchy). Does anything in this MVP need to account for multi-model routing within a single pipeline, or is that purely a post-MVP concern?

---

*Authored-by: egg*
