# Analysis: Replace GHA review workflows with orchestrator-based babysit-pr pipelines

> Issue: #1278 | Phase: refine

## Problem Statement

The repository currently has eight GitHub Actions workflows that handle PR automation (code review, contract verification, agent-mode design review, review feedback addressing, CI check fixing, and merge conflict resolution). These workflows invoke agents via `gha_exec()`, which uses `claude --print` — a path that:

1. **Lacks structured error handling** — `claude --print` exits with code 1 and no stderr, giving zero diagnostic information
2. **Creates two divergent execution paths** — orchestrator pipelines use the Agent SDK, while GHA workflows use `claude --print` via `gha_exec()`
3. **Contradicts the EGG100 linter rule** — EGG100 flags `claude --print` as deprecated, but the GHA action itself uses it

The desired outcome is to replace all eight GHA workflows with a single babysit-pr cycle triggered on each push to a PR branch, using the orchestrator and Agent SDK exclusively.

## Current Behavior

### GHA Workflow Architecture

Eight workflows handle distinct PR automation concerns, all routing through `reusable-review.yml` → `action/entrypoint.sh` → `gha_exec()` → `claude --print`:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `on-pull-request.yml` | PR opened/sync | Base code review |
| `reusable-review.yml` | Called by others | Template workflow for all reviews |
| `on-pull-request-contract-verify.yml` | PR with `sdlc:pr` label or contract files | Contract verification |
| `on-pull-request-agent-mode-design.yml` | Changes to action/workflows/sandbox/prompts | Agent-mode design review |
| `on-review-feedback.yml` | Review submitted or @mention | Address review feedback |
| `reusable-check-fixer.yml` | Called by on-check-failure | Fix CI failures |
| `reusable-conflict-resolve.yml` | Called by on-merge-conflict | Resolve merge conflicts |
| `on-merge-conflict.yml` | Push to main or cron | Detect and dispatch conflict resolution |

Six bash prompt builder scripts (`action/build-*.sh`) generate agent prompts from trusted `main` branch sources.

The `gha_exec()` function in `sandbox/egg_lib/cli.py` (line 223) orchestrates container lifecycle: creates Docker networks, starts the gateway sidecar, builds the `claude --print` command, and executes in a sandbox container.

### Existing babysit-pr Implementation

The `shared/egg_babysit/` package is **fully implemented** and provides:

- **`loop.py`**: Complete state machine: `CHECK_CONFLICTS → WAIT_CI → FIX_CHECKS → WAIT_CI → REVIEW → ADDRESS_FEEDBACK → (loop or exit)`
- **`fixer.py`**: Agent spawner using `egg_agent.build_agent_command()` (Agent SDK, not `claude --print`)
- **`reviewer.py`**: Read-only reviewer agent spawner
- **`prompts.py`**: Pure Python prompt builders (already replaces bash scripts functionally)
- **`pr_state.py`**: PR metadata polling via `gh` CLI
- **`escalation.py`**: Multi-channel HITL escalation (GitHub comments, orchestrator decisions, Slack)
- **`ci_waiter.py`**: CI polling with stale check detection
- **`steps/`**: Modular step implementations (conflict, check_fix, review, feedback)

The loop currently runs **sequentially** — one agent at a time (fixer or reviewer), not concurrent BRC.

### Key Differences Between Current and Proposed

| Aspect | Current (GHA) | Current (babysit-pr) | Proposed |
|--------|---------------|---------------------|----------|
| Execution | `claude --print` | Agent SDK | Agent SDK |
| Coordination | GHA event chaining | Sequential loop | Concurrent BRC |
| Agent model | One agent per workflow | One agent at a time | Fixer + reviewer concurrent |
| Prompt building | Bash scripts | Python modules | Python modules |
| Trigger | GHA events (push, review, check) | Manual / API | Single push trigger |
| State tracking | PR comment markers | Loop state object | BRC consensus state |

## Constraints

- **Dependencies satisfied**: Both #1014 (babysit-pr loop) and #1027 (cross-agent communication) are closed/implemented
- **Security**: Prompt building from trusted `main` branch must be preserved — the current bash scripts read from main to prevent malicious PR-branch prompt injection. The Python `prompts.py` already reads `check-fixers.yml` from the base branch via `git show`
- **Concurrency control**: The `reusable-review.yml` uses GHA concurrency groups (`egg-$bot_name-$pr_number`) to prevent duplicate runs. The orchestrator pipeline must provide equivalent deduplication
- **Review marker compatibility**: Other systems may rely on `<!-- egg-automated-review bot=... commit=... verdict=... -->` markers for deduplication. Must either preserve marker format or update consumers
- **Stale review dismissal**: Current workflows dismiss stale reviews before posting new ones — babysit-pr must replicate this
- **on-check-failure.yml**: Not listed in the 8 workflows being replaced, but it calls `reusable-check-fixer.yml` which IS being deleted. This workflow must also be addressed
- **Workflow file count**: The issue lists 8 workflows, but `on-check-failure.yml` is a 9th that depends on `reusable-check-fixer.yml`. Its fate must be decided
- **Review criteria consolidation**: The reviewer agent must combine the criteria from three separate reviews (base code, contract verification, agent-mode design) into a single review role. Must ensure no review criteria are lost

