# Analysis: Improve checkpoint discoverability for agents

> Issue: #887 | Phase: refine

## Problem Statement

The checkpoint system (`egg-checkpoint` CLI) captures rich cross-agent context — transcripts, tool calls, files touched, token usage — but agents rarely use it. The infrastructure works and documentation exists, but nothing in agent prompts or handoff data tells agents to look at checkpoints. Agents only discover checkpoints if they happen to read the right docs, which they typically don't.

The desired outcome is that agents (especially Tester, Documenter, Integrator) are aware of checkpoints as a context source and use them for handoff discovery, error recovery, and pipeline visibility — without agents needing to stumble onto the documentation.

## Current Behavior

### What exists today

1. **`egg-checkpoint` CLI** — fully functional with `list`, `show`, `browse`, `context`, `cost` commands and multi-dimensional filtering (by issue, pipeline, agent-type, phase, status)
2. **Claude Code rule** — `sandbox/.claude/rules/checkpoint.md` (62 lines) is loaded into every agent's context via the rules system, providing CLI reference and common workflows
3. **Documentation guide** — `docs/guides/checkpoint-access.md` (220+ lines) with detailed examples

### What's missing

1. **Agent mode commands** — `tester-mode.md`, `integrator-mode.md`, `documenter-mode.md`, and `coder-mode.md` have zero checkpoint references. They tell agents to read `.egg-state/agent-outputs/` JSON files but never mention `egg-checkpoint` as a complementary context source.

2. **Orchestrator-generated prompts** — `_build_agent_prompt()` and `_build_phase_prompt()` in `orchestrator/routes/pipelines.py` don't mention checkpoint browsing. For execution roles (tester, documenter, integrator), the `_build_role_context()` function (line 1199) provides pointers to handoff data and `git diff`, but no checkpoint discovery hints.

3. **Mission rules** — `sandbox/.claude/rules/mission.md` describes the workflow as "Gather Context → Plan → Implement → Test → Commit & PR" and lists context sources (repo docs, confluence, JIRA, Slack) but doesn't mention checkpoints as a context-gathering tool.

4. **Handoff data** — The `AgentOutput` model in `orchestrator/handoffs.py` has fields for `commit`, `files_changed`, `handoff_data`, `logs`, and `metrics`, but no `checkpoint_id` field. Downstream agents have no structured way to know which checkpoint to review.

5. **Error recovery** — When a phase fails and reruns, the revision-cycle prompt (`_build_phase_scoped_prompt()`, line 2678) includes reviewer/tester feedback but doesn't suggest checking prior failed checkpoints via `egg-checkpoint list --status failed`.

### How handoff data currently flows

The orchestrator sets `EGG_HANDOFF_DATA` as an environment variable (JSON string) when spawning agents. The data comes from `.egg-state/agent-outputs/{role}-output.json` files via `collect_handoff_data()` in `orchestrator/handoffs.py`. This is a structured summary of what the previous agent did, but lacks the full session context (tool calls, reasoning, files explored) that checkpoints provide.

## Constraints

- **Token budget**: Agent mode commands and rules are loaded into every session. Adding checkpoint instructions increases baseline token usage. Each role's mode command is currently 73-153 lines.
- **Relevance filtering**: Not every agent needs the same checkpoint workflows. Tester needs coder's work; Integrator needs pipeline overview; Documenter needs files changed. Generic instructions waste tokens.
- **Rules already loaded**: `checkpoint.md` (62 lines) is already loaded as a Claude Code rule for every agent. The issue is that agents don't know *when* or *why* to use it, not that they lack the CLI reference.
- **Orchestrator prompt size**: The orchestrator already builds multi-KB prompts. Each role's prompt section in `_build_agent_prompt()` is ~20-40 lines. Adding checkpoint hints needs to be concise.
- **Handoff schema stability**: The `AgentOutput` class and handoff JSON files are consumed by multiple parts of the system (orchestrator, agents, reviewers). Adding fields must be backward-compatible.
- **Slash command discovery**: Agents don't automatically run slash commands. A `/checkpoint-discovery` command would only help if agents are explicitly told to invoke it or if a human manually triggers it.
- **Checkpoint availability**: Checkpoints are written when an agent session completes. For the first agent in a pipeline (Coder), there are no prior checkpoints to discover. Checkpoint hints are only useful for downstream agents.
- **`egg-agent-context` scope**: Creating a new CLI wrapper (`egg-agent-context`) adds maintenance burden and a new tool for agents to learn. It may duplicate what `egg-checkpoint context` already provides.

