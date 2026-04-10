# Analysis: Build Custom Coding Harness with Multi-Provider Support

> Issue: #1570 | Phase: refine | Complexity: **high**

## Problem Statement

Egg currently depends on two Anthropic-maintained runtimes — the Claude Code CLI (for interactive sessions) and the Claude Agent SDK (for headless pipeline agents). Both are opaque, evolve on Anthropic's release schedule, and have recently been targets of critical security vulnerabilities (CVE-2026-35022, CVE-2025-59536, CVE-2026-21852). The project needs a custom coding harness that gives egg full ownership of the agent execution loop, tool system, context management, and provider routing — while keeping Claude Code and the Agent SDK as supported alternatives for users with Anthropic subscriptions.

**Current state**: All agent execution routes through either `claude_agent_sdk.query()` (headless) or `claude --dangerously-skip-permissions` (interactive). Neither supports non-Anthropic models, custom compaction, or session persistence. Context window exhaustion is handled opaquely by the SDK/CLI with no integration into egg's anchor mechanism (#1032).

**Desired outcome**: A self-contained `egg_harness` package that can replace both runtimes, supports Anthropic and OpenAI-compatible providers, owns compaction with anchor integration, supports session persistence for consensus wrapper restarts, and is structured for eventual extraction from the egg monorepo.

## Prior Work (v1 and v2 Runs)

This is the **v3 refine run**. Prior runs produced significant artifacts:

