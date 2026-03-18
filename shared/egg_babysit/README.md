# egg_babysit

Automated PR lifecycle management -- monitors a GitHub pull request through CI checks, code review, and feedback resolution until it is merged, times out, or escalates to a human.

## Overview

`egg_babysit` implements the "babysit-pr" loop: a state machine that drives a PR from open to merged by automatically handling merge conflicts, CI failures, code review, and review feedback. At each step it spawns Claude agent sessions (via `egg_agent`) for LLM-powered fixes or falls back to non-LLM shell commands defined in `check-fixers.yml`. When the loop cannot make progress, it escalates to a human through the orchestrator HITL system, GitHub PR comments, and Slack notifications.

The loop supports two execution modes: **sequential** (one agent at a time) and **concurrent BRC** (fixer and reviewer agents run simultaneously during the review/feedback phase, coordinating via [BRC consensus](../../docs/guides/concurrent-execution.md)). Concurrent mode is activated when `EGG_CONCURRENT_MODE` is set, typically via the orchestrator pipeline triggered by `on-push-babysit.yml`.

## CLI Usage

```bash
egg-babysit <PR_NUMBER> [options]
```

Can also be invoked as a module:

```bash
python -m egg_babysit <PR_NUMBER> [options]
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `<PR_NUMBER>` | *(required)* | GitHub PR number to babysit |
| `--repo OWNER/REPO` | auto-detected | Repository in `owner/repo` format. Parsed from `git remote -v` if omitted. |
| `--timeout SECONDS` | `14400` (4h) | Maximum wall-clock time before timeout exit |
| `--max-iterations N` | `10` | Maximum fix-check-review loop iterations |
| `--poll-interval SECONDS` | `30` | Seconds between CI status polls |
| `--max-retries N` | `3` | Default max retries per failing CI job |
| `--max-feedback-rounds N` | `5` | Maximum rounds of review feedback addressing |
| `--check-fixers PATH` | auto-detected | Path to `check-fixers.yml` config |
| `--verbose`, `-v` | off | Enable debug logging |

### Exit Codes

- `0` -- PR merged, ready to merge, escalated to human, or cancelled (valid outcomes)
- `1` -- Timeout, max iterations exceeded, or error

### Programmatic Usage

```python
from egg_babysit import babysit, BabysitConfig

config = BabysitConfig(pr_number=42, repo="owner/repo")
result = babysit(config)
print(result.exit_reason)  # "merged", "timeout", etc.
```

## Configuration

### BabysitConfig

Frozen dataclass (`config.py`) with all loop parameters:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pr_number` | `int` | *(required)* | PR number to babysit |
| `repo` | `str` | *(required)* | Repository in `owner/repo` format |
| `timeout_seconds` | `int` | `14400` (4h) | Wall-clock timeout |
| `max_iterations` | `int` | `10` | Max fix-check-review iterations |
| `poll_interval_seconds` | `int` | `30` | CI poll interval |
| `max_retries_per_job` | `int` | `3` | Max retries per failing CI job |
| `max_feedback_rounds` | `int` | `5` | Max review feedback rounds |
| `check_fixers_path` | `str` | `""` | Path to `check-fixers.yml`; auto-detected if empty |
| `orchestrator_url` | `str` | `""` | Orchestrator URL; auto-detected from `EGG_ORCHESTRATOR_URL` |
| `pipeline_id` | `str` | `""` | Pipeline ID; auto-generated as `pr-{N}` if empty |
| `consensus_timeout_minutes` | `int` | `30` | BRC consensus timeout before HITL escalation |
| `max_consensus_rounds` | `int` | `3` | Maximum BRC propose/NACK/re-propose cycles (flip-flop cap) |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `EGG_ORCHESTRATOR_URL` | Orchestrator API URL for progress events and escalation |
| `EGG_PIPELINE_ID` | Pipeline ID; defaults to `pr-{N}` if unset |
| `EGG_REPO_PATH` | Working directory for git remote auto-detection |
| `EGG_CONCURRENT_MODE` | When `true`, enables concurrent BRC mode for the review/feedback phase |
| `EGG_BRC_ROLE_TYPE` | Agent's BRC role: `producer` (fixer) or `reviewer` |
| `EGG_BRC_REVIEWERS` | Comma-separated list of reviewer agent IDs |
| `EGG_BRC_PRODUCERS` | Comma-separated list of producer agent IDs |

### check-fixers.yml Integration

Defines non-LLM fix commands and per-job retry limits for CI failures. The file is searched at:

1. Explicit path from `--check-fixers` / `check_fixers_path`
2. `.egg/check-fixers.yml` in the repo root

When a CI job fails, the loop checks this config for a matching shell command (e.g., `make lint-fix` for lint failures) before spawning an LLM agent.

## Architecture

### Loop Lifecycle

The `BabysitLoop` class (`loop.py`) implements a state machine that cycles through these steps:

```
CHECK_CONFLICTS --> WAIT_CI --> FIX_CHECKS --> WAIT_CI --> REVIEW --> ADDRESS_FEEDBACK
      ^                                                                      |
      |______________________________________________________________________|
```

