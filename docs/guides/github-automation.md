# GitHub Automation Guide

egg includes GitHub Actions workflows that automate development tasks. Each workflow
runs inside the sandbox with full security controls — the agent cannot access
credentials, merge PRs, or push outside its branch namespace.

**Using these workflows in external repositories?** See the [Reusable Workflows guide](reusable-workflows.md)
for how to call egg's workflows from your own repositories.

## Workflows Overview

| Workflow | Trigger | What It Does |
|----------|---------|--------------|
| [AI Code Review](#ai-code-review) | PR opened/updated | Reviews code changes, posts feedback via `gh pr review` |
| [Address Review Feedback](#address-review-feedback) | Review posted on bot PR | Automatically addresses review feedback, enabling review loops |
| [Design Review](#design-review) | PR opened/updated (specialized) | Applies project-specific review rules via the same reusable framework |
| [@mention Response](#mention-response) | Bot mention in issues/PR comments | Runs arbitrary tasks requested by authorized users |
| [Check Autofixer](#check-autofixer) | CI check failure on a PR | Diagnoses failures, auto-fixes or reports |
| [Conflict Resolver](#conflict-resolver) | Push to main / every 2 hours / manual | Resolves merge conflicts via rebase |
| [Self-Improvement](#self-improvement) | Nightly schedule (2 AM UTC) | Analyzes all runs for issues, creates tracking issues |
| [Doc Updater](#doc-updater) | Push to main | Checks if code changes require documentation updates |

## AI Code Review

**Workflow:** [`.github/workflows/on-pull-request.yml`](../../.github/workflows/on-pull-request.yml)
**Framework:** [`.github/workflows/reusable-review.yml`](../../.github/workflows/reusable-review.yml)

Triggers on `pull_request` events (`opened`, `synchronize`, `ready_for_review`, `reopened`)
and via `workflow_dispatch` with a PR number.

### How It Works

1. **Skip checks** — Skips draft PRs, PRs with `[skip-review]` in the title, and PRs where
   the current HEAD commit has already been reviewed by the same bot. This prevents redundant
   reviews when a draft PR is marked as ready for review without new commits.
2. **Wait for CI checks** — Waits for all non-review checks (e.g., lint, tests) to complete
   before starting the review. Skips checks matching `egg-review /` (the standard reviewer
   job prefix), `egg-reviewer-` (nested reviewer jobs), `SDLC Pipeline`, or `SDLC HITL` to
   avoid self-deadlock. If checks fail, the review is skipped. Workflow dispatch triggers
   bypass this wait and the already-reviewed check. Times out after 25 minutes with a warning
   and proceeds anyway.
3. **Re-review detection** — Searches for an `<!-- egg-automated-review bot=<name> commit=<sha> -->`
   marker in previous reviews/comments to identify the last reviewed commit. On re-review,
   the agent uses `git diff <last-commit>..HEAD` to focus on new changes only.
4. **Stale review dismissal** — Dismisses previous bot reviews before posting a new one.
5. **Trusted prompt build** — Checks out `main` (not the PR branch) to run
   `build-review-prompt.sh`, preventing prompt injection from malicious PRs.
6. **SHA validation** — Verifies the PR HEAD still matches the commit that passed checks,
   aborting if code was pushed after checks completed but before the review started.
7. **Agent review** — Checks out the PR branch and runs egg with the review prompt.
   The agent reads the diff, examines context, and posts its review via `gh pr review`.

### Review Decisions

The agent chooses one of:
- **Approve** — No blocking issues found.
- **Request changes** — Security vulnerabilities, logic errors, or breaking changes.
- **Comment** — Advisory feedback, questions, suggestions.

### Customization

Place a `.egg/review-rules.md` file in your repository to override the default review
focus areas. Default rules focus on security, correctness, and code quality while
skipping linter-handled style issues.

### Reusable Framework

`reusable-review.yml` is a `workflow_call` workflow that any caller can invoke with:
- `pr_number` — PR to review
- `bot_name` — Identifier for marker tracking and concurrency
- `prompt_script` — Path to the prompt builder (default: `action/build-review-prompt.sh`)
- `timeout` — Minutes before the review times out

This enables multiple specialized reviewers (e.g., security-focused, design-focused)
by providing different prompt scripts while sharing the review infrastructure.

### Reviewer Job Naming Convention

**All reviewer workflows must use the `egg-review /` prefix for their job names.** This
standardized prefix ensures check-waiting logic correctly filters out all reviewer
jobs to prevent self-deadlock.

Example job definition:
```yaml
jobs:
  review:
    name: egg-review / My Custom Review  # Must start with "egg-review /"
    uses: ./.github/workflows/reusable-review.yml
    with:
      bot_name: my-custom-review
      # ...
```

Check-waiting logic filters out `egg-review /` in check run names. If a new reviewer
workflow doesn't follow this convention, it will cause infinite loops where reviewers
wait on each other indefinitely.

## Address Review Feedback

**Workflow:** [`.github/workflows/on-review-feedback.yml`](../../.github/workflows/on-review-feedback.yml)

Triggers when a review bot posts feedback on a PR, enabling an automated review loop:
PR opened → review → address feedback → re-review → ... → approval or human escalation.

### Trigger Events

The workflow runs on:
- `pull_request_review` — Formal reviews posted via `gh pr review`
- `issue_comment` — Self-reviews posted as comments (GitHub blocks bots from reviewing their own PRs via the Reviews API)
- `workflow_dispatch` — Manual trigger with PR number (bypasses filters for debugging)

### How It Works

1. **Filter checks** — Only runs when:
   - PR is open (not closed/merged)
   - PR is from the same repository (not a fork — bot can't push to forks)
   - PR author is the bot (unless manually triggered)
   - PR doesn't have `[skip-review]` marker
   - Review is not an approval (filtered at job level to prevent runner allocation)
   - Iteration count is below the limit (default: 3 rounds)

2. **Wait for all reviewers** — Polls GitHub check runs for all `egg-reviewer-*` jobs
   to complete before proceeding. This prevents race conditions when multiple reviewers
   (e.g., Code Review and Design Review) trigger the feedback workflow concurrently.
   The workflow waits up to 10 minutes, proceeding with a warning on timeout. If no
   reviewer checks are found after 2 minutes, the workflow exits with a warning (this
   indicates a potential configuration issue since the workflow was triggered by
   reviewer feedback).

3. **Comment cleanup** — Minimizes previous feedback-addressing comments to reduce clutter.

4. **Acknowledgment** — Posts a comment indicating feedback is being addressed, with an `<!-- egg-feedback-addressing -->` marker for iteration tracking.

5. **Trusted prompt build** — Checks out `main` (not the PR branch) to run `build-feedback-prompt.sh`, preventing prompt injection from malicious PRs.

6. **Agent execution** — Checks out the PR branch and runs egg. The agent:
   - Reads review feedback via `gh pr view`, `gh api` for reviews and line-level comments
   - Understands the current code via `gh pr diff`
   - Makes fixes addressing actionable feedback
   - Runs tests and linters before pushing
   - Commits and pushes all fixes together
   - Replies to feedback it disagrees with or cannot address

7. **Result comment** — Posts success or failure status with link to run logs.

### Iteration Limiting

To prevent infinite feedback loops, the workflow limits rounds to 3 by default:
- Counts previous feedback-addressing runs via `<!-- egg-feedback-addressing -->` markers
- When limit is reached, posts a comment requesting human review
- Manual `workflow_dispatch` triggers bypass the limit for debugging

There's a small race window where concurrent runs could exceed the limit by one round, but the concurrency group (`cancel-in-progress: true`) mitigates this for most cases.

### Feedback Rules

The agent addresses all actionable review feedback:

| Action | When |
|--------|------|
| **Fix** | Correctness issues, security concerns, logic errors, missing error handling, resource leaks, breaking changes, pattern violations |
| **Respond (do not fix)** | Disagreement with feedback — agent posts a reply explaining reasoning instead of making the change |
| **Skip** | Pure style suggestions that linters handle, subjective preferences without technical justification |

### Security

The workflow follows the trusted prompt build pattern:
1. Prompt script runs from `main` to prevent PR-based prompt injection
2. Agent runs in the sandbox with no credential access
3. Gateway enforces branch ownership and blocks merges

## Design Review

**Workflow:** [`.github/workflows/on-pull-request-agent-mode-design.yml`](../../.github/workflows/on-pull-request-agent-mode-design.yml)

A specialized reviewer that checks PRs for alignment with [agent-mode design principles](agent-mode-design.md).
Uses the same reusable framework as AI Code Review but with a focused prompt.

### Trigger Scope

Only runs on PRs that modify agent-related files:
- `action/**` — Action code and prompt builders
- `.github/workflows/**` — Workflow definitions
- `sandbox/**/*.md` — Sandbox documentation
- `docs/guides/agent-mode-design.md` — The design guide itself
- `docs/guides/sdlc-pipeline.md` — SDLC pipeline operational guide
- `docs/architecture/**` — Architecture documentation

### What It Reviews

This is a **specialized** review, not a general code review. The base AI Code Review
handles correctness, security, and style. Design Review focuses exclusively on:

| Anti-Pattern | Description |
|--------------|-------------|
| Excessive pre-fetching | Baking large diffs (10KB+) or full file contents into prompts |
| Structured output for humans | Requiring JSON when output goes directly to PR comments |
| Post-processing pipelines | Scripts that parse agent output to take actions the agent could take directly |
| Rigid procedures | Micromanaging step-by-step procedures when objectives would suffice |
| Prompt-level security | Using instructions for constraints that should be sandbox-enforced |

### Review Philosophy

The reviewer applies guidelines with judgment, not as absolute rules:
- **Orienting vs constraining** — Lightweight metadata that helps the agent is fine;
  large pre-fetched data that constrains exploration is not.
- **Practical balance** — A design that's 80% aligned but works is better than 100%
  pure but fragile.
- **Benefit of the doubt** — Borderline cases lean toward the charitable interpretation.

If a PR has no agent-mode concerns, the reviewer approves with a brief note rather
than providing general feedback that duplicates the base review.

## @mention Response

**Workflow:** [`.github/workflows/on-mention.yml`](../../.github/workflows/on-mention.yml)

Triggers when an authorized user mentions the configured bot in:
- Issue comments
- PR comments
- PR review comments (inline code comments)
- PR review submissions (full review summary)
- New issues

### How It Works

1. **Authorization** — Only runs for authorized users (currently `jwbron`).
   Bot self-triggers are blocked as defense-in-depth.
2. **Acknowledgment** — Reacts to the comment/issue with an eyes emoji.
3. **Context detection** — Determines whether the mention is on a PR (checks out PR branch)
   or an issue (checks out `main`).
4. **Trusted prompt build** — Builds the prompt from `main`, then checks out the PR branch
   for execution if applicable.
5. **Execution** — Runs egg with the context of the mention. The agent can read code,
   make changes, push commits, create PRs, and post comments.

### Concurrency

Mentions on the same issue/PR are queued (not cancelled) via concurrency groups,
so each request is processed.

## Check Autofixer

**Workflow:** [`.github/workflows/on-check-failure.yml`](../../.github/workflows/on-check-failure.yml)

Triggers when `Lint` or `Test` workflows fail on a PR, or via `workflow_dispatch`.

### How It Works

1. **Skip check** — Skips PRs with `[skip-autofix]` in the title.
2. **Comment cleanup** — Minimizes previous "investigating" comments to reduce clutter.
3. **Acknowledgment** — Posts a comment linking to the failed workflow run.
4. **Trusted prompt build** — Builds the autofixer prompt from `main` using
   `build-autofixer-prompt.sh`, which includes the failed workflow name and run ID.
5. **Investigation** — The agent uses `gh pr checks` to list failures, examines logs
   via `gh run view <id> --log-failed`, and reads workflow files for context.
6. **Fix or report** — Auto-fixable issues (lint, formatting, simple type errors) are
   fixed, committed, and pushed. Complex issues get a comment explaining the problem
   and suggested next steps.

### Auto-Fix vs Report

The agent follows these rules (customizable via `.egg/autofixer-rules.md`):

| Action | When |
|--------|------|
| **Auto-fix** | Lint errors, formatting, import order, type errors with clear fixes, simple test failures |
| **Report only** | Complex logic errors, security issues, unclear requirements, missing environment config |

## Conflict Resolver

**Workflow:** [`.github/workflows/on-merge-conflict.yml`](../../.github/workflows/on-merge-conflict.yml)

Triggers on push to main (when conflicts are actually introduced) to detect PRs with merge conflicts and resolve them via rebase. A scheduled run every 2 hours provides a safety net for cases where the push-triggered run fails or GitHub's mergeable state computation takes longer than expected. Also supports `workflow_dispatch` for manual triggering on a specific PR.

### How It Works

1. **Push-triggered detection** — When code is pushed to main, waits 60 seconds for GitHub to
   recompute mergeable state, then queries all open PRs to find conflicts. Concurrent pushes
   are deduplicated via a concurrency group with cancel-in-progress.
2. **Scheduled fallback** — Runs every 2 hours as a safety net for PRs that develop conflicts
   outside of main branch updates or where the push-triggered run failed.
3. **Conflict detection** — Queries all open PRs and checks their `mergeable_state`. PRs with
   a "dirty" state (indicating conflicts) are queued for resolution. Fork PRs are skipped
   since the bot cannot push to forks.
4. **Skip check** — Skips PRs with `[skip-conflict-fix]` in the title.
5. **Comment cleanup** — Minimizes previous conflict resolution comments to reduce clutter.
6. **Acknowledgment** — Posts a comment indicating conflict resolution has started.
7. **Trusted prompt build** — Builds the conflict prompt from `main` using
   `build-conflict-prompt.sh`, which includes the base branch name for rebase.
8. **Resolution** — The agent:
   - Fetches the base branch and starts a rebase
   - Resolves each conflict based on conflict resolution rules
   - Runs local checks to verify the resolution
   - Pushes with `--force-with-lease` if successful
9. **Escalation** — If conflicts require human judgment (semantic conflicts, security code,
   database migrations), the agent aborts the rebase and posts a comment explaining which
   files need review and why.

### Resolution Strategy

The agent follows these rules (customizable via `.egg/conflict-rules.md`):

| Action | When |
|--------|------|
| **Auto-resolve** | Lock files (regenerate), additive changes, formatting, version bumps |
| **Escalate** | Semantic conflicts, API changes, security code, migrations, config files |

### Concurrency

Each PR gets its own concurrency group (`egg-conflict-<pr_number>`) with
`cancel-in-progress: false`, ensuring conflict resolution attempts complete rather than
being cancelled by subsequent scheduled runs.

## Self-Improvement

**Workflow:** [`.github/workflows/self-improvement.yml`](../../.github/workflows/self-improvement.yml)

Runs nightly at 2 AM UTC (and via `workflow_dispatch`) to analyze recent workflow runs.

### How It Works

1. **Run collection** — Pre-collects data from all egg-related workflow runs (mention,
   review, autofixer, self-improvement) from the last 24 hours. Analyzes both failed
   AND successful runs, since successful runs may contain tool errors, warnings, or
   patterns worth investigating.
2. **Partitioned analysis** — Run data is partitioned across multiple egg instances
   that analyze in parallel. Each partition receives a subset of runs to ensure all
   runs fit within context limits.
3. **Log analysis** — For each run, examines logs via `gh run view <id> --log` to
   understand what happened. Looks for gateway failures, auth issues, rate limiting,
   infrastructure problems, tool failures, warnings, and recurring patterns.
4. **Self-reflection** — The workflow analyzes its own runs. When self-improvement
   workflow failures are detected, the agent pays special attention to improving
   the self-improvement process itself.
5. **Issue management** — Creates GitHub issues with the `self-improvement` label.
   Checks for existing open issues to avoid duplicates, updating them with new
   occurrences instead.

### What It Looks For

The agent uses judgment to identify issues worth tracking:

| Category | Examples |
|----------|----------|
| **Infrastructure** | Gateway/sidecar failures, Docker issues, network problems |
| **Authentication** | Credential issues, token expiration, permission errors |
| **Rate limiting** | API limits, throttling patterns |
| **Tool failures** | Tool call errors (even in successful runs), retries |
| **Patterns** | Recurring warnings, deprecation notices, concerning trends |

A single transient failure may not need an issue, but recurring patterns do.

### Manual Trigger Options

- `since_hours` — Analyze runs from the last N hours (default: 24)
- `dry_run` — Analyze only, don't create issues

## Doc Updater

**Workflow:** [`.github/workflows/on-push-doc-updater.yml`](../../.github/workflows/on-push-doc-updater.yml)

Runs after PRs are merged to main. Analyzes code changes to determine if documentation
needs updating, and creates a PR if so.

### How It Works

1. **Trigger filtering** — Only runs on pushes to `main` that include code changes.
   Skips doc-only changes to prevent infinite loops.
2. **Change analysis** — Examines the merged commit, including diff stats and newly
   added files, to understand what changed.
3. **Impact assessment** — Checks if documentation needs updating based on the nature
   of changes. Focuses on new features, new files, and breaking changes.
4. **Structural doc check** — Reads `docs/development/STRUCTURE.md`,
   `docs/architecture/README.md`, and `docs/index.md` to check if they cover new
   components or features.
5. **Related doc discovery** — Extracts domain-specific terms from changed file paths
   and commit subjects, then searches all docs for files that reference those terms.
   This catches guides and ADRs that discuss the same feature area as the code change
   (e.g., `docs/guides/sdlc-pipeline.md` when SDLC code changes).
6. **PR creation** — If updates are needed, creates a PR with the documentation changes.
   PRs are tagged with `[doc-updater]` to prevent re-triggering.

### When Docs Get Updated

The doc-updater analyzes both the magnitude of changes and newly added files to catch
features that need documentation. It creates PRs when:

| Update Docs | Skip Updates |
|-------------|--------------|
| New files introduce tools, CLIs, or components not in STRUCTURE.md or architecture/README.md | Internal refactoring that doesn't change interfaces |
| New features or capabilities users/agents need to know about | Performance improvements |
| Breaking changes that make existing docs incorrect | Bug fixes (unless the bug was documented behavior) |
| New configuration options or API changes | Test-only changes |
| Architecture changes affecting documented system design | Prompt/config tuning that doesn't change documented interfaces |

The agent pays special attention to newly added files - commits adding 500+ lines of
new code often introduce components that should be documented in the project structure
and architecture docs.

### Manual Trigger Options

- `commit_sha` — Analyze changes from this specific commit (defaults to HEAD~1)
- `dry_run` — Analyze only, don't create PR

## Custom Linters

egg includes project-specific safety checks in `scripts/` that run as part of CI:

| Script | What It Checks |
|--------|----------------|
| `check-bin-symlinks.py` | `bin/` symlinks point to existing targets |
| `check-claude-imports.py` | Host services don't import Claude/Anthropic directly |
| `check-container-host-boundary.py` | Sandbox code doesn't import from host services |
| `check-container-paths.py` | No `sys.path` patterns that break in containers |
| `check-docker-and-claude-invocations.py` | Docker/Claude CLI invocations have `noqa` justification |
| `check-gh-cli-usage.py` | `gh` CLI write operations only run inside the container |
| `check-workflow-secrets.py` | No untrusted script execution with secrets in workflows |

These enforce the container/host security boundary and architectural invariants that
generic linters can't catch.

## Configuration

### Required Secrets

All workflows need these GitHub Actions secrets:

| Secret | Purpose |
|--------|---------|
| `BOT_APP_ID` | GitHub App ID for bot authentication |
| `BOT_APP_PRIVATE_KEY` | GitHub App private key |
| `BOT_APP_INSTALLATION_ID` | GitHub App installation ID |
| `ANTHROPIC_OAUTH_TOKEN` | Anthropic API authentication |

### Required Repository Variables

Event-triggered workflows require these repository variables (Settings → Secrets and variables → Actions → Variables):

| Variable | Purpose | Example |
|----------|---------|---------|
| `EGG_BOT_USERNAME` | Bot's GitHub username for self-trigger prevention | `james-in-a-box[bot]` |
| `EGG_BRANCH_PREFIX` | Branch prefix for bot-owned branches | `egg` |

Reusable workflows called via `workflow_call` receive these values as inputs from the caller instead.

### Per-Repository Customization

| File | Purpose |
|------|---------|
| `.egg/review-rules.md` | Custom review focus areas (overrides defaults) |
| `.egg/autofixer-rules.md` | Custom auto-fix vs report rules (overrides defaults) |
| `.egg/conflict-rules.md` | Custom conflict resolution rules (overrides defaults) |

### Skip Labels

| Marker | Effect |
|--------|--------|
| `[skip-review]` in PR title | Skips AI code review |
| `[skip-autofix]` in PR title | Skips check autofixer |
| `[skip-conflict-fix]` in PR title | Skips conflict resolver |

## Security Model

All workflows follow the same security pattern:

1. **Trusted prompt build** — Prompt scripts run from `main`, not from PR branches,
   preventing prompt injection via malicious PRs.
2. **Sandboxed execution** — The agent runs inside the egg sandbox with no credential access.
3. **Gateway enforcement** — All git/GitHub operations go through the gateway sidecar
   which enforces branch ownership, blocks merges, and injects credentials.

## Design Philosophy

These workflows follow [agent-mode design principles](agent-mode-design.md): give the
agent a clear objective and let it figure out how to accomplish it. Prompts are minimal —
they describe *what* to do, not *how*. The agent fetches what it needs via `gh` CLI and
takes action directly, rather than receiving pre-parsed data through a rigid pipeline.