## Options Considered

### Option A: Concurrent BRC (as described in issue)

**Approach**: Restructure the babysit-pr loop to run fixer and reviewer agents concurrently using BRC consensus. Both agents iterate together — the fixer resolves issues while the reviewer evaluates until they reach consensus that the PR is clean.

**Pros**:
- Matches the issue's chosen direction exactly
- Natural fit for the existing BRC infrastructure (orchestrator message bus, consensus protocol)
- Potentially faster — reviewer can start evaluating while fixer is still working
- Single execution model for all agent work (orchestrator + Agent SDK)

**Cons**:
- Significant refactoring of the existing sequential `BabysitLoop` state machine
- Concurrent agents on the same branch create race conditions (fixer pushes while reviewer reads)
- BRC consensus adds complexity — must handle NACK cycles, timeout, agent failures
- The current babysit-pr loop is production-tested; concurrent BRC is a new execution model for this use case
- More expensive — two agents running simultaneously doubles token costs even when one is idle

### Option B: Sequential loop with push trigger (minimal change)

**Approach**: Keep the existing sequential `BabysitLoop` as-is. Add a single GHA workflow that triggers `egg-babysit <PR>` on push to a PR branch. Delete the 8 old workflows and bash scripts.

**Pros**:
- Minimal code changes — existing babysit-pr loop is production-tested
- All issue goals achieved: single execution path, Agent SDK only, EGG100 resolved, bash scripts removed
- Lower risk — no architectural change to the loop itself
- Lower cost — one agent at a time

**Cons**:
- Doesn't leverage BRC consensus for fixer/reviewer coordination
- Doesn't match the issue's "concurrent agent pipeline with BRC consensus" specification
- Sequential execution may be slower for complex PRs

### Option C: Hybrid (sequential steps, concurrent review/feedback)

**Approach**: Keep conflict resolution and CI fixing sequential. Make the review + feedback-addressing step concurrent: spawn fixer and reviewer simultaneously, use BRC consensus for the review/feedback cycle only.

**Pros**:
- Targets concurrency where it adds most value (review iteration)
- Avoids race conditions during CI fixing (only one agent pushes at a time during fixes)
- Partial BRC adoption — lower complexity than full concurrent
- Natural fit: reviewer evaluates while fixer addresses feedback

**Cons**:
- Hybrid model is more complex to understand than pure sequential or pure concurrent
- Still requires BRC integration for the review/feedback step
- Doesn't match the issue description exactly

## Recommended Approach

**Option A: Concurrent BRC** — this matches the issue's explicit chosen direction and leverages the infrastructure that #1027 already built. The issue author has clearly thought through the execution model (fixer = read-write, reviewer = read-only, iterate until consensus). The babysit-pr loop needs to be restructured as a concurrent agent pipeline rather than a sequential state machine, but the building blocks (Agent SDK, BRC protocol, message bus) are all in place.

The key risk is race conditions between fixer pushes and reviewer reads, but this is mitigated by the reviewer being read-only (no push permissions) and the BRC protocol's NACK mechanism allowing the reviewer to request re-evaluation after a push.

## Open Questions

> **Note**: Contract registration via `egg-contract add-decision` / `egg-contract add-feedback` failed because the contract for issue #1278 has not been initialized by the orchestrator. The questions below should be registered as HITL decisions once the contract is available.

### Decision 1: Concurrent vs. Sequential Architecture

**Question**: Should the babysit-pr cycle use concurrent BRC agents (fixer + reviewer) or keep the current sequential loop architecture?

**Options**:
- **A) Concurrent BRC** — Fixer + reviewer iterate with consensus as described in the issue. Requires restructuring the loop as a concurrent agent pipeline.
- **B) Sequential loop** — Keep the current sequential architecture, just trigger it from a GHA push event. Achieves all other goals (single path, Agent SDK, EGG100 fix) with minimal refactoring.
- **C) Hybrid** — Sequential for conflict/CI steps, concurrent BRC for review/feedback cycle only.

### Decision 2: Trigger Mechanism