Each iteration proceeds as follows:

1. **CHECK_CONFLICTS** -- Fetch PR state via `gh` CLI. If already merged or closed, exit immediately. If the PR has merge conflicts (`mergeable_state == "dirty"`), call `resolve_conflicts()` to spawn a fixer agent. Escalate if conflicts are unresolvable.

2. **WAIT_CI** -- Poll CI check statuses at `poll_interval_seconds` via `wait_for_ci()`. The CI wait has a 30-minute cap (or remaining timeout, whichever is smaller). Stale checks (no status change after 20 consecutive polls) trigger escalation.

3. **FIX_CHECKS** -- For each failing check, call `fix_failed_checks()` which attempts a non-LLM fix from `check-fixers.yml` first, then spawns an LLM fixer agent. Per-job retry counts are tracked in `LoopState.retry_counts`; escalation occurs when `max_retries_per_job` is exceeded.

4. **WAIT_CI** (post-fix) -- Re-poll CI after fixes are pushed. If checks still fail, loop back to step 1 for the next iteration.

5. **REVIEW** -- If all CI checks pass and the PR is not already approved, call `run_review()` to spawn a read-only reviewer agent that posts a GitHub review. If the review approves the PR, exit with `READY_TO_MERGE` (the loop does not merge PRs — a human or coordinator does).

6. **ADDRESS_FEEDBACK** -- If the review requests changes, call `address_feedback()` to spawn a fixer agent that addresses review comments. Increment `feedback_rounds`; escalate when `max_feedback_rounds` is exceeded. Loop back to step 1.

### Concurrent BRC Execution

When `EGG_CONCURRENT_MODE` is set, the review/feedback phase is handled by `concurrent.py` instead of the sequential reviewer→fixer handoff. The `ConcurrentReviewExecutor` spawns both agents simultaneously:

1. **Fixer** starts as a BRC **producer** using `build_consensus_wrapped_command()` — its prompt includes instructions to run `egg-orch consensus propose` after completing fixes
2. **Reviewer** starts as a BRC **reviewer** — its prompt includes instructions to run `egg-orch consensus ack` or `egg-orch consensus nack` after reviewing
3. Both agents communicate via the orchestrator message bus (HANDOFF messages signal new commits)
4. The executor waits for BRC consensus (all reviewers ACK) or escalates on timeout/flip-flop

Non-LLM fixes (e.g., `make lint-fix`) still run outside the BRC loop — if they succeed, the fix is auto-proposed without LLM involvement.

Sequential phases (CHECK_CONFLICTS, WAIT_CI, FIX_CHECKS) are unaffected by concurrent mode — they run the same way regardless.

### Concurrent Push Detection

On each iteration, the loop compares the PR's HEAD SHA against `LoopState.last_head_sha`. If the SHA changed (indicating an external push), per-job retry counts are reset to avoid penalizing fixes for a now-stale branch state.

### Agent Sessions

Sub-agents are spawned as subprocesses via `egg_agent.build_agent_command` (sequential mode) or `egg_agent.build_consensus_wrapped_command` (concurrent BRC mode):

- **Fixer** (`fixer.py`) -- Read-write agent that fixes CI failures, resolves merge conflicts, or addresses review feedback. Supports both shell-command fixes and full LLM agent sessions. In BRC mode, uses consensus propose/confirmed flow.
- **Reviewer** (`reviewer.py`) -- Read-only agent that reviews the PR diff and posts a GitHub review via `gh pr review`. Captures the review verdict from PR state after the agent completes. In BRC mode, ACKs or NACKs fixer proposals.
- **Concurrent executor** (`concurrent.py`) -- Manages the concurrent review/feedback phase: spawns fixer and reviewer simultaneously, waits for BRC consensus, handles NACK cycles and timeout escalation.
- **Prompt builder** (`prompts.py`) -- Constructs task-specific prompts for each agent role, incorporating `check-fixers.yml` config, failure logs, review comments, and BRC consensus instructions when in concurrent mode.
- **Status comments** (`comments.py`) -- Manages PR status comments with `<!-- egg-status-comment -->` markers: posts new comments, minimizes prior ones as "OUTDATED", and deduplicates on the same commit SHA.

### Orchestrator Integration

When `EGG_ORCHESTRATOR_URL` is set, the loop integrates with the egg orchestrator:

- **Startup** -- Registers the pipeline via `egg-orch progress emit --step babysit_start`
- **Per-step progress** -- Emits structured progress events after each step (`conflict_resolution`, `fix_checks`, `review`, `address_feedback`) with working/blocked/complete states
- **Escalation** -- Routes HITL escalations through the orchestrator's decision queue, GitHub PR comments, and Slack notifications (via `escalation.py`)

All orchestrator calls are best-effort -- failures are logged at debug level but never interrupt the loop.

### Signal Handling

The loop installs `SIGTERM` and `SIGINT` handlers for graceful shutdown. On receipt, the `_cancelled` flag is set; the current iteration completes and the loop exits with `CANCELLED`.

### Crash Recovery

