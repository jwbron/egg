# Babysit-PR Guide

Autonomous review/fix loop that monitors a pull request through its full lifecycle — from CI failures to code review to merge.

## What It Does

`egg babysit-pr` watches a PR and automatically:

1. **Resolves merge conflicts** — detects dirty mergeable state and spawns a fixer agent
2. **Waits for CI checks** — polls `gh pr checks` at configurable intervals
3. **Fixes failing checks** — tries non-LLM fixes first (e.g., `make lint-fix`), then spawns an LLM fixer agent
4. **Posts code reviews** — spawns a read-only reviewer agent that posts GitHub reviews
5. **Addresses review feedback** — spawns a fixer agent to resolve requested changes
6. **Loops** until the PR is merged, a timeout is reached, or human intervention is needed

This replicates the manual cycle demonstrated in [PR #1011](https://github.com/jwbron/egg/pull/1011), where code was pushed, lint checks failed, the check fixer applied corrections, the reviewer posted feedback, and changes were addressed — all automatically.

## Usage

### Standalone CLI

```bash
# Basic usage — monitors PR #42 until merged or timeout
egg babysit-pr 42

# Specify repository explicitly
egg babysit-pr 42 --repo owner/repo

# Custom timeout (default: 4 hours)
egg babysit-pr 42 --timeout 2h

# Limit loop iterations (default: 10)
egg babysit-pr 42 --max-iterations 5
```

### As a Coordinator Sub-Task

When using the coordinator (#1028), babysit-pr mode is entered automatically after the implementation phase completes:

```
Coordinator assesses PR → spawns agents → enters babysit-pr mode → loop until merged
```

The coordinator calls `egg_babysit.loop.babysit()` directly, using the same loop logic as the CLI.

## How the Loop Works

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

### Exit Conditions

| Condition | What Happens |
|-----------|--------------|
| **PR merged** | Loop exits with success status |
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

When `egg babysit-pr` runs, it registers a pipeline with the orchestrator:

- **Pipeline ID**: `pr-{N}` (e.g., `pr-42` for PR #42)
- **Mode**: `babysit` (distinct from the standard `issue` mode)
- **State**: Tracks current loop iteration, step (conflict/ci-wait/fix/review/feedback), and per-job retry counts
- **Health monitoring**: The [OverseerMonitor](pipeline-health-monitoring.md) detects stalled loops via progress events
- **Crash recovery**: Loop state is persisted. If the container restarts, it resumes from the last saved position

## Agent Roles

`babysit-pr` reuses existing agent infrastructure — no new agent types:

| Role | Access | What It Does |
|------|--------|--------------|
| **Fixer** | Read-write: pushes to PR branch | Resolves conflicts, fixes CI failures, addresses review feedback. Uses prompts from `action/build-check-fixer-prompt.sh` and `action/build-conflict-prompt.sh` |
| **Reviewer** | Read-only: posts GitHub reviews only | Reviews code changes using `shared/prompts/code-review-criteria.md`. Same behavior as `egg-reviewer` in GitHub Actions |

Each agent runs as a short-lived container spawned by the orchestrator's `ContainerSpawner`. This keeps context windows fresh and costs predictable.

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

## Relationship to Existing Workflows

`babysit-pr` consolidates the logic from several existing GitHub Actions workflows:

| Workflow | babysit-pr Equivalent |
|----------|-----------------------|
| `on-check-failure.yml` (check autofixer) | Check fix step |
| `on-merge-conflict.yml` (conflict resolver) | Conflict resolution step |
| `on-pull-request.yml` (AI code review) | Review step |
| `on-review-feedback.yml` (feedback responder) | Feedback addressing step |

The key difference: GitHub Actions workflows are event-driven (triggered by webhooks), while `babysit-pr` is a continuous polling loop. Both use the same shared prompts and criteria files.

## Limitations

- **No PR creation**: `babysit-pr` monitors an existing PR. To create a PR and then babysit it, use the coordinator (#1028)
- **Single PR**: Each `babysit-pr` instance monitors one PR. For multi-PR workflows, run multiple instances
- **No force push**: The loop never force-pushes. If the branch is in a state requiring force push, it escalates to HITL
- **Coordinator dependency**: PR-seeded task workflows (where the coordinator reads a PR as a task prompt) require #1028

## Related Documentation

- [GitHub Automation Guide](github-automation.md) — Existing webhook-driven automation workflows
- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard issue-based pipeline
- [Pipeline Health Monitoring](pipeline-health-monitoring.md) — Health monitoring for pipelines including babysit mode
- [Concurrent Execution Guide](concurrent-execution.md) — Multi-agent coordination
- [`shared/egg_babysit/README.md`](../../shared/egg_babysit/README.md) — Package-level technical reference
