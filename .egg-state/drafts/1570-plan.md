# Plan: Build Custom Coding Harness with Multi-Provider Support

> Issue: #1570 | Phase: plan | Pipeline: issue-1570-v3

## Summary

This plan decomposes the custom coding harness into a single PR organized across
10 implementation phases. The harness replaces egg's dependency on the Claude
Agent SDK and Claude Code CLI with an owned runtime that supports multiple LLM
providers, context management with compaction, session persistence, and an event
system — while keeping the existing runtimes as supported alternatives.

The work is structured as two new packages: `shared/egg_harness/` (core,
extractable, no egg imports) and `shared/egg_harness_integration/` (egg-specific
wiring). The core harness uses the Anthropic Python SDK for Anthropic providers
and raw httpx for OpenAI-compatible endpoints (Approach D from v2 architect
analysis). All 12 v1 HITL decisions are binding; 6 additional v3 decisions are
pending human input and the plan notes default recommendations for each.

Phases are ordered by dependency: foundation types → providers → tools → agent
loop → context management → prompt/permissions → client/CLI → egg integration →
harness selection → tests. Each phase is a logical commit boundary within the
single PR.

## Architecture Analysis Summary

The v2 architect analysis (ACKed) and v3 refine analysis established:

- **Approach D (Hybrid)**: Anthropic SDK for Anthropic streaming (8+ SSE event
  types), raw httpx for OpenAI-compatible endpoints (simpler SSE format).
- **7 subsystems**: Providers, Tools, Agent Loop, Context Management, Session
  Persistence, Event System, Config/CLI.
- **Two packages**: Core harness (extractable) + egg integration layer.
- **Compaction**: Threshold-based (80% of model max), summarize + clear aligned
  with Pi, anchor persistence per #1032.
- **Harness selection**: `EGG_HARNESS` env var routes to `egg` (new), `claude-sdk`
  (current default), or `claude-code` (interactive).

## Risk Assessment Summary

The v2 risk analyst identified 12 risks (3 CRITICAL, 4 HIGH). Key mitigations:

- **RISK-1 (Credential handling)**: All API calls through gateway. Startup
  assertion that `ANTHROPIC_API_KEY` NOT in `os.environ`. Never `shell=true`.
- **RISK-2 (Tool behavioral parity)**: Compliance test suite comparing outputs.
  Process group management for Bash. Exact-match Edit semantics.
- **RISK-3 (Agent loop reliability)**: Strict turn counting + token budget +
  circuit breaker. Handle all `stop_reason` values. SIGTERM with 30s grace.

Five mandatory human review gates: credential flow, agent loop, tool parity,
system prompt assembly, and parallel validation results.

## Pending HITL Decisions (v3 Refine)

Six decisions from the v3 refine phase are pending human input. The plan uses
these defaults (will be updated when decisions are resolved):