`LoopState` is a serializable dataclass tracking: iteration count, current step, last HEAD SHA, per-job retry counts, feedback round count, and ISO 8601 timestamps (`started_at`, `last_activity_at`). This state is designed for persistence so a coordinator can resume the loop from the last known position after a container restart.

## Module Reference

| Module | Description |
|--------|-------------|
| `__init__.py` | Public API exports: `babysit()`, `BabysitConfig`, `BabysitLoop`, and all type classes |
| `config.py` | `BabysitConfig` frozen dataclass with all loop parameters including BRC consensus fields |
| `types.py` | Enums (`BabysitStep`, `BabysitExitReason`, `CICheckStatus`, `ReviewVerdict`) and data classes (`PRState`, `LoopState`, `CICheckResult`, `BabysitResult`). Includes BRC agent role constants. |
| `cli.py` | CLI entry point: argument parsing, repo auto-detection from `git remote -v`, orchestrator pipeline registration |
| `loop.py` | `BabysitLoop` state machine and `babysit()` convenience function. Delegates to `concurrent.py` for the review/feedback phase when `EGG_CONCURRENT_MODE` is set. |
| `concurrent.py` | `ConcurrentReviewExecutor`: manages concurrent fixer+reviewer execution with BRC consensus for the review/feedback phase. Handles propose/ACK/NACK cycles, flip-flop cap, and consensus timeout. |
| `comments.py` | Status comment lifecycle: post with `<!-- egg-status-comment -->` markers, minimize prior comments as "OUTDATED", deduplicate on same commit SHA. |
| `ci_waiter.py` | CI polling loop with configurable interval and stale-check detection (20-poll threshold) |
| `pr_state.py` | PR metadata, CI status, review verdict fetching via `gh` CLI JSON output. Supports conditional review criteria detection (labels, changed file paths). |
| `fixer.py` | `FixerResult` dataclass and agent spawner for CI fixes, conflict resolution, and feedback addressing. Supports both sequential and BRC consensus-wrapped execution. |
| `reviewer.py` | `ReviewResult` dataclass and read-only reviewer agent spawner. In BRC mode, uses consensus ACK/NACK flow. Supports consolidated review criteria (base code + conditional contract verification + agent-mode design). |
| `prompts.py` | Prompt construction for all agent types; `check-fixers.yml` loading and search path resolution. Includes BRC consensus instructions when `EGG_CONCURRENT_MODE` is active. Conditional inclusion of contract verification and agent-mode design review criteria. |
| `escalation.py` | Multi-channel HITL escalation: orchestrator decisions, GitHub PR comments, Slack notifications |
| `steps/conflict.py` | Merge conflict detection and resolution step |
| `steps/check_fix.py` | CI check fixer step (non-LLM first, then LLM agent) |
| `steps/review.py` | Code review posting step with status comment management |
| `steps/feedback.py` | Review feedback addressing step |
| `__main__.py` | `python -m egg_babysit` support |

## Integration with Coordinator

The `egg_babysit` package is designed to be consumed as a library by the future coordinator (#1028). The key integration points:

- **`babysit(config)`** -- Single-function entry point. Pass a `BabysitConfig`, receive a `BabysitResult`. The coordinator calls this as a sub-task within a larger PR-seeded workflow.
- **`BabysitLoop`** -- For finer control, instantiate the loop directly. The coordinator can inspect `loop.state` (a `LoopState` instance) between iterations or subclass `BabysitLoop` to override individual steps.
- **`BabysitResult`** -- Structured result with `exit_reason`, `iterations`, `duration_seconds`, `last_step`, and `message`. The coordinator can branch on `exit_reason` to decide next actions (e.g., notify on escalation, retry on timeout).
- **`LoopState`** -- Fully serializable state for crash recovery. The coordinator can persist this to disk and restore it across container restarts to resume the loop mid-iteration.
- **Progress events** -- The loop emits `egg-orch progress` events at each step. The coordinator can consume these for dashboard reporting without modifying babysit internals.

Expected coordinator flow:

```
Coordinator (PR-seeded task)
  -> Assess PR state
  -> Spawn agents as needed (coder, tester, documenter)
  -> Enter babysit-pr mode: babysit(config)
  -> Inspect BabysitResult, report completion or escalate
```

## Exit Conditions

The loop exits and returns a `BabysitResult` when any of these conditions is met:

| Exit Reason | Trigger | Exit Code |
|-------------|---------|-----------|
| `merged` | PR is actually merged (detected via PR state) | 0 |
| `ready_to_merge` | PR is approved with all CI checks passing — ready for human merge | 0 |
| `timeout` | Wall-clock time exceeds `timeout_seconds` (default 4h) | 1 |
| `max_iterations` | Iteration count exceeds `max_iterations` (default 10) | 1 |
| `escalated` | Unresolvable merge conflicts, stale CI checks, per-job retry limit exceeded, or feedback round limit exceeded | 0 |
| `error` | Unhandled exception or repeated failure to fetch PR state | 1 |
| `cancelled` | `SIGTERM`/`SIGINT` received, or PR was closed without merging | 0 |
