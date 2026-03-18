# egg_babysit

Autonomous PR review/fix loop that monitors a pull request through its lifecycle — resolving merge conflicts, fixing CI failures, posting code reviews, and addressing feedback — until the PR is merged or a timeout is reached.

## Overview

`egg_babysit` is a shared Python package that implements the `babysit-pr` workflow described in [issue #1014](https://github.com/jwbron/egg/issues/1014). It runs as a standalone CLI command or as a sub-task of the coordinator (#1028).

The loop replicates the manual review/fix cycle demonstrated in [PR #1011](https://github.com/jwbron/egg/pull/1011), automating steps 1-9 of the typical PR lifecycle:

1. Code pushed → CI checks run
2. Failures detected → fixer applies corrections
3. Reviews posted → feedback addressed
4. Loop continues until PR merges or escalation

## Quick Start

```bash
# Monitor a PR through its review/fix lifecycle
egg babysit-pr 42

# With options
egg babysit-pr 42 --repo owner/repo --timeout 4h --max-iterations 10
```

## Architecture

```
egg babysit-pr <PR>
  └── Creates orchestrator pipeline (pr-{N}, mode: babysit)
  └── Main loop (shared/egg_babysit/loop.py):
        ┌──────────────────────────────────────┐
        │  1. Check merge conflicts             │
        │     → spawn fixer if dirty            │
        │  2. Wait for CI checks                │
        │     → poll gh pr checks               │
        │  3. Fix failing checks                │
        │     → non-LLM first, then LLM fixer  │
        │  4. Wait for CI re-run                │
        │  5. Post code review                  │
        │     → read-only reviewer agent        │
        │  6. Address review feedback            │
        │     → fixer agent if changes requested│
        │  7. Loop back to step 1               │
        └──────────────────────────────────────┘
  └── Exit when: merged | timeout | max iterations | HITL escalation
```

## Package Structure

```
shared/egg_babysit/
├── __init__.py          # Public API: babysit(), BabysitConfig, BabysitLoop, type exports
├── __main__.py          # python -m egg_babysit support
├── cli.py               # CLI entry point (egg-babysit / egg babysit-pr)
├── config.py            # BabysitConfig frozen dataclass
├── types.py             # BabysitStep, BabysitExitReason, CICheckStatus, ReviewVerdict,
│                        #   CICheckResult, PRState, LoopState, BabysitResult
├── pr_state.py          # PR state poller via gh CLI (fetch_pr_state, fetch_ci_checks,
│                        #   get_full_pr_state, detect_head_sha_change)
├── ci_waiter.py         # CI check waiter with poll interval and stale detection
├── loop.py              # BabysitLoop class and babysit() entry point
├── prompts.py           # Prompt builders: load check-fixers.yml, build fixer/review prompts
├── fixer.py             # Fixer agent spawner (FixerResult, run non-LLM and LLM fixes)
├── reviewer.py          # Reviewer agent spawner (ReviewResult, read-only mode)
├── escalation.py        # HITL escalation (orchestrator decisions, GitHub comments, Slack)
├── steps/               # Individual loop step implementations
│   ├── __init__.py      # Re-exports: fix_failed_checks, resolve_conflicts,
│   │                    #   address_feedback, run_review
│   ├── conflict.py      # Merge conflict detection and resolution
│   ├── check_fix.py     # CI check fixer (non-LLM first, then LLM agent)
│   ├── review.py        # Code review posting (ReviewStepResult)
│   └── feedback.py      # Review feedback addressing
└── README.md            # This file
```

## Configuration

### BabysitConfig

Frozen dataclass defined in `config.py`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pr_number` | `int` | required | PR number to monitor |
| `repo` | `str` | required | Repository in `owner/repo` format |
| `timeout_seconds` | `int` | 14400 (4h) | Maximum wall-clock time in seconds |
| `max_iterations` | `int` | 10 | Maximum loop iterations |
| `poll_interval_seconds` | `int` | 30 | CI check poll interval in seconds |
| `max_retries_per_job` | `int` | 3 | Max fix retries per failing CI job |
| `max_feedback_rounds` | `int` | 5 | Max review/feedback rounds per iteration |
| `check_fixers_path` | `str` | auto-detected | Path to `check-fixers.yml` |
| `orchestrator_url` | `str` | auto-detected | Orchestrator API URL (from `EGG_ORCHESTRATOR_URL`) |
| `pipeline_id` | `str` | auto-generated | Pipeline ID (defaults to `pr-{N}`) |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `EGG_ORCHESTRATOR_URL` | Orchestrator API URL (for pipeline registration) |
| `EGG_PIPELINE_ID` | Auto-set to `pr-{N}` when running via orchestrator |
| `GATEWAY_URL` | Gateway sidecar URL (for `gh` CLI routing) |

## How It Works

### PR State Polling

The `pr_state` module fetches PR metadata using `gh` CLI commands routed through the gateway:

```python
from egg_babysit.pr_state import get_full_pr_state, detect_head_sha_change

# Fetch complete PR state (metadata + CI checks + review comments)
state = get_full_pr_state(pr_number=42, repo="owner/repo")
# state.merged           -> bool
# state.mergeable_state  -> "clean" | "dirty" | "blocked" | "unknown"
# state.has_conflicts     -> bool (property: mergeable_state == "dirty")
# state.ci_checks        -> list[CICheckResult]
# state.ci_status        -> CICheckStatus (aggregated property)
# state.failed_checks    -> list[CICheckResult] (property: failing checks)
# state.review_verdict   -> ReviewVerdict (approved/changes_requested/pending)
# state.head_sha         -> str

# Detect concurrent pushes
changed = detect_head_sha_change(old_sha="abc123", new_state=state)
```

Internally, `get_full_pr_state()` combines three `gh` CLI calls:
- `gh pr view --json ...` for PR metadata and review decision
- `gh pr checks --json ...` for CI check statuses
- `gh api repos/{owner}/{repo}/pulls/{N}/reviews` for review comment bodies

### Agent Spawning

Each fix/review step spawns a short-lived agent container via the orchestrator's `ContainerSpawner`:

- **Fixer agents** get read-write access to the PR branch. They receive a prompt tailored to the fix type (conflict resolution, check fix, or feedback addressing) and push commits.
- **Reviewer agents** run in read-only mode. They post GitHub reviews using `gh pr review` with the same prompts and criteria as `egg-reviewer`.

### Check Fixing Strategy

The check fixer follows a non-LLM-first strategy:

1. Look up the failing job in `shared/check-fixers.yml` for a non-LLM fix command
2. If a non-LLM fix exists, run it first (e.g., `make lint-fix` for lint failures)
3. If the non-LLM fix doesn't resolve the issue, spawn an LLM fixer agent
4. Track retries per job using `<!-- egg-autofix-state -->` HTML markers in PR comments
5. Escalate to HITL after `max_retries_per_job` attempts

### Retry Tracking

Retry state is tracked via HTML comment markers on the PR, consistent with the existing GitHub Actions check fixer behavior:

```html
<!-- egg-autofix-state {"job":"lint","attempts":2,"max":3} -->
```

### HITL Escalation

When the loop encounters an unrecoverable state:

1. Creates a HITL decision via the orchestrator's `DecisionQueue`
2. Posts a GitHub comment on the PR explaining the blocker
3. Optionally sends a Slack notification via `notifications.slack_notify`
4. Loop pauses until the decision is resolved, then resumes or exits

### Exit Conditions

| Condition | Behavior |
|-----------|----------|
| PR merged | Exit with success |
| Timeout reached | Exit with timeout status |
| Max iterations | Exit with iteration limit status |
| HITL escalation (unresolved) | Pause, wait for human decision |
| Agent crash (unrecoverable) | Exit with error, notify human |

## Orchestrator Integration

The CLI registers a pipeline with the orchestrator for observability:

- **Pipeline ID**: `pr-{N}` (e.g., `pr-42`)
- **Pipeline mode**: `babysit`
- **State tracking**: Loop iteration, current step, per-job retry counts
- **Health monitoring**: OverseerMonitor detects stalled loops
- **Crash recovery**: Loop state persisted in pipeline state; restart resumes from saved position

## Shared Components

`egg_babysit` reuses existing egg infrastructure:

| Component | Source | Usage |
|-----------|--------|-------|
| Check fixer config | `shared/check-fixers.yml` | Non-LLM fix commands, retry limits, model selection |
| Review prompt builder | `action/build-review-prompt.sh` | Constructs review prompts from PR diff |
| Check fixer prompt | `action/build-check-fixer-prompt.sh` | Constructs fix prompts from failure logs |
| Review criteria | `shared/prompts/code-review-criteria.md` | Code review standards |
| Autofixer rules | `shared/prompts/autofixer-rules.md` | Fix vs. report classification |
| Container spawner | `orchestrator/container_spawner.py` | Agent container lifecycle |
| Gateway sidecar | `gateway/` | Git/GitHub operations, credential injection |

## Gateway Compatibility

The gateway already allows pushing to PR branches when:

- The bot has an open PR on that branch, OR
- The push comes from a trusted user (`GATEWAY_TRUSTED_USERS` / `TRUSTED_BRANCH_OWNERS`)

No gateway changes are required for core babysit-pr functionality.

## Future: Coordinator Integration

When the coordinator (#1028) is implemented, `egg_babysit` will be consumed as a sub-task:

```
Coordinator (PR-seeded task)
  └── Assess PR state
  └── Spawn agents as needed (coder, tester, documenter)
  └── Enter babysit-pr mode (calls egg_babysit.loop.babysit())
  └── Report completion
```

The loop module is designed as a standalone, importable component for this purpose. The `babysit()` function returns a `BabysitResult` synchronously:

```python
from egg_babysit import babysit, BabysitConfig

config = BabysitConfig(pr_number=42, repo="owner/repo")
result = babysit(config)
print(result.exit_reason)  # "merged", "timeout", "escalated", etc.
```

The `BabysitLoop` class can also be instantiated directly for more control over the loop lifecycle.

## Testing

```bash
# Unit tests
pytest shared/tests/test_egg_babysit/ -v

# Integration tests (requires Docker)
pytest integration_tests/test_babysit_pr/ -v
```

## Related Documentation

- [Issue #1014](https://github.com/jwbron/egg/issues/1014) — Feature specification
- [PR #1011](https://github.com/jwbron/egg/pull/1011) — Reference implementation of the review/fix lifecycle
- [Babysit-PR Guide](../../docs/guides/babysit-pr.md) — Operational guide
- [GitHub Automation Guide](../../docs/guides/github-automation.md) — Related automation workflows
- [SDLC Pipeline Guide](../../docs/guides/sdlc-pipeline.md) — Pipeline lifecycle