## Options Considered

### Option A: Prompt-only changes (agent mode commands + orchestrator prompts + mission.md)

**Approach**: Add role-specific checkpoint hints to each agent mode command file, add a one-liner to `mission.md`, and inject checkpoint discovery hints into `_build_agent_prompt()` for downstream roles. No code changes to handoff data or new tooling.

**Changes**:
- `tester-mode.md`: Add section "Review prior work" with `egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder`
- `integrator-mode.md`: Add `egg-checkpoint cost` and `egg-checkpoint context --files` hints
- `documenter-mode.md`: Add `egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files` hint
- `mission.md`: Add checkpoint to context sources table
- `orchestrator/routes/pipelines.py`: Add 2-3 line checkpoint hints in `_build_agent_prompt()` for tester/documenter/integrator roles; add failed-checkpoint hint in revision-cycle prompts

**Pros**:
- Highest leverage: orchestrator prompts reach every agent session automatically
- Low risk: only text changes to prompt templates and markdown files
- No schema migrations or backward-compatibility concerns
- Builds on existing `checkpoint.md` rule (already loaded) — just tells agents *when* to use it

**Cons**:
- No structured checkpoint linking (agents must query by pipeline/issue, not by exact ID)
- Slightly increases prompt token usage per session

### Option B: Prompt changes + handoff data enrichment (checkpoint_ids in agent output)

**Approach**: Everything in Option A, plus extend the `AgentOutput` model to include a `checkpoint_ids` field. When an agent session completes, the orchestrator stores the checkpoint ID in the output JSON. Downstream agents receive exact checkpoint IDs in `EGG_HANDOFF_DATA`.

**Changes**:
- All changes from Option A
- `orchestrator/handoffs.py`: Add optional `checkpoint_ids: list[str]` field to `AgentOutput`
- `orchestrator/routes/pipelines.py` or `orchestrator/multi_agent.py`: After agent session completes, write checkpoint ID to the agent's output file
- Agent mode commands: Reference `checkpoint_ids` from handoff data for direct `egg-checkpoint show`

**Pros**:
- Agents get exact checkpoint IDs without needing to query
- Reduces latency of checkpoint discovery (no list + filter step)
- Structured, machine-readable linking between sessions

**Cons**:
- Requires knowing the checkpoint ID at session completion time, which depends on how/when checkpoints are written (they may be written asynchronously by the gateway)
- Schema change to `AgentOutput` — backward-compatible but requires coordination
- More moving parts to debug if checkpoint IDs are missing or stale

### Option C: Full suite — prompts + handoff data + `egg-agent-context` wrapper + slash command

**Approach**: Everything in Options A and B, plus create an `egg-agent-context` convenience wrapper that auto-fetches and summarizes prior agent checkpoints for the current pipeline, and a `/checkpoint-discovery` slash command for interactive sessions.

**Changes**:
- All changes from Options A and B
- New CLI tool `egg-agent-context` (Python script in `bin/`)
- New slash command `sandbox/.claude/commands/checkpoint-discovery.md`
- Revision-cycle prompts include mini-summary of prior checkpoint context (files touched, tool call count)

**Pros**:
- Most comprehensive discovery surface
- `egg-agent-context` reduces cognitive load (agents run one command instead of composing `egg-checkpoint` queries)
- Slash command useful for human-initiated interactive sessions