| Decision | Question | Default |
|----------|----------|---------|
| decision-1 | Compaction summarization model | Same model as conversation (simpler, Pi's approach) |
| decision-2 | Session storage location | Container filesystem (`/tmp/egg-sessions/`) for MVP |
| decision-3 | HTML-to-markdown library for WebFetch | markdownify (lightweight, pure Python) |
| decision-4 | Interactive mode scope for MVP | Minimal readline (basic I/O, meets HITL-2) |
| decision-5 | settings.json property handling | Both (settings.json fallback, HarnessConfig precedence) |
| decision-6 | OpenAI-compatible minimum API surface | Explicit config per endpoint (declare features) |

## Design Decisions

| ID | Decision | Rationale |
|----|----------|-----------|
| DD-1 | Anthropic SDK for Anthropic, httpx for OpenAI-compatible | v2 architect Approach D. SDK handles 8+ SSE event types reliably. |
| DD-2 | `shared/egg_harness/` as isolated extractable package | Modularity requirement from issue. No imports from orchestrator/, gateway/, sandbox/. |
| DD-3 | Shell out to CLIs for egg-native tools initially | HITL-3. Lower risk, faster MVP. |
| DD-4 | `AsyncIterator[StreamEvent]` for provider interface | v2 architect TD-4. Composable stream processing via async generators. |
| DD-5 | Replicate exact CLAUDE.md rule-merging | HITL-9. Simplifying risks behavioral differences in parallel validation. |
| DD-6 | Keep transcript capture in gateway | HITL-6. Gateway already captures; no duplication needed. |
| DD-7 | Default `claude-sdk`, opt-in `egg` harness | HITL-4. Safe transition; egg harness must prove parity first. |
| DD-8 | Hardcoded per-model cost rates | HITL-7. Anthropic SDK returns token counts; multiply by known rates. |
| DD-9 | Pin anthropic SDK `>=0.50,<1.0` | Risk analyst recommendation. Current latest is 0.79.0. |
| DD-10 | Model alias `haiku` maps to `claude-haiku-4-5` | Haiku 3 retires 2026-04-19. Must use current model. |

## Implementation Phases

### Phase 1: Foundation & Types

**Goal**: Create the `egg_harness` package skeleton with all shared types,
configuration, and cost tracking. This is the foundation all other phases depend on.

**Tasks**:

- **[TASK-1-1]** Create `shared/egg_harness/` package with `pyproject.toml`,
  `__init__.py`. Define `StreamEvent` union type in `providers/base.py`:
  `TextDelta`, `ToolUseStart`, `ToolUseInputDelta`, `ToolUseEnd`,
  `ThinkingDelta`, `MessageStart`, `MessageDelta`, `MessageEnd`. Define
  `Provider` abstract base class with
  `send_message(messages, tools, system, model) -> AsyncIterator[StreamEvent]`.
  - **Acceptance**: Package is importable. `from egg_harness.providers.base import Provider, StreamEvent` works. All 8 event dataclasses exist with documented fields. Provider ABC has abstract `send_message` method.

- **[TASK-1-2]** Implement `config.py`: `ProviderConfig` (provider type, model,
  endpoint, api_key_env), `HarnessConfig` (max_turns, timeout, cwd, env,
  disallowed_tools, intercept_tools, compaction_threshold, keep_recent_tokens).
  Model alias resolution (`opus` → `claude-opus-4-6`, `sonnet` →
  `claude-sonnet-4-5-20250514`, `haiku` → `claude-haiku-4-5`). `opus[1m]` suffix
  parser per HITL-10. Model context window lookup table.
  - **Acceptance**: `resolve_model("opus")` returns `"claude-opus-4-6"`. `parse_model_spec("opus[1m]")` returns model + max_tokens=1000000. `get_context_window("claude-opus-4-6")` returns correct token limit. HarnessConfig defaults match current AgentResult behavior.

- **[TASK-1-3]** Implement `cost.py`: Hardcoded per-model token rates (input,
  output, cache read/write). `CostTracker` class that accumulates token usage
  per turn and computes total cost. Rate table for all current Claude models
  and a generic fallback for OpenAI-compatible (no cost tracking).
  - **Acceptance**: `CostTracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")` computes correct USD cost. Rates match published Anthropic pricing.

- **[TASK-1-4]** Implement `result.py`: `AgentResult` dataclass matching existing
  `egg_agent.result.AgentResult` fields plus `compaction_count: int | None`.
  - **Acceptance**: All existing fields preserved (success, stdout, stderr, returncode, error, metadata, cost_usd, num_turns, duration_ms, session_id). New `compaction_count` field added. Backward-compatible with existing consumers.

- **[TASK-1-5]** Implement `events.py`: `EventBus` class with typed callback
  registration: `on_output(text)`, `on_tool_call(name, input)`,
  `on_tool_result(name, output)`, `on_compaction(summary, tokens_before,
  tokens_after)`, `on_error(error)`, `on_turn_complete(turn_number, usage)`.
  Callbacks are composable (multiple registrations per event type).
  - **Acceptance**: EventBus supports registering multiple callbacks per event. `bus.emit_output("text")` calls all registered `on_output` callbacks. Callbacks that raise exceptions are caught and logged, not propagated.

### Phase 2: Provider Layer

**Goal**: Implement the Anthropic provider (using the SDK) and the
OpenAI-compatible provider (using httpx). Both implement the `Provider` interface
from Phase 1.

**Tasks**:

- **[TASK-2-1]** Implement `providers/anthropic.py`: `AnthropicProvider` using
  `anthropic.AsyncAnthropic(base_url=...)`. Map `StreamEvent` types to SDK stream
  events. Support system prompt injection, tool definitions, model selection.
  Handle cache control headers (`anthropic-beta: prompt-caching`). Support
  extended thinking passthrough. Gateway URL validation at init (CVE-2026-21852
  mitigation). Startup assertion that `ANTHROPIC_API_KEY` is NOT in `os.environ`.
  - **Acceptance**: Provider streams responses from Anthropic API via gateway proxy. TextDelta events yield text chunks. ToolUseStart/End events bracket tool calls with JSON input. Gateway URL is validated against expected pattern. Test with mock HTTP responses.

- **[TASK-2-2]** Implement `providers/openai_compat.py`:
  `OpenAICompatibleProvider` using raw `httpx.AsyncClient` for SSE streaming.
  Parse `/v1/chat/completions` streaming format. Map OpenAI tool_call chunks to
  `StreamEvent` types. Support explicit endpoint config and capability
  declaration (tool_choice support, streaming support, system message support).
  Model passed as-is (no alias mapping).
  - **Acceptance**: Provider streams responses from OpenAI-compatible endpoint. SSE chunks correctly accumulated into TextDelta and ToolUse events. Endpoint capabilities respected (graceful degradation if tool_choice unsupported). Test with mock SSE stream.

- **[TASK-2-3]** Add retry logic with exponential backoff to both providers per
  HITL-8: retry on 429 (rate limit), 5xx (server error), connection reset.
  Do not retry 4xx (except 429). Jitter on backoff. Max 3 retries.
  Circuit breaker: 3 consecutive non-retryable failures → raise immediately.
  - **Acceptance**: Transient 429/5xx errors retried with exponential backoff. 4xx errors (except 429) raised immediately. Circuit breaker trips after 3 consecutive failures. Backoff includes jitter.

### Phase 3: Tool System

**Goal**: Implement the tool registry and all 8 standard tools that match
Claude Code's behavior.

**Tasks**:

- **[TASK-3-1]** Implement `tools/registry.py`: `ToolRegistry` with
  `register(tool_def, handler)`, `execute(name, input) -> ToolResult`,
  `get_definitions() -> list[ToolDefinition]`. Permission callback interface:
  `can_use_tool(name, input) -> str | None` (returns error string if blocked,
  None if allowed). Apply permission check before execution. Output truncation
  for large tool results (configurable max, default 100KB).
  - **Acceptance**: Tools can be registered and executed by name. Permission callback invoked before execution; blocked tools return error result. Large outputs truncated with message indicating truncation. Unknown tool names return error result.

- **[TASK-3-2]** Implement `tools/bash.py`: Shell command execution with
  configurable timeout (default 120s). Process group management via
  `os.setpgrp()`/`os.killpg()` for reliable timeout cleanup. Working directory
  support. Output capture (stdout + stderr). NEVER use `shell=true` in
  subprocess calls (CVE-2026-35022 mitigation). Use `["bash", "-c", command]`
  pattern.
  - **Acceptance**: Commands execute in specified working directory. Timeout kills process group, not just parent. Output captured and returned. Exit code preserved. No `shell=true` anywhere in implementation.

- **[TASK-3-3]** Implement `tools/read.py`: File reading with offset/limit
  support. Line number output (cat -n format). Binary file detection (return
  error for binary). Image file passthrough (return base64 for multimodal).
  PDF support with page range parameter. Symlink resolution. Encoding detection
  with UTF-8 default.
  - **Acceptance**: Files read with correct line numbers starting at 1. Offset/limit parameters work correctly. Binary files detected and rejected with error. Symlinks resolved. Non-existent files return clear error.

- **[TASK-3-4]** Implement `tools/write.py` and `tools/edit.py`: Write creates
  or overwrites files. Edit does exact string replacement (old_string →
  new_string). Edit fails if old_string is not found or is not unique (unless
  `replace_all=true`). Both create parent directories as needed. Preserve file
  permissions on edit. Newline handling (preserve original line endings).
  - **Acceptance**: Write creates new files and overwrites existing. Edit replaces exact strings. Non-unique old_string in Edit returns error with count of matches. replace_all replaces all occurrences. Parent directories created automatically.

- **[TASK-3-5]** Implement `tools/glob_tool.py` and `tools/grep.py`: Glob uses
  `pathlib.Path.glob()` or the `fd` binary for file pattern matching. Results
  sorted by modification time. Grep uses the `rg` (ripgrep) binary for content
  search. Support regex patterns, file type filtering, context lines,
  output modes (content, files_with_matches, count), head_limit.
  - **Acceptance**: Glob matches files by pattern and returns sorted paths. Grep finds content matching regex. File type filter works. Context lines (-A, -B, -C) work. Head limit caps output. Both handle large directories without hanging.

- **[TASK-3-6]** Implement `tools/web_fetch.py` and `tools/web_search.py`:
  WebFetch downloads URL content, converts HTML to markdown (using markdownify
  or configured library per decision-3), processes with prompt. WebSearch
  performs web search query. Both conditionally disabled in private mode
  (check `EGG_NETWORK_MODE` env var). Both tools are stubs that shell out to
  external utilities or use httpx directly, matching Claude Code's tool schemas.
  - **Acceptance**: WebFetch downloads and converts HTML to markdown. WebSearch returns search results. Both disabled in private mode (return error explaining why). Tool JSON schemas match Claude Code's definitions.

### Phase 4: Agent Loop Core

**Goal**: Implement the core agentic loop that replaces `claude_agent_sdk.query()`.
This is the central component that ties providers and tools together.

**Tasks**:

- **[TASK-4-1]** Implement `loop.py`: Core `AgentLoop` class with `run(prompt,
  system_prompt, messages) -> AgentResult`. Loop logic: build messages → call
  provider.send_message() → consume StreamEvents → accumulate text + tool calls
  → execute tools via ToolRegistry → append tool results → repeat. Sequential
  tool execution within a turn (matching Claude Code behavior). Emit events via
  EventBus (on_output for text, on_tool_call/result for tools, on_turn_complete).
  - **Acceptance**: Loop executes multiple turns of tool use. Text streamed via on_output callback. Tool calls executed and results fed back. Loop terminates on `end_turn` stop_reason or when model produces no tool calls.

- **[TASK-4-2]** Add turn limits, timeout, and stop_reason handling to
  `AgentLoop`. Max turns limit (configurable, default 200). Wall-clock timeout
  (configurable, default 7200s). Handle all stop_reasons explicitly: `end_turn`
  (normal completion), `max_tokens` (response truncated — continue), `stop_sequence`
  (treat as end_turn), `tool_use` (execute tools, continue). Circuit breaker:
  3 consecutive tool execution failures → abort.
  - **Acceptance**: Loop stops at max_turns with appropriate result. Timeout triggers graceful stop. All stop_reasons produce correct behavior. Circuit breaker fires after 3 consecutive tool failures.

- **[TASK-4-3]** Add SIGTERM graceful shutdown to `AgentLoop`. Register signal
  handler. On SIGTERM: set shutdown flag, let current tool finish (up to 30s
  grace), return partial AgentResult with success=False. Kill any running
  subprocess tool processes via process group.
  - **Acceptance**: SIGTERM during tool execution waits for tool to finish (up to 30s), then returns partial result. SIGTERM during API call cancels the call and returns. No orphaned subprocesses after shutdown.

### Phase 5: Context Management & Session Persistence

**Goal**: Implement token tracking, compaction, and session persistence for
long-running agents and consensus wrapper restarts.

**Tasks**:

- **[TASK-5-1]** Implement `compaction.py`: Token budget tracking using API
  response `usage` field. Calculate total context size per turn (system prompt
  + conversation history + tool definitions). Compaction trigger when tokens
  exceed configurable threshold (default 80% of model context window).
  Compaction strategy: walk backwards from newest message to find cut point
  (accumulate `keep_recent_tokens`, default 20,000), never cut between tool
  call and result. Generate structured summary of older messages (goal,
  progress, decisions, files modified, errors encountered). Clear history and
  inject summary as new conversation start.
  - **Acceptance**: Token tracking accurate per turn. Compaction triggers at correct threshold. Cut point never splits tool call/result pairs. Summary captures key conversation context. Post-compaction conversation starts with summary + retained recent messages.

- **[TASK-5-2]** Add compaction loop protection and manual trigger.
  Loop protection: if compaction fires twice within N turns (configurable,
  default 3), abort with error rather than infinite compaction loop. Manual
  compaction via explicit method call (`loop.compact_now()`) for agents that
  want to compact at natural checkpoints. Emit `on_compaction` event.
  - **Acceptance**: Double compaction within N turns raises error. Manual compaction works and emits event. Compaction count tracked in AgentResult.

- **[TASK-5-3]** Implement `session.py`: JSONL serialization of conversation
  state. Session metadata: session_id, model, total_cost, turn_count,
  duration_ms, compaction_count, created_at, updated_at. Auto-save at
  configurable intervals or on compaction. Resume from file: load messages +
  metadata, reconstruct conversation state. Storage location configurable
  (default `/tmp/egg-sessions/` per decision-2 default).
  - **Acceptance**: Session saves to JSONL with all metadata. Resume from file reconstructs conversation state. Auto-save triggers on compaction and at configured intervals. Session ID is stable across saves.

### Phase 6: System Prompt & Permissions

**Goal**: Implement system prompt assembly and the permission/interception layer.

**Tasks**:

- **[TASK-6-1]** Implement `prompt.py`: Generic system prompt assembly that
  accepts a list of prompt sources (strings or callables that return strings).
  Concatenates with `---` separators (matching existing CLAUDE.md convention).
  No egg-specific logic in core — prompt sources injected by integration layer.
  - **Acceptance**: `build_system_prompt([source1, source2])` concatenates with separators. Callable sources invoked at build time. Empty sources skipped.

- **[TASK-6-2]** Implement permission callback wiring in `AgentLoop`. Before
  each tool execution, invoke `can_use_tool(name, input)` callback from
  ToolRegistry. If callback returns error string, skip execution and return
  error as tool result. Support tool disallow list (disallowed_tools config).
  - **Acceptance**: Disallowed tools return error without execution. Permission callback blocks specific tool invocations. Error messages match format expected by LLM (clear explanation of why tool was blocked).

### Phase 7: Client, CLI & Interactive Mode

**Goal**: Create the high-level client API, CLI entry point, and interactive mode
that serve as drop-in replacements for existing entry points.

**Tasks**:

- **[TASK-7-1]** Implement `client.py`: `run_agent_async(prompt, model, max_turns,
  system_prompt, cwd, timeout, on_output, env, intercept_tools) -> AgentResult`.
  Interface matches `egg_agent.client.run_agent_async()` signature. Assembles
  HarnessConfig + ProviderConfig from parameters, creates Provider + ToolRegistry
  + EventBus + AgentLoop, executes, returns AgentResult. Add synchronous
  `run_agent()` wrapper.
  - **Acceptance**: `run_agent_async()` has identical signature to `egg_agent.client.run_agent_async()`. Returns AgentResult with all fields populated. on_output callback receives streaming text.

- **[TASK-7-2]** Implement `__main__.py`: CLI entry point matching `egg_agent`
  CLI args: `--model`, `--max-turns`, `--system-prompt`, `--timeout`. Read
  prompt from positional arg or stdin. Stream output to stdout. Return exit
  code from AgentResult.returncode. Invoked as `python3 -m egg_harness`.
  - **Acceptance**: `python3 -m egg_harness --model opus --max-turns 200 "prompt"` works. Stdin prompt reading works when no positional arg. Output streams to stdout. Exit code matches AgentResult.returncode.

- **[TASK-7-3]** Implement `interactive.py`: Multi-turn terminal REPL using
  minimal readline (per decision-4 default). Read user input, stream response,
  repeat. Ctrl-C interrupts current generation, Ctrl-D exits. Same tool set
  and provider configuration as headless mode. Invoked as
  `python3 -m egg_harness --interactive`.
  - **Acceptance**: Interactive REPL reads input, streams response, loops. Ctrl-C interrupts cleanly. Ctrl-D exits. Multi-turn conversation maintains context. Tools available during interactive session.

### Phase 8: Egg Integration Layer

**Goal**: Create the egg-specific integration layer that wires egg's tools,
permissions, prompt assembly, and compaction into the core harness.

**Tasks**:

- **[TASK-8-1]** Create `shared/egg_harness_integration/` package. Implement
  `egg_tools.py`: Register 5 egg-native tools via ToolRegistry (EggOrch,
  EggContract, EggCheckpoint, GitOps, GhCli). Each tool shells out to the
  corresponding CLI (per HITL-3). Tool definitions include JSON schemas
  matching the CLI interfaces.
  - **Acceptance**: All 5 egg-native tools registered in ToolRegistry. EggOrch tool executes `egg-orch` CLI commands. GitOps tool executes `git` commands. GhCli tool executes `gh` commands. Tools return CLI output as tool results.

- **[TASK-8-2]** Implement `egg_prompt.py`: CLAUDE.md rule-merging that
  replicates the exact behavior of `sandbox/entrypoint.py:setup_agent_rules()`.
  Load rule files from configured directory (default
  `sandbox/agent-config/rules/`), concatenate with `---` separators in the
  correct order. Also load project-level CLAUDE.md if present.
  Implement settings.json property parsing per decision-5 default (settings.json
  as fallback, HarnessConfig takes precedence).
  - **Acceptance**: Rule assembly output matches `setup_agent_rules()` output byte-for-byte for the same input files. Project-level CLAUDE.md loaded when present. settings.json properties applied as defaults.

- **[TASK-8-3]** Implement `egg_permissions.py`: Adapter wrapping
  `egg_restrictions.check_agent_file_access()` as a `can_use_tool` callback.
  Reads `EGG_AGENT_ROLE` from environment. Blocks Write/Edit/NotebookEdit
  to out-of-scope paths. Returns descriptive error messages identifying the
  owning role (matching existing `tool_interceptor.py` behavior).
  - **Acceptance**: Permission callback blocks file writes outside role boundaries. Error messages match existing tool_interceptor.py format. Roles correctly loaded from EGG_AGENT_ROLE. Non-write tools always allowed.

- **[TASK-8-4]** Implement `egg_compaction.py`: Anchor-based compaction
  integration for #1032. On compaction, persist state to
  `.egg-state/agent-anchors/<agent-id>.json` using existing `egg_anchor`
  package. Post-compaction recovery: read anchor + poll message bus for missed
  BRC messages. Emit `on_compaction` event for monitoring.
  - **Acceptance**: Compaction persists anchor file with correct schema. Post-compaction recovery reads anchor. Message bus polled for missed messages. Existing egg_anchor models and validator used.

- **[TASK-8-5]** Implement `harness_factory.py`: Factory function
  `create_egg_harness(model, max_turns, system_prompt, ...) -> AgentLoop`
  that wires all egg integrations: creates ProviderConfig (routed through
  gateway), registers standard + egg-native tools, configures CLAUDE.md prompt
  assembly, hooks permission callback, configures compaction with anchor
  integration. This is the single entry point for egg's use of the harness.
  - **Acceptance**: Factory creates fully-configured AgentLoop with all egg integrations. Provider routes through gateway URL. Egg-native tools registered. Permissions enforced. System prompt includes CLAUDE.md rules.

### Phase 9: Harness Selection & Wiring

**Goal**: Add harness selection to the existing egg_agent module so consumers
can route to either the new harness or the existing Claude SDK.

**Tasks**:

- **[TASK-9-1]** Update `shared/egg_agent/client.py`: Add `EGG_HARNESS` env var
  detection. When `EGG_HARNESS=egg`, route `run_agent_async()` to
  `egg_harness_integration.harness_factory` instead of `claude_agent_sdk.query()`.
  When `EGG_HARNESS=claude-sdk` or unset, use existing Claude SDK path (default
  per HITL-4). TOS warning log when subscription users select egg harness.
  - **Acceptance**: `EGG_HARNESS=egg` routes to new harness. `EGG_HARNESS=claude-sdk` or unset uses existing SDK path. Both paths return AgentResult with same fields. Existing consumers (consensus_wrapper, babysit) need zero changes.

- **[TASK-9-2]** Update `shared/egg_agent/command.py`: Route
  `build_agent_command()` to `python3 -m egg_harness` when `EGG_HARNESS=egg`.
  Propagate `EGG_HARNESS` env var to child agent processes to ensure consistent
  harness selection across agent spawning chains.
  - **Acceptance**: `build_agent_command()` returns egg_harness module when EGG_HARNESS=egg. Child agents inherit harness selection. Existing callers (consensus_wrapper, babysit) work without changes.

- **[TASK-9-3]** Update `sandbox/entrypoint.py`: Add `EGG_HARNESS` to the
  environment setup. When `EGG_HARNESS=egg` and interactive mode requested,
  launch `python3 -m egg_harness --interactive` instead of
  `claude --dangerously-skip-permissions`. Set `ANTHROPIC_BASE_URL` to gateway
  URL for the harness provider. Add startup validation per RISK-1 mitigations.
  - **Acceptance**: Entrypoint respects EGG_HARNESS for interactive mode. Gateway URL configured correctly. ANTHROPIC_API_KEY absence validated at startup. Existing claude-code path unchanged when EGG_HARNESS unset.

- **[TASK-9-4]** Update `shared/pyproject.toml`: Add `egg_harness` and
  `egg_harness_integration` to the package discovery list. Add
  `anthropic>=0.50,<1.0` and `markdownify` (or configured HTML library) to
  dependencies.
  - **Acceptance**: Both new packages discoverable via setuptools. Dependencies installable. `pip install -e shared/` includes all new packages.

### Phase 10: Tests

**Goal**: Comprehensive test coverage for all harness subsystems, integration
tests for the end-to-end flow, and compliance tests for tool behavioral parity.

**Tasks**:

- **[TASK-10-1]** Unit tests for config, cost tracking, events, and result types.
  Test model alias resolution, opus[1m] parsing, cost calculation accuracy,
  EventBus callback registration and emission, AgentResult field compatibility.
  - **Acceptance**: All config edge cases tested (unknown model, malformed suffix, empty input). Cost rates match published pricing. EventBus handles errors in callbacks. Tests pass.

- **[TASK-10-2]** Unit tests for providers. Mock HTTP responses for both
  Anthropic and OpenAI-compatible providers. Test streaming event parsing,
  error handling, retry logic, circuit breaker, gateway URL validation.
  - **Acceptance**: Both providers correctly parse mocked stream responses. Retry logic tested with simulated transient errors. Circuit breaker trips correctly. Gateway URL validation rejects unexpected URLs.

- **[TASK-10-3]** Unit tests for tool registry and standard tools. Test
  permission callback blocking, output truncation, tool execution with mock
  filesystem. Test Bash timeout with process group cleanup. Test Edit
  exact-match semantics. Test Grep with mock ripgrep output.
  - **Acceptance**: Permission blocking prevents execution. Truncation works at configured limit. Bash timeout kills process group. Edit non-unique string returns error. All tools handle error conditions gracefully.

- **[TASK-10-4]** Unit tests for agent loop. Test multi-turn execution with
  mock provider. Test max_turns limit, timeout, stop_reason handling,
  circuit breaker, SIGTERM shutdown. Test compaction trigger and execution.
  Test session save/resume.
  - **Acceptance**: Loop executes correct number of turns with mock. All stop_reasons handled. Timeout and SIGTERM produce clean results. Compaction triggers at threshold and produces valid summary. Session round-trips correctly.

- **[TASK-10-5]** Integration tests for harness end-to-end. Test
  `run_agent_async()` with a mock Anthropic endpoint (local HTTP server that
  returns canned responses). Verify tool execution, streaming output,
  result metadata. Test harness selection routing (EGG_HARNESS env var).
  - **Acceptance**: End-to-end test executes prompt → tool use → result cycle. Streaming output captured. Cost and metadata populated. Harness selection routes correctly.

- **[TASK-10-6]** Tool behavioral parity compliance tests. For each standard
  tool, run identical inputs through both Claude Code (via SDK) and the new
  harness implementation, compare outputs. Focus on edge cases: Edit with
  non-unique strings, Read with binary files, Bash with timeout, Grep with
  large directories. These tests document known behavioral differences.
  - **Acceptance**: Compliance test suite exists with at least 5 test cases per tool. Known differences documented. Critical differences (Edit semantics, Bash timeout behavior) have exact parity.

## Test Strategy

### Automated Tests

- **Unit tests** (Phase 10, TASK-10-1 through TASK-10-4): Cover all subsystems
  with mocked dependencies. Target: every module in `egg_harness/` has
  corresponding test file. Run via `pytest shared/egg_harness/tests/`.
- **Integration tests** (Phase 10, TASK-10-5): End-to-end with mock HTTP server.
  Run via `pytest shared/egg_harness/tests/integration/`.
- **Compliance tests** (Phase 10, TASK-10-6): Tool parity comparison. Run via
  `pytest shared/egg_harness/tests/compliance/`.
- **Existing tests**: `make test` must continue to pass — no regressions in
  existing egg_agent, orchestrator, or gateway tests.

### Manual Verification

- **Security review**: Verify credential flow — ANTHROPIC_API_KEY never in
  container environment, all API calls route through gateway.
- **Parallel validation**: Run harness on 10+ test pipelines, compare
  cost_usd, num_turns, duration_ms, task success rate against Claude SDK.
- **Interactive mode**: Manual test of REPL — multi-turn conversation, tool
  use, Ctrl-C interrupt, Ctrl-D exit.
- **System prompt parity**: Compare assembled system prompt output between
  harness rule-merging and existing `setup_agent_rules()` for sample runs.

## Dependency Ordering

```
Phase 1 (Foundation) ──┬── Phase 2 (Providers) ──┐
                       ├── Phase 3 (Tools) ───────┤
                       └── Phase 6 (Prompt/Perms) ┤
                                                   ├── Phase 4 (Agent Loop) ── Phase 5 (Context Mgmt)
                                                   │
                                                   └── Phase 7 (Client/CLI) ── Phase 8 (Egg Integration)
                                                                                         │
                                                                               Phase 9 (Harness Selection)
                                                                                         │
                                                                               Phase 10 (Tests)
```

Phases 2, 3, and 6 can be worked in parallel after Phase 1 completes.
Phase 4 requires Phases 2 and 3.
Phase 7 requires Phase 4.
Phase 10 can start partially after Phase 4 (unit tests) but compliance tests
need Phase 9.

## Security Considerations

Per v2 risk assessment mandatory mitigations:

1. **Zero-credential sandbox**: Startup assertion that `ANTHROPIC_API_KEY` NOT in
   `os.environ` (TASK-2-1). All API calls via gateway proxy (TASK-2-1, TASK-8-5).
2. **CVE-2026-35022**: No `shell=true` in any subprocess call (TASK-3-2). CI
   linter rule should be added separately.
3. **CVE-2026-21852**: Validate `ANTHROPIC_BASE_URL` / gateway URL at provider
   init (TASK-2-1).
4. **Tool interception**: Port existing `tool_interceptor.py` logic via injection
   (TASK-8-3).
5. **Defense-in-depth**: Tool filtering at both harness (permission callback) and
   gateway (private mode tool stripping) levels.

## Rollback Plan

If the harness proves unreliable during parallel validation:

1. Set `EGG_HARNESS=claude-sdk` (or unset) — immediate rollback to existing
   Claude SDK path.
2. No code changes needed — harness selection is runtime configuration.
3. The harness code remains in the codebase but is not active.
4. Claude Code CLI and Agent SDK remain installed in container images.

```yaml
# yaml-tasks
pr:
  title: "Add custom coding harness with multi-provider LLM support"
  description: |
    Egg currently depends on the Claude Agent SDK and Claude Code CLI as its
    agent runtimes — both are opaque, evolve on Anthropic's release schedule,
    and have been targets of recent critical CVEs (CVE-2026-35022, CVE-2025-59536).
    Context window exhaustion is handled opaquely with no integration into egg's
    anchor mechanism. Neither runtime supports non-Anthropic models.

    This PR introduces a custom coding harness (`shared/egg_harness/`) as an
    opt-in alternative runtime, organized into two packages:

    1. **Core harness** (`egg_harness/`): Provider-abstracted LLM client
       supporting Anthropic (via SDK) and OpenAI-compatible endpoints (via httpx),
       8 standard tools (Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch),
       a core agent loop with streaming, context management with threshold-based
       compaction aligned with Pi's approach, JSONL session persistence for
       consensus wrapper restarts, and an event system for monitoring.
    2. **Egg integration layer** (`egg_harness_integration/`): Egg-native tools
       (EggOrch, EggContract, EggCheckpoint, GitOps, GhCli) shelling out to CLIs,
       CLAUDE.md rule-merging replicating exact existing behavior, role-based
       permission enforcement via egg_restrictions, anchor-based compaction
       integration (#1032), and a factory function wiring everything together.

    Harness selection is controlled by the `EGG_HARNESS` environment variable:
    `egg` (new harness), `claude-sdk` (current default), or `claude-code`
    (interactive CLI). The new harness is opt-in during the transition period —
    it must prove parity on parallel validation before becoming the default.
    Existing consumers (consensus_wrapper, babysit agents) require zero changes.

    Key security mitigations: all API calls routed through gateway (zero-credential
    sandbox preserved), startup assertion that ANTHROPIC_API_KEY is absent,
    gateway URL validation (CVE-2026-21852), no shell=true in subprocess calls
    (CVE-2026-35022), and ported role-based tool interception.
  test_plan: |
    - Automated: Unit tests for all harness subsystems (config, providers, tools,
      loop, compaction, session, events, client) in shared/egg_harness/tests/.
      Integration test with mock HTTP endpoint for end-to-end agent loop.
      Tool compliance tests comparing harness vs Claude Code tool outputs.
      Existing make test suite must continue to pass (no regressions).
    - Manual: Security review of credential flow (gateway routing, no API key
      in container). Parallel validation on 10+ test pipelines comparing
      cost_usd, num_turns, duration_ms, and task success rate between harness
      and Claude SDK. Interactive mode manual test (multi-turn, tool use,
      Ctrl-C/Ctrl-D). System prompt parity check comparing harness rule-merging
      output against setup_agent_rules() for sample runs.
  manual_steps: |
    Pre-merge:
    - Resolve 6 pending HITL decisions from v3 refine phase (compaction model,
      session location, HTML library, interactive scope, settings.json handling,
      OpenAI API surface)
    - Security review of credential flow and agent loop implementation
    - Parallel validation on test pipelines must show metric parity
    Post-merge:
    - No immediate action required — harness defaults to disabled (claude-sdk)
    - To enable: set EGG_HARNESS=egg in pipeline config or container env
    - Monitor parallel validation metrics before switching default
phases:
  - id: 1
    name: Foundation & Types
    goal: Create egg_harness package skeleton with shared types, configuration, cost tracking, events, and result type
    tasks:
      - id: TASK-1-1
        description: Create shared/egg_harness/ package with pyproject.toml, __init__.py. Define StreamEvent union type and Provider ABC in providers/base.py
        acceptance: Package importable. All 8 StreamEvent dataclasses exist. Provider ABC has abstract send_message method returning AsyncIterator[StreamEvent].
        role: coder
        files:
          - shared/egg_harness/__init__.py
          - shared/egg_harness/pyproject.toml
          - shared/egg_harness/providers/__init__.py
          - shared/egg_harness/providers/base.py
      - id: TASK-1-2
        description: Implement config.py with ProviderConfig, HarnessConfig, model alias resolution (opus→claude-opus-4-6, haiku→claude-haiku-4-5), opus[1m] suffix parser, and context window lookup
        acceptance: resolve_model and parse_model_spec return correct values. HarnessConfig defaults match current behavior. haiku maps to claude-haiku-4-5 (not deprecated haiku-3).
        role: coder
        files:
          - shared/egg_harness/config.py
      - id: TASK-1-3
        description: Implement cost.py with hardcoded per-model token rates and CostTracker class that accumulates usage and computes total cost
        acceptance: CostTracker computes correct USD cost for known models. Rate table matches published Anthropic pricing.
        role: coder
        files:
          - shared/egg_harness/cost.py
      - id: TASK-1-4
        description: Implement result.py with AgentResult dataclass matching existing egg_agent.result.AgentResult plus compaction_count field
        acceptance: All existing AgentResult fields preserved. New compaction_count field present. Backward-compatible.
        role: coder
        files:
          - shared/egg_harness/result.py
      - id: TASK-1-5
        description: Implement events.py with EventBus class supporting typed callback registration (on_output, on_tool_call, on_tool_result, on_compaction, on_error, on_turn_complete)
        acceptance: Multiple callbacks per event type supported. Emit calls all registered callbacks. Callback exceptions caught and logged.
        role: coder
        files:
          - shared/egg_harness/events.py
  - id: 2
    name: Provider Layer
    goal: Implement Anthropic and OpenAI-compatible providers with retry logic
    tasks:
      - id: TASK-2-1
        description: Implement providers/anthropic.py using anthropic.AsyncAnthropic with gateway URL validation, ANTHROPIC_API_KEY absence assertion, streaming, cache control, and extended thinking support
        acceptance: Provider streams via gateway proxy. StreamEvents correctly mapped. Gateway URL validated. ANTHROPIC_API_KEY not in os.environ asserted at init.
        role: coder
        files:
          - shared/egg_harness/providers/anthropic.py
      - id: TASK-2-2
        description: Implement providers/openai_compat.py using raw httpx SSE for OpenAI-compatible endpoints with capability declaration config
        acceptance: Provider streams from OpenAI-compatible endpoint. SSE chunks parsed into StreamEvents. Capability config respected.
        role: coder
        files:
          - shared/egg_harness/providers/openai_compat.py
      - id: TASK-2-3
        description: Add exponential backoff retry (429, 5xx, connection reset) to both providers with jitter, max 3 retries, and circuit breaker (3 consecutive non-retryable failures)
        acceptance: Transient errors retried with backoff. 4xx (except 429) not retried. Circuit breaker trips after 3 consecutive failures.
        role: coder
        files:
          - shared/egg_harness/providers/anthropic.py
          - shared/egg_harness/providers/openai_compat.py
          - shared/egg_harness/providers/base.py
  - id: 3
    name: Tool System
    goal: Implement tool registry and all 8 standard tools matching Claude Code behavior
    tasks:
      - id: TASK-3-1
        description: Implement tools/registry.py with ToolRegistry (register, execute, get_definitions), permission callback interface, and output truncation
        acceptance: Tools registered and executed by name. Permission callback blocks tools. Output truncated at configurable limit. Unknown tools return error.
        role: coder
        files:
          - shared/egg_harness/tools/__init__.py
          - shared/egg_harness/tools/registry.py
      - id: TASK-3-2
        description: Implement tools/bash.py with subprocess execution, process group management for timeout, working directory support. No shell=true (CVE mitigation).
        acceptance: Commands execute in working directory. Timeout kills process group. No shell=true. Exit code preserved.
        role: coder
        files:
          - shared/egg_harness/tools/bash.py
      - id: TASK-3-3
        description: Implement tools/read.py with offset/limit, line numbers, binary detection, image passthrough, symlink resolution
        acceptance: Files read with correct line numbers. Offset/limit work. Binary files rejected. Non-existent files return error.
        role: coder
        files:
          - shared/egg_harness/tools/read.py
      - id: TASK-3-4
        description: Implement tools/write.py and tools/edit.py with exact string replacement, replace_all, parent directory creation, permission preservation
        acceptance: Write creates/overwrites files. Edit replaces exact strings. Non-unique old_string errors. replace_all works. Parent dirs created.
        role: coder
        files:
          - shared/egg_harness/tools/write.py
          - shared/egg_harness/tools/edit.py
      - id: TASK-3-5
        description: Implement tools/glob_tool.py (pathlib/fd) and tools/grep.py (ripgrep) with file type filtering, context lines, output modes, head_limit
        acceptance: Glob matches patterns and returns sorted paths. Grep finds regex matches with context. Head limit caps output.
        role: coder
        files:
          - shared/egg_harness/tools/glob_tool.py
          - shared/egg_harness/tools/grep.py
      - id: TASK-3-6
        description: Implement tools/web_fetch.py (HTML-to-markdown conversion) and tools/web_search.py, both conditionally disabled in private mode
        acceptance: WebFetch converts HTML to markdown. Both disabled in private mode. Tool schemas match Claude Code definitions.
        role: coder
        files:
          - shared/egg_harness/tools/web_fetch.py
          - shared/egg_harness/tools/web_search.py
  - id: 4
    name: Agent Loop Core
    goal: Implement the core agentic loop tying providers and tools together with turn management and signal handling
    tasks:
      - id: TASK-4-1
        description: Implement loop.py AgentLoop with core loop logic (messages→provider→stream→tools→repeat), sequential tool execution, EventBus integration
        acceptance: Loop executes multi-turn tool use. Text streamed via on_output. Tool calls executed and results fed back. Loop terminates on end_turn.
        role: coder
        files:
          - shared/egg_harness/loop.py
      - id: TASK-4-2
        description: Add max_turns limit, wall-clock timeout, all stop_reason handling (end_turn, max_tokens, stop_sequence, tool_use), and circuit breaker to AgentLoop
        acceptance: Loop stops at max_turns. Timeout triggers graceful stop. All stop_reasons handled correctly. Circuit breaker fires after 3 tool failures.
        role: coder
        files:
          - shared/egg_harness/loop.py
      - id: TASK-4-3
        description: Add SIGTERM graceful shutdown to AgentLoop with 30s grace period, process group cleanup, and partial AgentResult return
        acceptance: SIGTERM during tool execution waits up to 30s. SIGTERM during API call cancels. No orphaned subprocesses. Partial result returned.
        role: coder
        files:
          - shared/egg_harness/loop.py
  - id: 5
    name: Context Management & Session Persistence
    goal: Implement compaction, session persistence, and anchor integration hooks for long-running agents
    tasks:
      - id: TASK-5-1
        description: Implement compaction.py with token budget tracking, threshold-based trigger (80% default), cut-point selection preserving tool call/result pairs, and structured summary generation
        acceptance: Token tracking accurate. Compaction triggers at threshold. Cut point never splits tool pairs. Summary captures key context.
        role: coder
        files:
          - shared/egg_harness/compaction.py
      - id: TASK-5-2
        description: Add compaction loop protection (abort if 2 compactions within 3 turns), manual compact_now() trigger, and on_compaction event emission
        acceptance: Double compaction within N turns raises error. Manual compaction works. Compaction count tracked.
        role: coder
        files:
          - shared/egg_harness/compaction.py
          - shared/egg_harness/loop.py
      - id: TASK-5-3
        description: Implement session.py with JSONL serialization, session metadata tracking, auto-save on compaction and intervals, and resume-from-file
        acceptance: Session saves to JSONL with metadata. Resume reconstructs state. Auto-save triggers correctly. Session ID stable across saves.
        role: coder
        files:
          - shared/egg_harness/session.py
  - id: 6
    name: System Prompt & Permissions
    goal: Implement generic system prompt assembly and permission callback wiring
    tasks:
      - id: TASK-6-1
        description: Implement prompt.py with generic system prompt assembly accepting list of sources (strings/callables), concatenated with --- separators
        acceptance: build_system_prompt concatenates sources with separators. Callables invoked. Empty sources skipped.
        role: coder
        files:
          - shared/egg_harness/prompt.py
      - id: TASK-6-2
        description: Wire permission callback in AgentLoop — can_use_tool check before each tool execution, tool disallow list from config
        acceptance: Disallowed tools return error. Permission callback blocks specific invocations. Error messages clear.
        role: coder
        files:
          - shared/egg_harness/loop.py
  - id: 7
    name: Client, CLI & Interactive Mode
    goal: Create drop-in replacement entry points for headless and interactive agent execution
    tasks:
      - id: TASK-7-1
        description: Implement client.py with run_agent_async() matching egg_agent.client signature, assembling all components, and synchronous run_agent() wrapper
        acceptance: run_agent_async() has identical signature to egg_agent version. Returns populated AgentResult. on_output streams text.
        role: coder
        files:
          - shared/egg_harness/client.py
      - id: TASK-7-2
        description: Implement __main__.py CLI entry point with --model, --max-turns, --system-prompt, --timeout args, stdin prompt reading, stdout streaming
        acceptance: python3 -m egg_harness works with all CLI args. Stdin prompt reading works. Output streams. Exit code correct.
        role: coder
        files:
          - shared/egg_harness/__main__.py
      - id: TASK-7-3
        description: Implement interactive.py with minimal readline REPL, multi-turn conversation, Ctrl-C interrupt, Ctrl-D exit
        acceptance: REPL reads input and streams responses. Ctrl-C interrupts. Ctrl-D exits. Multi-turn maintains context.
        role: coder
        files:
          - shared/egg_harness/interactive.py
  - id: 8
    name: Egg Integration Layer
    goal: Create egg-specific integration wiring egg tools, permissions, prompt assembly, compaction, and factory
    tasks:
      - id: TASK-8-1
        description: Create shared/egg_harness_integration/ package with egg_tools.py registering 5 egg-native tools (EggOrch, EggContract, EggCheckpoint, GitOps, GhCli) via CLI shell-out
        acceptance: All 5 tools registered and executable. CLI commands invoked correctly. Tool results returned.
        role: coder
        files:
          - shared/egg_harness_integration/__init__.py
          - shared/egg_harness_integration/egg_tools.py
      - id: TASK-8-2
        description: Implement egg_prompt.py replicating exact CLAUDE.md rule-merging from setup_agent_rules() plus settings.json parsing with HarnessConfig precedence
        acceptance: Rule assembly matches setup_agent_rules() output for same input files. settings.json properties applied as defaults.
        role: coder
        files:
          - shared/egg_harness_integration/egg_prompt.py
      - id: TASK-8-3
        description: Implement egg_permissions.py adapting egg_restrictions.check_agent_file_access() as can_use_tool callback matching tool_interceptor.py behavior
        acceptance: Permission callback blocks out-of-scope file writes. Error messages match existing format. Roles loaded from EGG_AGENT_ROLE.
        role: coder
        files:
          - shared/egg_harness_integration/egg_permissions.py
      - id: TASK-8-4
        description: Implement egg_compaction.py with anchor-based compaction integration persisting to .egg-state/agent-anchors/ and post-compaction message bus recovery
        acceptance: Compaction persists anchor with correct schema. Post-compaction reads anchor and polls message bus.
        role: coder
        files:
          - shared/egg_harness_integration/egg_compaction.py
      - id: TASK-8-5
        description: Implement harness_factory.py factory function creating fully-configured AgentLoop with gateway routing, all tools, permissions, prompt assembly, and compaction
        acceptance: Factory creates working AgentLoop. Provider routes through gateway. All tools registered. Permissions enforced.
        role: coder
        files:
          - shared/egg_harness_integration/harness_factory.py
  - id: 9
    name: Harness Selection & Wiring
    goal: Wire harness selection into existing egg_agent module and sandbox entrypoint
    tasks:
      - id: TASK-9-1
        description: Update shared/egg_agent/client.py to route run_agent_async() to egg_harness when EGG_HARNESS=egg, default to existing claude-sdk path
        acceptance: EGG_HARNESS=egg routes to new harness. Unset uses SDK. Both return AgentResult. Zero changes to consumers.
        role: coder
        files:
          - shared/egg_agent/client.py
      - id: TASK-9-2
        description: Update shared/egg_agent/command.py to route build_agent_command() to python3 -m egg_harness when EGG_HARNESS=egg, propagate env var to children
        acceptance: build_agent_command returns egg_harness module when EGG_HARNESS=egg. Child agents inherit harness selection.
        role: coder
        files:
          - shared/egg_agent/command.py
      - id: TASK-9-3
        description: Update sandbox/entrypoint.py to support EGG_HARNESS for interactive mode, configure gateway URL, add startup validation
        acceptance: Entrypoint respects EGG_HARNESS for interactive. Gateway URL configured. API key absence validated.
        role: coder
        files:
          - sandbox/entrypoint.py
      - id: TASK-9-4
        description: Update shared/pyproject.toml to include egg_harness and egg_harness_integration packages, add anthropic>=0.50,<1.0 and markdownify dependencies
        acceptance: Both packages discoverable. Dependencies installable. pip install -e shared/ works.
        role: coder
        files:
          - shared/pyproject.toml
  - id: 10
    name: Tests
    goal: Comprehensive test coverage for all harness subsystems with unit, integration, and compliance tests
    tasks:
      - id: TASK-10-1
        description: Unit tests for config, cost, events, and result types covering model aliases, cost calculation, EventBus, and AgentResult compatibility
        acceptance: Config edge cases tested. Cost rates accurate. EventBus error handling verified. All tests pass.
        role: tester
        files:
          - shared/egg_harness/tests/__init__.py
          - shared/egg_harness/tests/test_config.py
          - shared/egg_harness/tests/test_cost.py
          - shared/egg_harness/tests/test_events.py
          - shared/egg_harness/tests/test_result.py
      - id: TASK-10-2
        description: Unit tests for providers with mock HTTP responses, testing streaming, retry logic, circuit breaker, and gateway URL validation
        acceptance: Both providers parse mocked streams correctly. Retry and circuit breaker tested. Gateway URL validation tested.
        role: tester
        files:
          - shared/egg_harness/tests/test_providers.py
      - id: TASK-10-3
        description: Unit tests for tool registry and standard tools covering permission blocking, output truncation, Bash timeout, Edit exact-match semantics
        acceptance: Permission blocking tested. Truncation works. Bash process group cleanup verified. Edit semantics correct.
        role: tester
        files:
          - shared/egg_harness/tests/test_registry.py
          - shared/egg_harness/tests/test_tools.py
      - id: TASK-10-4
        description: Unit tests for agent loop covering multi-turn execution, max_turns, timeout, stop_reasons, circuit breaker, SIGTERM, compaction trigger, and session save/resume
        acceptance: Loop tested with mock provider. All stop_reasons verified. Timeout and SIGTERM tested. Compaction and session round-trip tested.
        role: tester
        files:
          - shared/egg_harness/tests/test_loop.py
          - shared/egg_harness/tests/test_compaction.py
          - shared/egg_harness/tests/test_session.py
      - id: TASK-10-5
        description: Integration test for end-to-end harness execution with mock HTTP endpoint, tool execution, streaming, and harness selection routing
        acceptance: End-to-end test passes. Streaming captured. Metadata populated. Harness selection routes correctly.
        role: tester
        files:
          - shared/egg_harness/tests/integration/__init__.py
          - shared/egg_harness/tests/integration/test_harness_e2e.py
      - id: TASK-10-6
        description: Tool behavioral parity compliance tests comparing harness tool outputs against Claude Code for edge cases (Edit non-unique, Read binary, Bash timeout, Grep large dirs)
        acceptance: At least 5 test cases per tool. Known differences documented. Critical differences (Edit, Bash) have exact parity.
        role: tester
        files:
          - shared/egg_harness/tests/compliance/__init__.py
          - shared/egg_harness/tests/compliance/test_tool_parity.py
```
