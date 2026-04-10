# egg_harness_integration

Egg-specific integration layer that wires egg's tools, permissions, prompt assembly, and compaction into the core [`egg_harness`](../egg_harness/README.md) package.

## Overview

The core `egg_harness` package is designed to be provider-agnostic and extractable. This integration layer bridges it with egg's infrastructure:

- **Egg-native tools** — EggOrch, EggContract, EggCheckpoint, GitOps, GhCli (shell out to CLIs)
- **CLAUDE.md rule-merging** — replicates exact `sandbox/entrypoint.py:setup_agent_rules()` behavior
- **Role-based permissions** — wraps `egg_restrictions` for `can_use_tool` callback
- **Anchor-based compaction** — persists state to `.egg-state/agent-anchors/` on compaction (#1032)
- **Harness factory** — single entry point that wires all integrations together

## Package Structure

```
egg_harness_integration/
├── __init__.py
├── egg_tools.py          # Egg-native tool registration (5 tools)
├── egg_prompt.py         # CLAUDE.md rule-merging from sandbox/agent-config/rules/
├── egg_permissions.py    # Role-based file access via egg_restrictions
├── egg_compaction.py     # Anchor-based compaction for #1032 integration
└── harness_factory.py    # Factory to create fully-configured harness
```

## Usage

### Factory (recommended)

The factory function is the primary entry point for egg's use of the harness:

```python
from egg_harness_integration.harness_factory import create_egg_harness

# Creates a fully-configured AgentLoop with all egg integrations:
# - Provider routed through gateway proxy
# - Standard tools + egg-native tools registered
# - CLAUDE.md rules assembled as system prompt
# - Role-based permission callback wired
# - Anchor-based compaction configured
loop = create_egg_harness(
    model="opus",
    max_turns=200,
    system_prompt="Additional context for this agent run",
)

result = await loop.run("Fix the authentication bug")
```

### Egg-Native Tools

Five CLI-backed tools are registered via the tool registry:

| Tool | CLI | Description |
|------|-----|-------------|
| **EggOrch** | `egg-orch` | Orchestrator operations (consensus, messages, anchors, health) |
| **EggContract** | `egg-contract` | SDLC contract operations (show, add-commit, update-notes) |
| **EggCheckpoint** | `egg-checkpoint` | Checkpoint browsing (list, show, context, search) |
| **GitOps** | `git` | Git operations routed through gateway |
| **GhCli** | `gh` | GitHub CLI operations routed through gateway |

These tools shell out to the corresponding CLIs (per HITL decision #3). They are registered via the `ToolRegistry` interface, not hardcoded into the core harness.

```python
from egg_harness.tools.registry import ToolRegistry
from egg_harness_integration.egg_tools import register_egg_tools

registry = ToolRegistry()
register_egg_tools(registry)
```

### CLAUDE.md Rule-Merging

Replicates the exact behavior of `sandbox/entrypoint.py:setup_agent_rules()`:

```python
from egg_harness_integration.egg_prompt import build_egg_system_prompt

# Loads rules from sandbox/agent-config/rules/, concatenates with ---
# separators in the correct order, then appends project-level CLAUDE.md
system_prompt = build_egg_system_prompt(
    rules_dir="/path/to/sandbox/agent-config/rules",
    project_claude_md="/path/to/project/CLAUDE.md",
)
```

Also handles `settings.json` property parsing — `settings.json` values are applied as defaults, with `HarnessConfig` values taking precedence.

### Role-Based Permissions

Wraps `egg_restrictions.check_agent_file_access()` as a `can_use_tool` callback:

```python
from egg_harness_integration.egg_permissions import create_permission_callback

# Reads EGG_AGENT_ROLE from environment
# Blocks Write/Edit/NotebookEdit to out-of-scope paths
# Returns descriptive error messages identifying the owning role
callback = create_permission_callback()
registry.set_permission_callback(callback)
```

### Anchor-Based Compaction

Integrates compaction with egg's anchor mechanism (#1032):

```python
from egg_harness_integration.egg_compaction import create_compaction_handler

# On compaction: persists state to .egg-state/agent-anchors/<agent-id>.json
# Post-compaction: reads anchor + polls message bus for missed BRC messages
handler = create_compaction_handler(
    agent_id="coder-abc12345",
    pipeline_id="issue-123",
)
```

## Environment Variables

The integration layer reads these egg-specific environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `EGG_AGENT_ROLE` | Yes | Agent role for permission enforcement (coder, tester, documenter, etc.) |
| `EGG_PIPELINE_ID` | No | Pipeline ID for anchor-based compaction |
| `EGG_HARNESS` | No | Harness selection (`egg`, `claude-sdk`, `claude-code`) |
| `EGG_NETWORK_MODE` | No | Network mode (`public` or `private`) — controls tool availability |
| `AGENT_ANCHOR_ID` | No | Agent anchor ID for compaction persistence |
| `EGG_ORCHESTRATOR_URL` | No | Orchestrator URL for message bus polling |
| `GATEWAY_URL` | No | Gateway URL for API proxy routing |

## Harness Selection

The integration layer adds harness selection to `egg_agent`:

| `EGG_HARNESS` Value | Runtime | Entry Point |
|---------------------|---------|-------------|
| `egg` | egg_harness (this) | `python3 -m egg_harness` |
| `claude-sdk` (default) | Claude Agent SDK | `python3 -m egg_agent` (existing) |
| `claude-code` | Claude Code CLI | `claude --dangerously-skip-permissions` |

When `EGG_HARNESS=egg`:
- `run_agent_async()` in `egg_agent/client.py` routes to `harness_factory.create_egg_harness()`
- `build_agent_command()` in `egg_agent/command.py` returns `python3 -m egg_harness` command
- Interactive mode in `sandbox/entrypoint.py` launches `python3 -m egg_harness --interactive`
- `EGG_HARNESS` is propagated to child agent processes for consistent harness selection

The Claude Agent SDK and Claude Code CLI remain the defaults during the transition period. The egg harness must prove parity via parallel validation before becoming the default.

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| Shell out to CLIs for egg-native tools | HITL #3: lower risk, faster MVP. Structured for incremental native replacement. |
| Replicate exact CLAUDE.md rule-merging | HITL #9: simplifying risks behavioral differences in parallel validation. |
| Default `claude-sdk`, opt-in `egg` harness | HITL #4: safe transition; egg harness must prove parity first. |
| Anchor-based compaction | Aligns with #1032 for post-compaction state recovery across agent restarts. |

## Related

- [`egg_harness`](../egg_harness/README.md) — core harness package (provider-agnostic)
- [`egg_agent`](../egg_agent/) — existing Claude Agent SDK wrapper (modified for harness selection)
- [`egg_anchor`](../egg_anchor/README.md) — anchor mechanism used by compaction integration
- [`egg_restrictions`](../egg_restrictions/) — file access patterns used by permission callback
- [Custom Harness Architecture](../../docs/architecture/custom-harness.md) — architecture doc
- [Anchor Recovery Guide](../../docs/guides/anchor-recovery.md) — anchor-based recovery
