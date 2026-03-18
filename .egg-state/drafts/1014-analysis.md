# Analysis: PR-based workflows — coordinator PR-seeded tasks and babysit-pr review/fix loop

> Issue: #1014 | Phase: refine

## Problem Statement

The egg platform currently operates through issue-driven SDLC pipelines: a user files a GitHub issue, `egg-sdlc` creates a pipeline, agents progress through REFINE → PLAN → IMPLEMENT → PR phases, and a PR is produced. There is no mechanism to start from an existing PR or to autonomously monitor and fix a PR through its review/CI lifecycle.

Two related capabilities are needed:

1. **PR-seeded coordinator session** — A user points the coordinator at an existing PR. The coordinator reads the PR context (title, description, diff, CI status, reviews), assesses what work remains, and spawns agents as needed to complete the PR. This depends on the conversational coordinator (#1028).

2. **`babysit-pr` review/fix loop** — A standalone command that monitors a PR, automatically fixes CI failures, resolves merge conflicts, runs code reviews, and addresses review feedback — looping until the PR is merged or a timeout/escalation is reached. This replicates the workflow demonstrated in PR #1011 (lint fix → retry → escalate → review → feedback → merge).

Both workflows reuse existing agents, gateway enforcement, and shared prompts. No new agent roles are required.

## Current Behavior

### Existing PR Automation (GitHub Actions)

The codebase already has robust PR automation via GitHub Actions workflows:

- **Check fixing** (`reusable-check-fixer.yml`): Triggers on CI failures, applies non-LLM fixes first (ruff, shfmt), then falls back to LLM agent. Tracks retries via `<!-- egg-autofix-state -->` markers. Escalates after max retries (default 3).
- **Code review** (`reusable-review.yml`): Triggers on PR open/sync, waits for CI, builds review prompt from trusted main branch, posts review via `gh pr review`. Tracks reviewed commits via `<!-- egg-automated-review -->` markers.
- **Conflict resolution** (`reusable-conflict-resolve.yml`): Triggers on push to main, finds PRs with `mergeable_state: dirty`, resolves conflicts via LLM agent.
- **Feedback addressing** (`on-review-feedback.yml`): Triggers on review submission or @mention, addresses feedback on bot-authored PRs, caps at 5 rounds to prevent infinite loops.

These workflows operate **independently as event-driven GitHub Actions** — each is triggered by a specific GitHub event and runs in isolation. There is no unified loop that coordinates them into a continuous review/fix cycle.

### Orchestrator Pipeline Model

The orchestrator manages fixed four-phase pipelines (`orchestrator/models.py`):
- Pipeline creation accepts `issue_number`, `repo`, `branch`, `config`, `prompt`, `network_mode`
- Pipelines are identified as `issue-{N}` (no PR-based pipeline ID)
- Branch naming convention is `egg/issue-{N}` (issue-centric, not PR-centric)
- Auto PR creation happens in the final PR phase via `_auto_create_pr()` in `orchestrator/routes/pipelines.py`

### Gateway Branch Enforcement

The gateway (`gateway/policy.py`) allows agents to push to:
- Branches prefixed with `egg-` or `egg/` (bot-owned)
- Any branch with an open PR authored by the bot identity or trusted users
- Protected branches (`main`/`master`) are always blocked

**Key insight for PR-seeded workflows**: The gateway already supports pushing to PR branches if the bot has an open PR on them. For `babysit-pr`, the gateway would need to allow pushing to the PR's existing head branch — which it already does if the PR was authored by the bot or a trusted user.

### Cross-Agent Communication (#1027, merged)

The concurrent execution infrastructure (`orchestrator/concurrent_executor.py`, `orchestrator/message_store.py`, `orchestrator/peer_consensus.py`) provides:
- Real-time inter-agent messaging via orchestrator REST API
- BRC consensus protocol for coordinated completion
- Long-lived agent containers that persist across review cycles
- Redis or in-memory message store backends

### Coordinator (#1028, analysis complete, not yet implemented)

The coordinator analysis recommends Option A: an embedded coordinator running as a privileged agent container with tools to spawn other agents via orchestrator APIs. It would use an MCP server as its tool interface. The coordinator is **not yet implemented** — #1014's PR-seeded workflow depends on it.

### Shared Components

| Component | Location | Reuse Path |
|-----------|----------|------------|
| Check fixer config | `shared/check-fixers.yml` | Direct reuse — per-job fix commands, retry limits, model selection |
| Check fixer prompt | `action/build-check-fixer-prompt.sh` | Needs Python wrapper or subprocess call |
| Review prompt | `action/build-review-prompt.sh` | Needs Python wrapper or subprocess call |
| Review criteria | `shared/prompts/code-review-criteria.md` | Direct reuse |
| Autofixer rules | `shared/prompts/autofixer-rules.md` | Direct reuse |
| Retry markers | `<!-- egg-autofix-state -->` | Direct reuse |
| Review markers | `<!-- egg-automated-review -->` | Direct reuse |

## Constraints

### Hard Dependencies

- **#1028 (Coordinator)**: The PR-seeded workflow is a coordinator capability. The coordinator must be implemented first or concurrently. `babysit-pr` can be built independently as a simpler standalone loop, but full PR-seeded pipelines require the coordinator's dynamic agent orchestration.
- **#1027 (Cross-agent communication)**: Merged. Available for use.

### Technical Constraints

- **Gateway branch enforcement**: Agents in `babysit-pr` must be allowed to push to the PR's head branch. The gateway already supports this for bot-authored or trusted-user PRs, but non-trusted-user PRs would require either (a) the gateway `GATEWAY_TRUSTED_USERS` to include the PR author, or (b) the bot to have its own PR on the same branch.
- **Prompt security**: Review and fix prompts must be built from the trusted main branch checkout (not the PR branch) to prevent prompt injection — this is the existing pattern in GitHub Actions.
- **Concurrent push safety**: If the PR author pushes while `babysit-pr` is running, merge conflicts can occur. The loop must handle or detect concurrent pushes.
- **Context window limits**: A long-running `babysit-pr` session (hours) will exhaust Claude's context window. The loop should use short-lived agent sessions for each fix/review cycle rather than a single persistent session.
- **GitHub API rate limits**: Polling PR state, CI status, and reviews requires GitHub API calls. At high frequency, this could hit rate limits (5000/hour for authenticated GitHub Apps).

### Scope Constraints

- **No new agents**: Both workflows reuse existing check fixer, reviewer, conflict resolver, and feedback responder roles.
- **No GitHub Actions changes**: The existing workflows continue operating independently. `babysit-pr` orchestrates the same operations from within the sandbox rather than being event-driven.
- **Gateway enforcement preserved**: All git/gh operations go through the gateway sidecar. No bypass.

### Security Constraints

- **Read-only reviewer**: The reviewer agent must remain read-only (cannot push code) — consistent with existing `reusable-review.yml` behavior.
- **Credential isolation**: Each agent session gets its own gateway registration. No shared tokens between fixer and reviewer.

## Options Considered

### Option A: babysit-pr as a Coordinator Capability Only

**Approach**: Implement `babysit-pr` exclusively as a coordinator (#1028) sub-task. The user invokes `egg babysit-pr <PR>`, which starts a coordinator session scoped to the review/fix loop. All logic lives in the coordinator's decision-making.

**Pros**:
- Single implementation path — no parallel codebase
- Coordinator's dynamic judgment handles escalation decisions naturally
- Unified interface for all agent orchestration
- Can reuse coordinator's agent spawning, health monitoring, and messaging

**Cons**:
- Blocked on #1028 implementation (coordinator not yet built)
- Heavier startup: coordinator container + per-cycle agent containers
- Overkill for a focused fix/review loop — the coordinator brings generality that isn't needed here
- If the coordinator has bugs, `babysit-pr` is unavailable

### Option B: babysit-pr as a Standalone Loop Module + Coordinator Integration

**Approach**: Build the review/fix loop as a self-contained Python module (e.g., `shared/egg_babysit/`) that encapsulates the loop logic: poll PR state → check conflicts → wait for CI → fix failures → review → address feedback → loop. The module is consumed by:
1. A standalone CLI command (`egg babysit-pr <PR>`) that runs the loop directly
2. The coordinator (#1028), which calls the same module as a sub-task when it enters babysit mode

**Pros**:
- Standalone CLI available immediately — no dependency on #1028
- Shared module ensures consistency between CLI and coordinator paths
- Simpler for the common case (user just wants to babysit a PR)
- Easier to test in isolation
- Coordinator can adopt it later without reimplementing

**Cons**:
- Two invocation paths to maintain (CLI + coordinator)
- Standalone CLI lacks coordinator's adaptive judgment (hardcoded retry limits vs. dynamic escalation decisions)
- Module interface must be designed to work both standalone and as a coordinator sub-task

### Option C: babysit-pr as an Orchestrator Pipeline Mode

**Approach**: Extend the existing orchestrator pipeline model with a new `babysit` mode. Create a pipeline with `mode: "babysit"` and `pr_number: N`. The orchestrator manages the loop as a specialized pipeline with its own phase sequence.

**Pros**:
- Leverages existing pipeline infrastructure (state persistence, health monitoring, SSE streaming, container lifecycle)
- Checkpointing and recovery come free
- Observable via existing `egg-sdlc` CLI and orchestrator dashboard
- Natural fit with the orchestrator's role as the execution engine

**Cons**:
- Pipeline model is designed for linear phase progression; a loop doesn't fit naturally
- Adds complexity to the orchestrator's already-substantial pipeline code
- Pipeline ID model (`issue-{N}`) doesn't naturally accommodate PR-based pipelines
- Over-engineers what could be a simpler loop

## Recommended Approach

**Option B: Standalone Loop Module + Coordinator Integration** is recommended.

**Justification**:
1. **Unblocks delivery**: The standalone CLI can ship without waiting for #1028 (coordinator). Given that #1028 is still in analysis, this avoids a potentially long dependency chain.
2. **Correct abstraction**: The review/fix loop is a well-defined, self-contained algorithm. Encapsulating it as a module makes it testable, reusable, and composable. The coordinator can call it without reimplementation.
3. **Matches the issue's design**: The issue explicitly describes `babysit-pr` as operable in both standalone CLI and coordinator sub-task modes. Option B directly implements this dual-mode design.
4. **Incremental complexity**: Start with the standalone loop (simpler), then integrate with the coordinator (adds dynamic judgment). Each step delivers value.

For the **PR-seeded coordinator session** (workflow 1 in the issue), this remains a coordinator capability that depends on #1028. The analysis below focuses primarily on `babysit-pr` since it can be implemented independently.

### Orchestrator Integration for Standalone Mode

The standalone CLI should still register with the orchestrator for observability:
- Create a pipeline with `pipeline_id: pr-{N}`, `mode: babysit`
- Report progress via `egg-orch signal` / `egg-orch progress emit`
- Use the orchestrator's container spawner for agent sessions
- This gives checkpointing, health monitoring, and SSE streaming without blocking on the coordinator

## Key Design Decisions

### Loop Architecture

The `babysit-pr` loop would follow this sequence (per the issue):

```
1. Check merge conflicts → fixer resolves
2. Wait for CI checks → poll and stream status
3. Fix failing checks → non-LLM first, then LLM agent
4. Wait for CI checks → poll after fix push
5. Review PR → reviewer posts GitHub review (read-only)
6. Address review feedback → fixer addresses and pushes
7. Loop back to step 1
8. Exit when PR is merged or timeout/escalation reached
```

Each step spawns a short-lived agent session (not a persistent Claude session) to stay within context limits. State is maintained in the loop module, not in the agent's conversation context.

### Agent Session Model

Two agent roles per cycle:
- **Fixer** (read-write): Resolves conflicts, fixes CI failures, addresses review feedback. Pushes to PR branch.
- **Reviewer** (read-only): Posts code review to GitHub. Cannot push.

These reuse existing shared prompts (`code-review-criteria.md`, `autofixer-rules.md`, `check-fixers.yml`) and prompt builders (`build-check-fixer-prompt.sh`, `build-review-prompt.sh`).

### State Tracking

The loop maintains state across iterations:
- Current iteration count (for max-iterations exit condition)
- CI check attempts per job (for escalation after max retries)
- Review round count (for feedback loop cap)
- PR merge status (for success exit)

State could be stored in a pipeline state file (`pr-{N}.json`) or in-memory with crash recovery via git-based persistence.

### Prompt Consumption Strategy

The existing prompt builders are bash scripts. The Python loop module needs to consume them. The options (already surfaced as a decision on the issue) are:
1. Call bash scripts via subprocess
2. Port to Python
3. Thin Python wrappers around bash scripts

This is an open decision requiring human input.

## Open Questions

The following questions require human decisions. Each is registered via `egg-contract` below.

### Q1: babysit-pr Execution Model

Should `babysit-pr` use the orchestrator for lifecycle management, or run as a standalone process?

- **Orchestrator-backed**: Checkpoints, container lifecycle, health monitoring built in; slower startup
- **Standalone**: Faster, simpler loop; no checkpoints or orchestrator observability
- **Both**: Orchestrator by default, `--standalone` flag for lightweight mode

*Previously posted as issue comment — re-registering in contract.*

### Q2: Prompt Builder Strategy

The current prompt builders are bash scripts (`action/build-*.sh`). How should the Python loop module consume them?

- **Call existing bash scripts via subprocess**: Minimal change, single source of truth
- **Port to Python modules**: Cleaner integration, but duplicates logic until Actions are updated
- **Thin Python wrappers that call bash scripts**: Python API, bash implementation underneath

*Previously posted as issue comment — re-registering in contract.*

### Q3: PR Branch Handling for PR-Seeded Pipelines

When the coordinator manages a PR-seeded pipeline, should agents push to the PR's existing head branch or create a new branch?

- **Push to existing PR head branch**: Simpler, but risks conflicts with PR author's ongoing work
- **Create new branch and update PR head**: Safer isolation, but changes the PR's branch
- **Use existing branch with expectation of no concurrent pushes**: Simple, relies on coordination

*Previously posted as issue comment — re-registering in contract.*

### Q4: babysit-pr Scope — Conflict Resolution

Should `babysit-pr` include merge conflict resolution (step 1 in the loop), or defer conflict resolution to v2?

- **Yes, include conflict resolution**: Full loop as described in issue
- **No, defer to v2**: Simpler v1: CI wait → check fix → review → feedback only

*Previously posted as issue comment — re-registering in contract.*

### Q5: Terminal UX — Streaming vs Polling Output

What level of terminal UX is expected for the `babysit-pr` CLI in v1?

- **Minimal**: Periodic status line updates (poll and print)
- **Medium**: Rich terminal output with progress bars and color (like egg-sdlc's DAG view)
- **Full**: Live-streaming agent output to terminal

*Previously posted as issue comment — re-registering in contract.*

### Q6: Review-Only vs Review-and-Fix

Should babysit-pr always pair review with auto-fix, or support a review-only mode?

- **Always fix**: Review always followed by auto-fix attempt
- **Optional review-only**: `--review-only` flag to post review and stop
- **Both by default**: Auto-fix with `--no-fix` flag to opt out

*Previously posted as issue comment — re-registering in contract.*

### Q7: Non-Trusted-User PR Handling

The gateway currently allows pushing to PR branches if the PR was authored by the bot or a trusted user. For `babysit-pr` on a PR authored by someone else (not in `GATEWAY_TRUSTED_USERS`), how should this be handled?

- **Require trusted user**: Only babysit PRs from trusted users or bot-authored PRs
- **Add bot as co-author**: Bot opens its own PR to the same branch, gaining push rights
- **Gateway policy extension**: Add a new `babysit` permission mode allowing push to any branch with an open PR where babysit was explicitly requested

### Q8: Dependency Sequencing — Ship babysit-pr Before Coordinator?

The issue describes both workflows as part of one unit, but `babysit-pr` can ship independently while the PR-seeded coordinator session requires #1028. Should we:

- **Ship babysit-pr standalone first**: Deliver value immediately, integrate with coordinator later
- **Wait for coordinator**: Implement both together to avoid rework
- **babysit-pr standalone + PR-seeded as coordinator milestone**: Treat as two separate deliverables

---

## Complexity Assessment

**High**

This issue spans multiple components (orchestrator pipeline model, gateway branch policy, shared prompt modules, new CLI command, new Python module, coordinator integration), has hard dependencies on #1028 and #1027, involves async agent lifecycle management, and requires design decisions on execution model, state management, and security policy. The `babysit-pr` loop alone is medium complexity, but the full scope (including PR-seeded coordinator sessions) is high.

---

*Authored-by: egg*