**Cons**:
- Highest implementation cost and maintenance burden
- `egg-agent-context` may be redundant with `egg-checkpoint context`
- Slash command only helps when explicitly invoked — agents in pipeline mode don't run slash commands
- Risk of over-engineering: the core problem is lack of prompting, not lack of tooling

## Recommended Approach

**Option A (prompt-only changes)** is recommended, with a structured path to adopt Option B later if needed.

The core problem is straightforward: agents don't know checkpoints exist because nothing in their prompts tells them. The checkpoint CLI already works. The rules file is already loaded. The fix is to add targeted, role-specific hints at the two highest-leverage injection points:

1. **Orchestrator prompts** (`_build_agent_prompt()`) — reaches every agent automatically, no opt-in needed
2. **Agent mode commands** — provides workflow-specific guidance when agents activate their role

This avoids schema changes, new tooling, and additional maintenance burden. If checkpoint discovery proves valuable in practice (agents actually use it), extending the handoff schema with checkpoint IDs (Option B) becomes a natural follow-up.

The `egg-agent-context` wrapper (Option C) is premature — `egg-checkpoint context --pipeline $EGG_PIPELINE_ID` already does what it would do. The slash command is low-value since agents in pipeline mode don't invoke slash commands.

## Open Questions

### 1. Should coder-mode.md also get checkpoint hints?

The issue specifically mentions tester, integrator, and documenter modes. The coder is typically the first agent and has no prior checkpoints to review (except in revision cycles). However, in Tier 3 multi-phase execution, a Phase 2 coder could benefit from seeing Phase 1's checkpoint. Should we add a conditional hint for coders in multi-phase pipelines, or keep coder-mode.md unchanged?

### 2. How specific should orchestrator prompt hints be?

The orchestrator prompts could range from a single line ("Use `egg-checkpoint` to review prior agent sessions") to a multi-line block with role-specific commands. More specific hints are more useful but consume more tokens on every session. Given that `checkpoint.md` (62 lines) is already loaded as a rule, should the orchestrator hints be:
- (a) A brief nudge ("Review prior work via egg-checkpoint — see the checkpoint rule for details")
- (b) Role-specific one-liners ("Run `egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder` to see coder's work")
- (c) A full workflow block per role (3-5 lines with multiple commands)

### 3. Should we add checkpoint hints to revision-cycle prompts only, or all cycles?

On first-cycle runs, downstream agents (tester, documenter) may not have prior checkpoints to review. On revision cycles, there are always prior checkpoints. Adding hints only to revision cycles would reduce token waste on first runs. But for Tier 2/3 pipelines, there may be checkpoints from analysis/plan phases even on first implement cycle. Should hints appear on all cycles or only revision cycles?

### 4. Should the error-recovery hint be in the orchestrator prompt or the agent mode command?

The issue suggests adding `egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed` to prompts when a phase fails and reruns. This could go in:
- The orchestrator's revision-cycle prompt (automatic, but only for pipeline-managed retries)
- The agent mode commands (always visible, but less context-aware)
Both? Only one?

### 5. Is the current `checkpoint.md` rule sufficient as the CLI reference?

The existing `sandbox/.claude/rules/checkpoint.md` (62 lines) is loaded into every agent session and covers all CLI commands, filtering, and common workflows. The proposed changes add *when* to use checkpoints but rely on this rule for *how*. Is the current rule content adequate, or does it need updates to better support the new discovery patterns?

### 6. What is the priority ordering of the 8 items in the issue?

The issue lists 8 changes spanning agent mode commands, orchestrator prompts, handoff data, and new tooling. Should we implement all 8 in this issue, or scope down to the highest-leverage items (items 1-4) and defer items 5-8 (slash command, handoff enrichment, revision summaries, `egg-agent-context`) to a follow-up? The recommended approach (Option A) covers items 1-4 but explicitly defers 5-8.

---

*Authored-by: egg*

<!-- HITL decisions are created via egg-contract and posted as GitHub comments -->

```yaml
# metadata
complexity_tier: mid
```
