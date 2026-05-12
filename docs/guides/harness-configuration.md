# Harness Configuration

> How to select and configure egg's agent runtime harness.

## Overview

egg supports three agent runtime options ("harnesses") for executing LLM agents in sandbox containers. The harness controls how the agent communicates with the LLM, executes tools, manages context, and handles sessions.

| Harness | Runtime | Use Case |
|---------|---------|----------|
| `claude-sdk` (default) | Claude Agent SDK | Anthropic subscription users running headless pipelines |
| `claude-code` | Claude Code CLI | Anthropic subscription users in interactive sessions |
| `egg` | egg_harness | Multi-provider support, full context management control, custom tool injection |

The default is `claude-sdk` during the transition period. The `egg` harness becomes the default after parallel validation demonstrates metric parity (cost, turns, duration, task success rate).

## Selecting a Harness

Set the `EGG_HARNESS` environment variable or configure it in `PipelineConfig`:

```bash
# Use the custom egg harness
export EGG_HARNESS=egg

# Use the Claude Agent SDK (default)
export EGG_HARNESS=claude-sdk

# Use the Claude Code CLI (interactive)
export EGG_HARNESS=claude-code
```

The harness selection propagates to child agent processes automatically, ensuring consistent runtime across all agents in a pipeline.

### Rollback

To roll back from the `egg` harness to the default SDK path, set `EGG_HARNESS=claude-sdk` (or unset the variable entirely). No code changes are required.

## The `egg` Harness

The custom `egg` harness (`shared/egg_harness/`) replaces the Claude Agent SDK with an owned runtime. It provides:

- **Multi-provider LLM support** -- Anthropic (via SDK) and OpenAI-compatible endpoints (via httpx)
- **8 standard tools** -- Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch
- **Context management** -- token tracking, threshold-based compaction, anchor persistence
- **Session persistence** -- JSONL serialization for resume across agent restarts
- **Event system** -- composable callbacks for monitoring and extension

### CLI Usage

```bash
# Drop-in replacement for python3 -m egg_agent
python3 -m egg_harness --model opus --max-turns 200 "Fix the authentication bug"

# Read prompt from stdin
echo "Fix the bug" | python3 -m egg_harness --model sonnet --max-turns 50

# With system prompt
python3 -m egg_harness --model opus --max-turns 200 \
  --system-prompt "You are a security reviewer" \
  "Review this code for vulnerabilities"

# Interactive REPL mode
python3 -m egg_harness --interactive --model opus
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
print(f"Cost: ${result.cost_usd:.4f}, Turns: {result.num_turns}")

# Async with streaming callback
async def on_output(text: str) -> None:
    print(text, end="", flush=True)

result = await run_agent_async(
    prompt="Fix the bug",
    model="opus",
    max_turns=200,
    on_output=on_output,
)
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--model` | `opus` | Model alias or full model ID |
| `--max-turns` | `200` | Maximum conversation turns |
| `--timeout` | `7200` | Wall-clock timeout in seconds |
| `--system-prompt` | None | System prompt text or file path |
| `--interactive` | false | Launch interactive REPL |
| `prompt` | stdin | Prompt text (positional arg or stdin) |

### Model Aliases

| Alias | Resolves To |
|-------|-------------|
| `opus` | `claude-opus-4-6` |
| `sonnet` | `claude-sonnet-4-5-20250514` |
| `haiku` | `claude-haiku-4-5` |

The `opus[1m]` suffix syntax is supported for backwards compatibility, setting `max_tokens=1000000` with the appropriate beta headers.

## Egg Integration Layer

When running inside egg's sandbox containers, the `egg_harness_integration` package wires in egg-specific functionality:

