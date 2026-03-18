# Babysit-PR Guide

Autonomous review/fix loop that monitors a pull request through its full lifecycle — from CI failures to code review to merge.

## What It Does

`egg-babysit` watches a PR and automatically:

1. **Resolves merge conflicts** — detects dirty mergeable state and spawns a fixer agent
2. **Waits for CI checks** — polls `gh pr checks` at configurable intervals
3. **Fixes failing checks** — tries non-LLM fixes first (e.g., `make lint-fix`), then spawns an LLM fixer agent
4. **Posts code reviews** — spawns a read-only reviewer agent that posts GitHub reviews
5. **Addresses review feedback** — spawns a fixer agent to resolve requested changes
6. **Loops** until the PR is merged, a timeout is reached, or human intervention is needed

This replicates the manual cycle demonstrated in [PR #1011](https://github.com/jwbron/egg/pull/1011), where code was pushed, lint checks failed, the check fixer applied corrections, the reviewer posted feedback, and changes were addressed — all automatically.

## Execution Modes

Babysit-pr supports two execution modes:

### Sequential Mode (Default)

The original execution model. Steps run one at a time: conflict resolution → CI wait → check fix → review → feedback addressing. One agent is active at any given point. This mode is used when `EGG_CONCURRENT_MODE` is not set, or when invoking `egg-babysit` from the CLI.

### Concurrent BRC Mode

When triggered via the orchestrator pipeline (e.g., from the `on-push-babysit.yml` workflow), babysit-pr runs the **review and feedback-addressing phase** with concurrent fixer and reviewer agents coordinating via the [BRC consensus protocol](concurrent-execution.md). The fixer proposes changes; the reviewer ACKs or NACKs. They iterate until consensus or escalation.

Sequential phases (conflict resolution, CI wait, check fixing) remain sequential — concurrency is applied where it adds the most value: the review/feedback iteration cycle.

| Aspect | Sequential | Concurrent BRC |
|--------|-----------|----------------|
| Review cycle | Reviewer runs, then fixer runs | Fixer and reviewer run simultaneously |
| Coordination | State machine transitions | BRC consensus (propose → ACK/NACK → confirmed) |
| Agent communication | None (sequential handoff) | Orchestrator message bus |
| Convergence control | `max_feedback_rounds` (default: 5) | BRC flip-flop cap + `max_consensus_rounds` + consensus timeout |
| Escalation | Feedback round limit exceeded | Consensus timeout → HITL escalation |
| Cost | One agent at a time | Two agents simultaneously during review phase |

See [Concurrent Execution Guide](concurrent-execution.md) for details on the BRC protocol.

## Usage

### Standalone CLI

```bash
# Basic usage — monitors PR #42 until merged or timeout
egg-babysit 42

# Specify repository explicitly
egg-babysit 42 --repo owner/repo

# Custom timeout (default: 4 hours)
egg-babysit 42 --timeout 7200

# Limit loop iterations (default: 10)
egg-babysit 42 --max-iterations 5
```

### As a Coordinator Sub-Task

When using the coordinator (#1028), babysit-pr mode is entered automatically after the implementation phase completes:

```
Coordinator assesses PR → spawns agents → enters babysit-pr mode → loop until merged
```

The coordinator calls `egg_babysit.loop.babysit()` directly, using the same loop logic as the CLI.

## How the Loop Works

### Sequential Mode

```
┌─────────────────────────────────────────────┐
│              BABYSIT-PR LOOP                 │
│                                             │
│  ┌─────────┐   conflicts?   ┌──────────┐   │
│  │  Start   │──────yes──────→│  Fixer   │   │
│  │iteration │                │(conflict)│   │
│  └────┬─────┘       no      └────┬─────┘   │
│       │              │            │          │
│       ▼              ▼            ▼          │
│  ┌──────────┐   ┌──────────┐               │
│  │ Wait CI  │←──│ Wait CI  │               │
│  │ checks   │   │ (re-run) │               │
│  └────┬─────┘   └────┬─────┘               │
│       │               │                     │
│       ▼          fails?                     │
│  all pass?──no──→┌──────────┐               │
│       │          │  Fixer   │               │
│       │          │(check fix)│               │
│      yes         └────┬─────┘               │
│       │               │                     │
│       ▼               ▼                     │
│  ┌──────────┐                               │
│  │ Reviewer │                               │
│  │(read-only)│                               │
│  └────┬─────┘                               │
│       │                                     │
│       ▼                                     │
│  changes    ┌──────────┐                    │
│  requested?─→│  Fixer   │                    │
│       │     │(feedback)│                    │
│      no     └────┬─────┘                    │
│       │          │                          │
│       ▼          ▼                          │
│  ┌──────────┐                               │
│  │ Merged?  │──yes──→ EXIT (success)        │
│  └────┬─────┘                               │
│       │ no                                  │
│       ▼                                     │
│  Loop back to start                         │
└─────────────────────────────────────────────┘
```

### Concurrent BRC Mode

In concurrent mode, the review and feedback phase runs both agents simultaneously:

```
┌──────────────────────────────────────────────────────┐
│           BABYSIT-PR (CONCURRENT BRC)                │
│                                                      │
│  [Sequential phases — same as above]                 │
│  CHECK_CONFLICTS → WAIT_CI → FIX_CHECKS → WAIT_CI   │
│                                                      │
│  [Concurrent review/feedback phase]                  │
│  ┌───────────────────────────────────────────┐       │
│  │          BRC CONSENSUS LOOP               │       │
│  │                                           │       │
│  │  ┌──────────┐       ┌──────────────┐      │       │
│  │  │  Fixer   │◄─────►│  Reviewer    │      │       │
│  │  │(producer)│ msg   │ (reviewer)   │      │       │
│  │  └────┬─────┘ bus   └──────┬───────┘      │       │
│  │       │                    │              │       │
│  │       ▼                    ▼              │       │
│  │   PROPOSE ──────────► ACK/NACK           │       │
│  │       │                    │              │       │
│  │   (if NACK)           (if ACK)            │       │
│  │   fix issues ──► re-PROPOSE → CONFIRMED  │       │
│  │                                           │       │
│  │   Timeout? ──► HITL escalation            │       │
│  │   Flip-flop cap? ──► HITL escalation      │       │
│  └───────────────────────────────────────────┘       │
│                                                      │
│  Loop back to start or EXIT                          │
└──────────────────────────────────────────────────────┘
```

The fixer operates as a BRC **producer** (read-write access, pushes to the PR branch) and the reviewer operates as a BRC **reviewer** (read-only, posts GitHub reviews). They communicate via the orchestrator message bus. HANDOFF messages signal when the fixer pushes new commits, prompting the reviewer to re-evaluate.

### Exit Conditions

| Condition | What Happens |
|-----------|--------------|
| **PR merged** | Loop exits with success status |
| **PR approved + CI passing** | Loop exits with `ready_to_merge` — human or coordinator merges |
| **Timeout** | Loop exits (default: 4 hours). Configurable via `--timeout` |
| **Max iterations** | Loop exits (default: 10). Configurable via `--max-iterations` |
| **HITL escalation** | Loop pauses. GitHub comment posted, Slack notification sent. Resumes after human decision |
| **Unrecoverable error** | Loop exits with error. Human notified |

## Check Fixing Strategy

The check fixer follows a non-LLM-first approach, consistent with the existing [check autofixer workflow](github-automation.md#check-autofixer):

1. **Lookup**: Find the failing job in `shared/check-fixers.yml`
2. **Non-LLM fix**: If a non-LLM fix command exists (e.g., `make lint-fix`), run it first
3. **LLM fix**: If non-LLM fix doesn't resolve the issue, spawn an LLM fixer agent with the failure context
4. **Retry tracking**: Track attempts per job using `<!-- egg-autofix-state -->` markers
5. **Escalation**: After max retries (configured per job in `check-fixers.yml`), escalate to HITL

### check-fixers.yml Reference

The check fixer configuration lives at `shared/check-fixers.yml`. Each entry maps a CI job name to its fix strategy:

```yaml
lint:
  non_llm_fix: "make lint-fix"
  max_retries: 3
  model: "sonnet"

test:
  max_retries: 3
  model: "opus"
  # No non_llm_fix — goes straight to LLM fixer
```

## Orchestrator Integration

When `egg-babysit` runs, it registers a pipeline with the orchestrator:

- **Pipeline ID**: `pr-{N}` (e.g., `pr-42` for PR #42)
- **Mode**: `babysit` (distinct from the standard `issue` mode)
- **State**: Tracks current loop iteration, step (conflict/ci-wait/fix/review/feedback), and per-job retry counts
- **Health monitoring**: The [OverseerMonitor](pipeline-health-monitoring.md) detects stalled loops via progress events
- **Crash recovery**: Loop state is persisted. If the container restarts, it resumes from the last saved position

### BRC Pipeline Registration

When triggered via the `on-push-babysit.yml` workflow, the orchestrator creates a babysit pipeline with BRC agent registration:

- **Fixer agent**: Registered with read-write permissions and `EGG_BRC_ROLE_TYPE=producer`
- **Reviewer agent**: Registered with read-only permissions and `EGG_BRC_ROLE_TYPE=reviewer`
- **Review graph**: Reviewer → Fixer (CRITICAL edge) — the reviewer must ACK the fixer's changes before the cycle completes
- **BRC environment**: Both agents receive `EGG_CONCURRENT_MODE=true`, `EGG_BRC_REVIEWERS`, and `EGG_BRC_PRODUCERS`

### Consensus Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `consensus_timeout_minutes` | `30` | Time before consensus failure triggers HITL escalation |
| `max_consensus_rounds` | `3` | Maximum propose/NACK/re-propose cycles (flip-flop cap) |

## Agent Roles

`babysit-pr` reuses existing agent infrastructure — no new agent types:

| Role | Access | BRC Role | What It Does |
|------|--------|----------|--------------|
| **Fixer** | Read-write: pushes to PR branch | Producer | Resolves conflicts, fixes CI failures, addresses review feedback. Proposes changes via BRC consensus in concurrent mode. |
| **Reviewer** | Read-only: posts GitHub reviews only | Reviewer | Reviews code changes using `shared/prompts/code-review-criteria.md`. ACKs or NACKs fixer proposals in concurrent mode. |

Each agent runs as a short-lived container spawned by the orchestrator's `ContainerSpawner`. This keeps context windows fresh and costs predictable.

### Consolidated Review Criteria

In concurrent mode, the reviewer agent consolidates three review responsibilities that were previously handled by separate GHA workflows:

| Review Domain | When Included | Source |
|---------------|---------------|--------|
| **Base code review** | Always | `shared/prompts/code-review-criteria.md` |
| **Contract verification** | PR has `sdlc:pr` label | `shared/prompts/contract-review-criteria.md` |
| **Agent-mode design review** | Changed files match `action/`, `.github/workflows/`, `sandbox/`, `shared/prompts/` | `shared/prompts/agent-design-criteria.md` |

This conditional inclusion ensures the reviewer evaluates all relevant criteria without wasting context on domains that don't apply to the current PR.

### Status Comment Management

Babysit-pr manages status comments on the PR using `<!-- egg-status-comment -->` HTML markers, matching the format used by the previous GHA workflows. Before posting a new status comment, prior status comments from the same bot are minimized as "OUTDATED" to reduce clutter. Duplicate comments for the same commit SHA are prevented.

## Gateway Requirements

The gateway sidecar enforces branch policies. For `babysit-pr` to push to a PR branch:

- The bot must have an open PR on that branch, **OR**
- The user must be in `GATEWAY_TRUSTED_USERS` / `TRUSTED_BRANCH_OWNERS`

No gateway changes are needed — this uses existing push policies. See the [Architecture Overview](../architecture/README.md) for details on gateway enforcement.

## HITL Escalation

When the loop hits an unrecoverable state:

1. A HITL decision is created via the orchestrator's [DecisionQueue](../reference/orchestrator-cli.md)
2. A GitHub comment is posted on the PR explaining the blocker
3. A Slack notification is sent (if configured)
4. The loop pauses until the decision is resolved

Typical escalation triggers:
- Max retries exhausted for a CI job
- Unresolvable merge conflict
- Repeated review/fix cycles without convergence
- Agent container crash after retry

## Concurrent Push Detection

If another user pushes to the PR branch while the loop is running:

1. The loop detects the head SHA change
2. Current cycle step is restarted with fresh PR state
3. A warning is logged
4. The loop never force-pushes — it always works on top of the latest HEAD

## GHA Workflow Replacement

Babysit-pr replaces the following GitHub Actions workflows with a single `on-push-babysit.yml` trigger that invokes a babysit-pr cycle on each push to a PR branch:

| Replaced Workflow | babysit-pr Equivalent |
|-------------------|-----------------------|
| `on-pull-request.yml` (AI code review) | Review step |
| `reusable-review.yml` (reusable review framework) | Review step |
| `on-pull-request-contract-verify.yml` (contract verification) | Review step (conditional criteria) |
| `on-pull-request-agent-mode-design.yml` (design review) | Review step (conditional criteria) |
| `on-review-feedback.yml` (feedback responder) | Feedback addressing step |
| `reusable-check-fixer.yml` (CI check fixer) | Check fix step |
| `reusable-conflict-resolve.yml` (conflict resolver) | Conflict resolution step |
| `on-merge-conflict.yml` (conflict trigger) | Conflict resolution step |
| `on-check-failure.yml` (check failure trigger) | Check fix step |

### Replaced Bash Prompt Builders

The following bash prompt builder scripts are also replaced by Python prompt modules in `shared/egg_babysit/prompts.py`:

| Replaced Script | Replacement |
|-----------------|-------------|
| `action/build-review-prompt.sh` | `build_review_prompt()` |
| `action/build-contract-verification-prompt.sh` | `build_review_prompt()` (conditional criteria) |
| `action/build-agent-mode-design-review-prompt.sh` | `build_review_prompt()` (conditional criteria) |
| `action/build-check-fixer-prompt.sh` | `build_check_fixer_prompt()` |
| `action/build-feedback-prompt.sh` | `build_feedback_fixer_prompt()` |
| `action/build-conflict-prompt.sh` | `build_conflict_resolution_prompt()` |

### Retained Workflows and Scripts

The following are **not** replaced by babysit-pr and remain operational:

| Workflow / Script | Reason |
|-------------------|--------|
| `on-push-doc-updater.yml` | Serves a different purpose (post-merge doc updates) |
| `reusable-autofix.yml` | Deprecated but kept for external repo consumers |
| `action/build-doc-updater-prompt.sh` | Used by `on-push-doc-updater.yml` |
| `action/build-autofixer-prompt.sh` | Used by `reusable-autofix.yml` |
| `gha_exec()` in `action/entrypoint.sh` | Still needed by the retained workflows above |

### Migration from GHA Workflows

The `on-push-babysit.yml` workflow triggers on `pull_request` events (`opened`, `synchronize`, `ready_for_review`, `reopened`) and creates a babysit pipeline via the orchestrator API. A concurrency group (`egg-babysit-${{ github.event.pull_request.number }}`) ensures that rapid successive pushes cancel in-flight cycles, matching the deduplication behavior of the previous GHA workflows.

## Limitations

- **No PR creation**: `babysit-pr` monitors an existing PR. To create a PR and then babysit it, use the coordinator (#1028)
- **Single PR**: Each `babysit-pr` instance monitors one PR. For multi-PR workflows, run multiple instances
- **No force push**: The loop never force-pushes. If the branch is in a state requiring force push, it escalates to HITL
- **Coordinator dependency**: PR-seeded task workflows (where the coordinator reads a PR as a task prompt) require #1028

## Related Documentation

- [GitHub Automation Guide](github-automation.md) — Remaining webhook-driven automation workflows (doc updater, autofix)
- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard issue-based pipeline
- [Pipeline Health Monitoring](pipeline-health-monitoring.md) — Health monitoring for pipelines including babysit mode
- [Concurrent Execution Guide](concurrent-execution.md) — BRC consensus protocol and multi-agent coordination
- [`shared/egg_babysit/README.md`](../../shared/egg_babysit/README.md) — Package-level technical reference