**Question**: What should trigger the babysit-pr cycle on push to a PR branch?

**Options**:
- **A) New GHA workflow** — A single `on-push-babysit.yml` that calls the orchestrator API to create a babysit pipeline on push. Leverages existing GHA infrastructure for event handling.
- **B) Direct orchestrator webhook** — Orchestrator listens for GitHub push webhooks directly, bypassing GHA entirely. Requires new webhook endpoint.
- **C) Modify existing workflow** — Repurpose `on-pull-request.yml` to invoke babysit-pr instead of individual review workflows.

### Decision 3: `on-check-failure.yml` Handling

**Question**: The issue lists 8 workflows to replace, but `on-check-failure.yml` (which calls `reusable-check-fixer.yml`) is not listed. Since `reusable-check-fixer.yml` IS being deleted, what should happen to `on-check-failure.yml`?

**Options**:
- **A) Delete it** — babysit-pr handles check fixing as part of its cycle. No separate check-failure trigger needed.
- **B) Rewire it** — Keep `on-check-failure.yml` but have it trigger a babysit-pr cycle instead of calling the reusable check fixer.

### Decision 4: Review Marker Compatibility

**Question**: Current workflows embed `<!-- egg-automated-review bot=... commit=... verdict=... -->` markers in review comments for deduplication. Should the babysit-pr cycle preserve this marker format?

**Options**:
- **A) Preserve markers** — Include the same HTML comment markers in babysit-pr reviews for backward compatibility.
- **B) Drop markers** — The babysit-pr cycle manages its own state; markers become unnecessary once all workflows are replaced.
- **C) New marker format** — Use a new format that includes BRC consensus state (e.g., cycle number, consensus status).

### Decision 5: Review Criteria Consolidation Strategy

**Question**: The three review workflows (base code, contract verification, agent-mode design) each have distinct review criteria. How should these be consolidated into the single reviewer agent?

**Options**:
- **A) Single combined prompt** — Merge all criteria into one review prompt. The reviewer evaluates all aspects in a single pass.
- **B) Multi-pass review** — The reviewer runs three sequential review passes (code, contract, design) within the same agent session.
- **C) Conditional criteria** — Include contract verification only when `sdlc:pr` label is present; include design review only when relevant file paths changed. Base code review always included.

### Decision 6: Bash Script Deletion Strategy

**Question**: Should the six bash prompt builders be deleted in the same PR as the workflow removal, or preserved temporarily?

**Options**:
- **A) Delete entirely** — One-shot cutover as described in the issue. Clean break.
- **B) Preserve as deprecated** — Keep for one release cycle with deprecation warnings, then delete.

### Feedback Questions

The following open-ended questions need human input:

1. **Non-LLM fix behavior**: The current `reusable-check-fixer.yml` has a two-phase strategy — try non-LLM shell commands first (from `check-fixers.yml`), then fall back to LLM. The babysit-pr loop's `steps/check_fix.py` already implements this. Are there any non-LLM fix behaviors in the GHA workflow that aren't captured in the babysit-pr implementation?

2. **Feedback iteration limits**: The current `on-review-feedback.yml` caps at 5 feedback rounds. The babysit-pr loop also defaults to 5 (`max_feedback_rounds`). Should this remain the default, or should the concurrent model use a different limit since fixer and reviewer iterate together?

3. **Reusable workflow consumers**: Are there external repositories using `reusable-review.yml`, `reusable-check-fixer.yml`, or `reusable-conflict-resolve.yml` as reusable workflows (via `workflow_call`)? If so, the deletion would break them and we'd need a migration path.

4. **`gha_exec()` removal scope**: The issue says to remove `gha_exec()` from `action/entrypoint.sh`. Should the entire `gha_exec()` function in `sandbox/egg_lib/cli.py` also be deleted, or kept for backward compatibility with other potential callers?

5. **Pipeline deduplication**: The GHA workflows use concurrency groups to prevent duplicate runs. How should the orchestrator handle rapid successive pushes to the same PR? Options include: queue and process latest only, debounce, or allow concurrent cycles with last-write-wins.

## Complexity Assessment

**High**. This is a cross-cutting architectural change that:

- Replaces 8 GHA workflow files and 6 bash scripts
- Restructures the babysit-pr loop from sequential to concurrent BRC
- Consolidates three review criteria into one agent role
- Adds a new pipeline trigger mechanism
- Modifies the entrypoint/CLI layer (`gha_exec()` removal)
- Potentially affects external workflow consumers
- Requires end-to-end validation against the cutover PR itself

Multiple independent workstreams (prompt consolidation, BRC refactoring, GHA workflow replacement, trigger mechanism) could be parallelized.

---

*Authored-by: egg*