- **Egg-native tools** -- EggOrch, EggContract, EggCheckpoint, GitOps, GhCli (shell out to CLIs)
- **CLAUDE.md rule-merging** -- replicates exact `setup_agent_rules()` behavior
- **Role-based permissions** -- wraps `egg_restrictions` for file access enforcement
- **Anchor-based compaction** -- persists state to agent anchors on compaction (#1032)

The integration layer is activated automatically via the `harness_factory`:

```python
from egg_harness_integration.harness_factory import create_egg_harness

loop = create_egg_harness(model="opus", max_turns=200)
result = await loop.run("Fix the authentication bug")
```

### Integration Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `EGG_AGENT_ROLE` | Yes | Agent role for permission enforcement |
| `EGG_PIPELINE_ID` | No | Pipeline ID for anchor-based compaction |
| `EGG_NETWORK_MODE` | No | `public` or `private` -- controls WebFetch/WebSearch |
| `AGENT_ANCHOR_ID` | No | Agent anchor ID for compaction persistence |
| `EGG_ORCHESTRATOR_URL` | No | Orchestrator URL for message bus polling |
| `GATEWAY_URL` | No | Gateway URL for API proxy routing |
| `EGG_PIPELINE_REPO_PATTERNS_JSON` | No | JSON object of per-repo role-pattern overrides, shape `{<owner/repo>: {tests_globs?: [...], code_globs?: [...], docs_globs?: [...]}}` (pre-resolved by orchestrator from `repositories.yaml`; sandbox containers have no access to `repositories.yaml` directly) |

## Context Management / Compaction

The `egg` harness owns context management rather than delegating to an opaque runtime. This is critical for long-running agents, especially in BRC consensus loops:

1. **Token tracking** -- each API response includes token counts; the harness tracks cumulative usage
2. **Threshold trigger** -- when tokens exceed `compaction_threshold` (default 80%) of the model's context window, compaction fires before the next API call
3. **Compaction strategy** -- summarize older messages (goal, progress, decisions, files, errors), keep recent history (default 20,000 tokens), clear and restart from summary
4. **Anchor integration** -- on compaction, state is persisted to `.egg-state/agent-anchors/` via the anchor mechanism (#1032)
5. **Loop protection** -- if compaction fires twice within 3 turns, the agent aborts to prevent infinite compaction loops

## Security Model

All three harness options maintain egg's zero-credential sandbox model:

- API calls route through the gateway proxy (credentials injected server-side)
- `ANTHROPIC_API_KEY` must NOT be present in the sandbox environment (asserted at startup)
- Permission callbacks enforce role-based file access before every tool execution
- The Bash tool never uses `shell=True` (mitigates CVE-2026-35022)
- Gateway URL validation prevents redirect-based key exfiltration (mitigates CVE-2026-21852)

## Comparison

| Capability | `egg` harness | `claude-sdk` | `claude-code` |
|------------|:---:|:---:|:---:|
| Multi-provider LLM support | Yes | No | No |
| Owned context management | Yes | No | No |
| Session persistence/resume | Yes | Limited | No |
| Event system (callbacks) | Yes | Limited | No |
| Interactive REPL | Yes | No | Yes |
| Custom tool injection | Yes | Via callback | Via settings |
| Anthropic-only | No | Yes | Yes |
| Requires Anthropic subscription | No | Yes | Yes |

## Parity Validation

Before promoting the `egg` harness to the default, run the parallel validation script to confirm metric parity (cost, turns, duration, success rate) against `claude-sdk`:

```bash
python3 scripts/validate_harness_parity.py [--scenarios N] [--model MODEL] [--output results.json]
```

The script runs each scenario through both harnesses sequentially and prints a comparison table. The `egg` harness passes validation when its success rate is ≥ 80% and its total cost does not exceed 1.5× the `claude-sdk` baseline.

| Flag | Default | Description |
|------|---------|-------------|
| `--scenarios N` | all (10) | Number of scenarios to run |
| `--model MODEL` | `haiku` | Model alias for fast/cheap validation |
| `--max-turns N` | `15` | Max turns per scenario |
| `--output PATH` | none | Write JSON results to file |

## Related

- [Custom Harness Architecture](../architecture/custom-harness.md) -- design decisions and security model
- [egg_harness README](../../shared/egg_harness/README.md) -- core package documentation
- [egg_harness_integration README](../../shared/egg_harness_integration/README.md) -- integration layer docs
- [Anchor Recovery Guide](anchor-recovery.md) -- compaction + anchor integration