- **v1**: Completed refine phase with all 12 HITL decisions resolved. Plan phase stalled (architect agent hung, see #1551).
- **v2**: Architect produced a detailed 7-subsystem architecture analysis (ACKed). Risk analyst identified 12 risks (3 CRITICAL, 4 HIGH) with mitigations (ACKed). Task planner stalled — did not produce the consolidated plan file. Plan review verdict: `needs_revision`.
- **Post-v2 scope expansion**: Issue scope expanded to include Pi-parity features (compaction, session management, context management) and a modularity requirement (isolated, extractable package).

All 12 HITL design decisions from v1 are resolved and binding. The v2 architect and risk analyst outputs are incorporated into this analysis as validated prior work.

## Current Behavior

### Agent SDK Path (`shared/egg_agent/client.py`)

The `run_agent_async()` function wraps `claude_agent_sdk.query()` with:

- **`ClaudeAgentOptions`**: `permission_mode="bypassPermissions"`, configurable model (default `"opus[1m]"`), `setting_sources=["project", "user"]`, optional `disallowed_tools`, `can_use_tool` callback
- **Streaming**: Async generator yielding `AssistantMessage`, `UserMessage`, `SystemMessage`, `ResultMessage`
- **Result metadata**: `total_cost_usd`, `num_turns`, `duration_ms`, `session_id`
- **Tool interception**: `tool_interceptor.py` blocks Write/Edit/NotebookEdit to out-of-scope paths via `egg_restrictions.check_agent_file_access()`
- **CLI entry**: `python3 -m egg_agent --model opus[1m] --max-turns N --system-prompt "..." prompt`

### Claude Code CLI Path (`sandbox/llm/runner.py`)

- Launches `claude --dangerously-skip-permissions --model opus[1m]` via `os.execvpe()`
- Configuration injected through `~/.claude/settings.json` (permissions, tools, model), `~/.claude/CLAUDE.md` (combined rule files), and `~/.claude.json` (onboarding bypass)
- Rule files assembled from `sandbox/agent-config/rules/` (mission.md, environment.md, code-standards.md, etc.) concatenated with `---` separators

### Gateway Proxy (`gateway/gateway.py`)

- `/v1/messages` endpoint proxies all API traffic, injects credentials from session, captures transcripts
- Session lookup by container IP (no bearer tokens in headers)
- Tool filtering: strips WebFetch/WebSearch definitions in private mode
- Transcript buffer: JSONL at `/tmp/egg-transcripts/{container_id}.jsonl`

### Consumers

| Consumer | Spawns via | Uses |
|----------|-----------|------|
| `orchestrator/consensus_wrapper.py` | `python3 -m egg_agent` subprocess | Handles restart-on-exit for BRC, injects recovery prompts |
| `shared/egg_babysit/fixer.py` | `build_agent_command()` subprocess | Compares HEAD before/after for commit detection |
| `shared/egg_babysit/reviewer.py` | `build_agent_command()` subprocess | Read-only prompt prefix, captures PR review verdict |
| `orchestrator/container_spawner.py` | Docker container with command override | Registers gateway session, wires environment |

## Constraints

### Technical

- **Gateway is the trust boundary**: All API calls MUST route through the gateway proxy. The sandbox container must never have direct access to API keys. This is non-negotiable per the zero-credential sandbox model.
- **CVE-2026-35022 (CVSS 9.8)**: Authentication helper `shell=true` command injection in Claude CLI/SDK. The harness must NEVER use `shell=true` in subprocess calls. A CI linter rule should enforce this.
- **CVE-2025-59536 / CVE-2026-21852**: ANTHROPIC_BASE_URL redirect allows pre-trust API key exfiltration. The harness sets this to the gateway — same mechanism exploited. Must validate the target URL is the expected gateway endpoint.
- **Claude Haiku 3 retires 2026-04-19**: Model alias `haiku` must map to `claude-haiku-4-5`, not the deprecated `claude-haiku-3-5-20241022`.
- **1M context window beta**: `opus[1m]` suffix syntax must use correct beta headers. The 1M beta retirement is scheduled for 2026-04-30 — the harness needs to handle both the beta period and post-beta gracefully.
- **Anthropic SDK version**: Pin `anthropic>=0.50,<1.0` per risk analyst recommendation. The latest version is 0.79.0 (Feb 2026).
- **Tool behavioral parity**: 10+ tools reimplemented from scratch. Edge cases (encoding, line endings, symlinks, binary detection, truncation, process groups for Bash timeout) must match Claude Code's behavior for parallel validation.
- **Streaming event complexity**: Anthropic's SSE stream has 8+ event types (message_start, content_block_start, content_block_delta, content_block_stop, message_delta, message_stop, plus thinking/redacted variants). The Anthropic SDK handles accumulation; raw httpx would require reimplementing this.

### Business

- **Parallel validation required**: The harness cannot become the default until metrics (cost_usd, num_turns, duration_ms, task success rate) show parity with the current Claude SDK path on 10+ test pipelines.
- **No forced migration**: Claude Code and Agent SDK remain supported alternatives. Users with Anthropic subscriptions can continue using them.
- **Extractability**: The harness must be structured for eventual extraction into a standalone package — no hardcoded egg assumptions in the core.

### Dependencies

- **#1032 (Anchor mechanism)**: Compaction integration requires the anchor schema and recovery protocol. The anchor system exists (`shared/egg_anchor/`) with complete models, loader, and validator.
- **#1571 (Multi-model routing)**: Post-MVP, but the provider abstraction must be designed so it layers on without rewiring.
- **Consensus wrapper**: Must continue to handle restart-on-exit. Session persistence enables the wrapper to resume conversation context rather than starting fresh.

## Options Considered

### Option A: Anthropic SDK Only

**Approach**: Use `anthropic` Python SDK for all providers, including OpenAI-compatible endpoints via SDK adapter.

**Pros**:
- Single dependency for all providers
- SDK handles streaming complexity (8+ event types, partial JSON accumulation)
- Less code to maintain

**Cons**:
- Cannot call OpenAI-compatible `/v1/chat/completions` endpoints — the SDK only speaks Anthropic's API format
- Locks the harness to Anthropic's SDK release schedule for bug fixes and new features
- No path to vLLM, Ollama, or other OpenAI-compatible backends

**Verdict**: Rejected (v2 architect analysis, confirmed by HITL decisions).

### Option B: Raw httpx Only

**Approach**: Build from scratch using httpx for both Anthropic and OpenAI-compatible providers.

**Pros**:
- Zero SDK dependency — full control over all HTTP interactions
- Consistent implementation pattern across providers

**Cons**:
- Anthropic streaming is complex (8+ SSE event types, partial JSON accumulation, content block lifecycle) — high risk of subtle bugs
- Would need to reimplement tool result formatting, thinking block handling, cache control headers
- Significantly more code and testing surface

**Verdict**: Rejected (v2 architect analysis). The risk of getting Anthropic streaming wrong is too high.

### Option C: LiteLLM Unified Abstraction

**Approach**: Use LiteLLM as a unified provider abstraction over all LLM backends.

**Pros**:
- Single interface for 100+ providers out of the box
- Community-maintained provider compatibility

**Cons**:
- 50+ transitive dependencies — contradicts the goal of reducing external dependencies
- Conflicts with the gateway credential model (LiteLLM wants to manage API keys)
- Opaque: behavior changes with LiteLLM upgrades could break agent behavior
- Adds another third-party runtime dependency — the opposite of the stated motivation

**Verdict**: Rejected (v2 architect analysis, confirmed by HITL decisions).

### Option D: Hybrid — Anthropic SDK + Raw httpx (Recommended)

**Approach**: Use the `anthropic` Python SDK for Anthropic provider (reliable streaming, battle-tested), and raw `httpx` for OpenAI-compatible endpoints (simpler SSE format).

**Pros**:
- Best reliability for the critical Anthropic path — SDK handles all streaming edge cases
- OpenAI-compatible SSE is simpler (fewer event types, no content block lifecycle) — manageable with httpx
- Minimal dependencies: `anthropic` SDK + `httpx` (which is already a transitive dep of the SDK)
- Provider interface is clean: both implement `send_message() -> AsyncIterator[StreamEvent]`

**Cons**:
- Two different streaming implementations to maintain
- Anthropic SDK version coupling (mitigated by pinning `>=0.50,<1.0`)

**Verdict**: Selected (v2 architect analysis, confirmed by HITL decisions).

## Recommended Approach

**Option D: Hybrid (Anthropic SDK + raw httpx)** is recommended, as validated by the v2 architect analysis and risk assessment.

### Architecture Summary (from v2 Architect)

Seven subsystems organized into two packages:

**`shared/egg_harness/`** (core, extractable, no egg imports):

1. **Providers** (`providers/`): Abstract `Provider` base class with `send_message() -> AsyncIterator[StreamEvent]`. Anthropic provider uses `AsyncAnthropic(base_url=gateway_url)`. OpenAI-compatible provider uses raw httpx SSE.

2. **Tools** (`tools/`): ToolRegistry with register/execute pattern. 8 standard tools (Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch). Registry applies permission callbacks before execution.

3. **Agent Loop** (`loop.py`): Core loop — send messages → consume stream → accumulate tool calls → execute → feed results back. Max turns, wall-clock timeout, SIGTERM handling, on_output callback. Context tracking and compaction trigger.

4. **Context Management** (`compaction.py`): Token budget tracking, threshold-based compaction (default 80% of model max), summarize + clear strategy aligned with Pi's approach (walk backwards to find cut point, summarize older messages, keep recent). Post-compaction anchor persistence.

5. **Session Persistence** (`session.py`): JSONL serialization of conversation state. Resume from file for consensus wrapper restart scenarios. Auto-save on compaction.

6. **Event System** (`events.py`): Observable callbacks — `on_output`, `on_tool_call`, `on_compaction`, `on_error`, `on_turn_complete`. Enables monitoring without modifying core loop.

7. **Config & CLI** (`config.py`, `__main__.py`, `client.py`): Drop-in `run_agent_async()` interface matching `egg_agent`. Same CLI args. Model alias resolution with `opus[1m]` suffix parsing. Hardcoded per-model cost rates.

**`shared/egg_harness_integration/`** (or within `egg_agent/`):

- Egg-native tools (EggOrch, EggContract, EggCheckpoint, GitOps, GhCli) — shell out to CLIs initially per HITL-3
- CLAUDE.md rule-merging replicating `sandbox/agent-config/rules/` assembly
- Permission callbacks wrapping `egg_restrictions`
- Anchor-based compaction integration (#1032)
- Harness factory wiring all integrations together

### Compaction Strategy

Aligned with Pi's coding agent approach but integrated with egg's anchor mechanism:

1. **Token tracking**: Use token counts from API response `usage` field. Track total context size (system prompt + conversation + tools) per turn.
2. **Trigger**: When `context_tokens > context_window * threshold` (default 0.80). Pi uses `context_window - reserve_tokens` (default 16,384 reserve) — similar concept.
3. **Cut point selection**: Walk backwards from newest message, accumulate tokens until `keep_recent_tokens` threshold (default 20,000). Messages before the cut point are summarized; messages after are kept. Never cut between a tool call and its result.
4. **Summarization**: Generate structured summary (goal, progress, decisions, next steps, files modified) using the same model. Pi uses structured markdown with specific sections.
5. **Anchor persistence**: Before clearing, persist state to `.egg-state/agent-anchors/<agent-id>.json` per the existing anchor schema. Post-compaction recovery reads the anchor + polls message bus for missed BRC messages.
6. **Loop protection**: If compaction fires twice within N turns (configurable, default 3), abort with error to prevent infinite compaction loops.
7. **Manual trigger**: Support explicit compaction via tool call for agents that want to compact at natural checkpoints.

### Harness Selection

Three harness options, selectable via `EGG_HARNESS` env var or pipeline config:

| Value | Runtime | Use case |
|-------|---------|----------|
| `egg` | egg_harness (new) | Multi-provider, full context control |
| `claude-sdk` (default during transition) | Claude Agent SDK | Anthropic subscription users (headless) |
| `claude-code` | Claude Code CLI | Anthropic subscription users (interactive) |

### Migration Path

1. Build harness with identical `run_agent_async()` interface
2. Default to `claude-sdk`; opt-in to `egg` via `EGG_HARNESS=egg`
3. Run both in parallel on test pipelines, compare metrics
4. Switch default to `egg` when parity is confirmed
5. Claude Code CLI and Agent SDK remain installed for subscription users

### Security Mitigations (from v2 Risk Assessment)

| Risk | Mitigation |
|------|------------|
| Credential leak | Route ALL API calls through gateway. Startup assertion that `ANTHROPIC_API_KEY` NOT in `os.environ`. |
| CVE-2026-35022 | NEVER use `shell=true` in subprocess calls. CI linter rule. |
| CVE-2026-21852 | Validate `ANTHROPIC_BASE_URL` matches expected gateway URL at startup. |
| Tool parity | Compliance test suite comparing same inputs across both implementations. |
| Infinite loops | Strict turn counting + token budget + circuit breaker (3 consecutive failures → exit). |
| SIGTERM handling | 30s grace period, clean shutdown of tool processes. |

## Open Questions

All questions below are registered as HITL decisions or feedback requests in the contract (`1570.json`). The pipeline will surface them for human input.

> **Note**: The gateway's contract mutation API was unavailable during this session (worktree resolution failure), so decisions and feedback were written directly to the contract file via the Python library. The questions are fully registered and will function correctly when the contract is read.

### HITL Decisions (Multiple Choice)

#### decision-1: Compaction Summarization Model

Should compaction summaries be generated by the same model running the conversation, or by a cheaper/faster model (e.g., always use Sonnet for summaries regardless of the main model)?

- [ ] Same model as conversation (simpler, Pi's approach)
- [ ] Dedicated cheaper model (e.g., Sonnet)
- [ ] Configurable per-provider (default same model)
- [ ] Other (explain in reply)

**Context**: Pi uses the same model. Same model is simpler to implement (no second provider call) but costlier for Opus conversations. A dedicated cheaper model adds API routing complexity.

#### decision-2: Session Storage Location

Where should JSONL session files be persisted for conversation resume?

- [ ] Container filesystem (`/tmp/egg-sessions/`) — simple, lost on container exit
- [ ] Repo `.egg-state/sessions/` — survives restarts, adds git noise
- [ ] Mounted volume (`~/sharing/sessions/`) — persistent, no git noise, requires mount config
- [ ] Other (explain in reply)

**Context**: Session persistence is needed for the consensus wrapper's restart-on-exit to resume conversation context rather than starting fresh. The primary use case is within a single container lifecycle (BRC restarts), but cross-container persistence would enable future session resume features.

#### decision-3: HTML-to-Markdown Library for WebFetch

Which library for converting fetched HTML to markdown in the WebFetch tool?

- [ ] markdownify (lightweight, pure Python, actively maintained)
- [ ] html2text (older, battle-tested, slightly different output)
- [ ] Custom minimal parser (no dependency, more work and edge cases)
- [ ] Other (explain in reply)

**Context**: Flagged as OD-1 by v2 architect. WebFetch needs to convert HTML pages to markdown for LLM processing. The choice affects output quality and dependency footprint.

#### decision-4: Interactive Mode Scope for MVP

How much terminal UI capability should the interactive mode have for MVP?

- [ ] Minimal readline (basic I/O, no colors — just enough to replace `claude --dangerously-skip-permissions`)
- [ ] Rich library (colors, markdown rendering, progress spinners — better UX, adds dependency)
- [ ] Defer interactive to post-MVP (keep Claude Code CLI as the interactive option)
- [ ] Other (explain in reply)

**Context**: The issue requires "both headless and interactive in MVP" (HITL-2), but the depth of the interactive UX is unspecified. Pi's interactive mode uses a full TUI with colors, markdown, and extensions — but egg's MVP may not need that level of polish.

#### decision-5: settings.json Property Handling

Should the harness replicate Claude Code's `settings.json` parsing for behavioral parity, or define its own config?

- [ ] Replicate settings.json parsing (behavioral parity during parallel validation)
- [ ] Own config only (HarnessConfig/ProviderConfig, ignore settings.json)
- [ ] Both (settings.json as fallback defaults, HarnessConfig takes precedence)
- [ ] Other (explain in reply)

**Context**: The v2 plan review noted that the architect covers CLAUDE.md loading but not `settings.json` properties (`defaultPermissionMode`, `autoApproveEdits`, `defaultModel`, `disallowedTools`). The current SDK uses `setting_sources=['project', 'user']`. For parallel validation, behavioral parity requires matching these settings.

#### decision-6: OpenAI-Compatible Provider Minimum API Surface

What minimum API surface should the OpenAI-compatible provider require from backends (vLLM, Ollama, llama.cpp, etc.)?

- [ ] Require full tool_choice + streaming + system messages
- [ ] Startup capability detection (probe the endpoint, adapt behavior)
- [ ] Explicit config per endpoint (declare which features the backend supports)
- [ ] Other (explain in reply)

**Context**: Different OpenAI-compatible backends support different subsets of the API. tool_choice is required for agentic use but some backends don't support it. The harness could fail-fast, adapt, or require explicit configuration.

### Feedback Questions (Open-Ended)

The following questions are registered as a feedback block in the contract (`feedback.id`). The human should provide free-text answers.

**Q1**: Which existing test pipelines should be used for parallel validation of the harness against the Claude SDK? What are the success criteria (e.g., within X% cost, Y% success rate)? (HITL-11 resolved the metrics to compare but not the specific pipelines.)

**Q2**: Are there any additional security review requirements beyond the mandatory human review gates identified by the risk analyst (credential flow, agent loop, tool parity, system prompt assembly, parallel validation results)?

**Q3**: Should the harness support extended thinking (Claude's thinking blocks) in MVP, or can that be deferred to post-MVP? The current codebase doesn't appear to use extended thinking explicitly, but the SDK may handle it transparently.

## Complexity Assessment

**HIGH**. This is an architectural change introducing a new core subsystem (25-30 new files, 3,000-4,500 lines) with:
- Cross-cutting impact on all agent execution paths (headless, interactive, babysit, consensus wrapper)
- Multiple parallelizable work streams (providers+config, tools, agent loop+client, CLI+interactive, compaction+session, integration layer)
- Critical security requirements (zero-credential sandbox, CVE mitigations)
- Parallel validation requirement before default switch
- External dependencies (Anthropic SDK, OpenAI-compatible backends, Pi feature parity reference)
- 12 resolved HITL decisions from v1 that constrain the design space
- 3 CRITICAL + 4 HIGH risks identified by v2 risk analyst requiring mandatory human review gates

The v2 architect estimated 3-4 parallelizable work streams. This aligns with "high" complexity: many independent phases that could be parallelized.

---

*Authored-by: egg*
