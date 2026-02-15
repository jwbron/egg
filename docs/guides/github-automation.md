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
| [Address Review Feedback](#address-review-feedback) | Review posted on bot PR, or human @mention | Automatically addresses review feedback, enabling review loops |
| [Design Review](#design-review) | PR opened/updated (specialized) | Applies project-specific review rules via the same reusable framework |
| [Contract Verification](#contract-verification) | PR with sdlc:pr label or contract file | Verifies implementation matches SDLC contract |
| [Check Autofixer](#check-autofixer) | CI check failure on a PR | Diagnoses failures, auto-fixes or reports |
| [Conflict Resolver](#conflict-resolver) | Push to main / schedule / manual | Resolves merge conflicts via merge commits |
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

Triggers when a review bot posts feedback on a PR, or when a human @mentions the bot, enabling an automated review loop:
PR opened → review → address feedback → re-review → ... → approval or human escalation.

### Trigger Events

The workflow runs on:
- `pull_request_review` — Formal reviews posted via `gh pr review` (bot or authorized human)
- `issue_comment` — Bot self-reviews posted as comments, or authorized human @mentions the bot
- `workflow_dispatch` — Manual trigger with PR number (bypasses filters for debugging)

### Separate Reviewer Bot (Recommended)

By default, the bot cannot approve or request changes on its own PRs (GitHub blocks this).
To enable full review capabilities, configure a separate reviewer GitHub App:

1. Create a second GitHub App (e.g., `egg-reviewer`) with `pull_requests: write` permission
2. Install it on your repositories
3. Add secrets: `REVIEWER_APP_ID`, `REVIEWER_APP_PRIVATE_KEY`, `REVIEWER_APP_INSTALLATION_ID`

When configured, reviews use the reviewer account, enabling approve/request-changes on bot PRs.
Without it, the system falls back to posting reviews as comments (self-review mode).

### How It Works

1. **Trigger authorization** — For event-triggered runs, verifies the triggering user is authorized:
   - Bot reviews always trigger (the bot can review its own PRs)
   - Human reviews and @mentions require the user to be in the `authorized_users` list
   - Configured via `EGG_AUTHORIZED_USERS` repository variable (defaults to `jwbron`)
   - Manual/workflow_call triggers bypass authorization

2. **Filter checks** — Only runs when:
   - PR is open (not closed/merged)
   - PR is from the same repository (not a fork — bot can't push to forks)
   - PR author is the bot (unless manually triggered)
   - PR doesn't have `[skip-review]` marker
   - Review requires action (filtered at job level to prevent runner allocation):
     - Non-approval reviews (request-changes, comment) always trigger
     - Approvals trigger only if they include `<!-- has-suggestions -->` marker
   - Iteration count is below the limit (default: 3 rounds)

3. **Wait for all reviewers** — For review-triggered runs, polls GitHub check runs for all `egg-reviewer-*` jobs
   to complete before proceeding. This prevents race conditions when multiple reviewers
   (e.g., Code Review and Design Review) trigger the feedback workflow concurrently.
   The workflow waits up to 10 minutes, proceeding with a warning on timeout. If no
   reviewer checks are found after 2 minutes, the workflow exits with a warning (this
   indicates a potential configuration issue since the workflow was triggered by
   reviewer feedback). Mention-triggered runs skip this step since there are no reviewer
   checks to wait for.

4. **Comment cleanup** — Minimizes previous feedback-addressing comments to reduce clutter.

5. **Acknowledgment** — Posts a comment indicating feedback is being addressed, with an `<!-- egg-feedback-addressing -->` marker for iteration tracking.

6. **Trusted prompt build** — Checks out `main` (not the PR branch) to run `build-feedback-prompt.sh`, preventing prompt injection from malicious PRs.

7. **Agent execution** — Checks out the PR branch and runs egg. The agent:
   - Reads review feedback via `gh pr view`, `gh api` for reviews and line-level comments
   - Understands the current code via `gh pr diff`
   - Makes fixes addressing actionable feedback
   - Runs tests and linters before pushing
   - Commits and pushes all fixes together
   - Replies to feedback it disagrees with or cannot address

8. **Result comment** — Posts success or failure status with link to run logs.

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

**Note:** Reviewers can include non-blocking suggestions in approval reviews by adding `<!-- has-suggestions -->` anywhere in the review body. This marker signals that the approval includes suggestions the agent should address, triggering the feedback workflow even though the review state is "approved".

### Security

The workflow follows the trusted prompt build pattern:
1. Prompt script runs from `main` to prevent PR-based prompt injection
2. Agent runs in the sandbox with no credential access
3. Gateway enforces branch ownership and blocks merges

## Design Review

**Workflow:** [`.github/workflows/on-pull-request-agent-mode-design.yml`](../../.github/workflows/on-pull-request-agent-mode-design.yml)
**Framework:** [`.github/workflows/reusable-review.yml`](../../.github/workflows/reusable-review.yml)

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

## Contract Verification

**Workflow:** [`.github/workflows/on-pull-request-contract-verify.yml`](../../.github/workflows/on-pull-request-contract-verify.yml)
**Framework:** [`.github/workflows/reusable-review.yml`](../../.github/workflows/reusable-review.yml)

Verifies that PR implementations match their SDLC pipeline contracts. This workflow ensures agents stay aligned with approved plans and task requirements during the implementation phase.

### Trigger Conditions

The workflow runs on pull requests when **either** of these conditions is met:

1. **Label-based trigger** — PR has the `sdlc:pr` label
2. **Contract file detection** — PR branch contains `.egg-state/contracts/{issue_number}.json`, where the issue number is extracted from the branch name (`egg/issue-{number}...`)

This dual-trigger approach ensures contract verification runs even when the label is missing but the contract file exists, preventing silently skipped verifications.

### How It Works

1. **Trigger check** — Determines if verification should run:
   - Fetches PR metadata (labels and branch name) in a single API call
   - Extracts issue number from branch name using pattern `egg/issue-{number}...`
   - For labeled PRs, runs immediately
   - For unlabeled PRs, checks if contract file exists on the PR's head branch
2. **Contract verification** — Uses the reusable review framework with a contract-specific prompt:
   - Reads the contract from `.egg-state/contracts/{issue_number}.json`
   - Compares implementation against contract tasks
   - Verifies commits are linked to tasks via `egg-contract` metadata
   - Posts feedback via `gh pr review` if misalignments are detected

### Manual Trigger

Supports `workflow_dispatch` with a `pr_number` input for manual verification runs, bypassing filter checks.

### Contract File Format

Contract files follow the schema at `.egg/schemas/contract.schema.json`. The workflow specifically checks for task-commit linkages and ensures all contract tasks have corresponding implementation.

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

Resolves merge conflicts on PRs via merge commits. Can be triggered on push to main, via schedule, or manually with `workflow_dispatch`.

### Trigger Modes

1. **Push-triggered detection** — When code is pushed to main, waits 60 seconds for GitHub to recompute mergeable state, then queries all open PRs to find conflicts. Concurrent pushes are deduplicated via a concurrency group with cancel-in-progress.

2. **Scheduled fallback** — Runs every 2 hours as a safety net for PRs that develop conflicts outside of main branch updates or where the push-triggered run failed.

3. **Manual dispatch** — Supports `workflow_dispatch` for manual triggering on a specific PR.

### How It Works

1. **Conflict detection** — Queries open PRs and checks their `mergeable_state`. PRs with a "dirty" state (indicating conflicts) are queued for resolution. Fork PRs are skipped since the bot cannot push to forks. When triggered by the SDLC pipeline with a specific PR number, only that PR is processed.
2. **Skip check** — Skips PRs with `[skip-conflict-fix]` in the title.
3. **Comment cleanup** — Minimizes previous conflict resolution comments to reduce clutter.
4. **Acknowledgment** — Posts a comment indicating conflict resolution has started.
5. **Trusted prompt build** — Builds the conflict prompt from `main` using `build-conflict-prompt.sh`, which includes the base branch name for merging.
6. **Resolution** — The agent:
   - Fetches the base branch and starts a merge
   - Resolves each conflict based on conflict resolution rules
   - Runs local checks to verify the resolution
   - Pushes the merge commit if successful
7. **Escalation** — If conflicts require human judgment (semantic conflicts, security code, database migrations), the agent aborts the merge and posts a comment explaining which files need review and why.

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
   `docs/architecture/README.md`, `docs/index.md`, and `README.md` to check if they
   cover new components or features. For README.md, validates CLI Reference tables
   against `sandbox/egg_lib/cli.py`, enforcement tables against `gateway/phase_filter.py`,
   and deployment instructions.
5. **High-risk cross-reference** — Automatically detects changes to specific components
   and provides targeted instructions for cross-referencing docs against source code.
   For example, changes to `cli.py` trigger README CLI Reference validation, changes
   to gateway enforcement trigger enforcement table validation.
6. **Related doc discovery** — Extracts domain-specific terms from changed file paths
   and commit subjects, then searches both `docs/` and root-level markdown files for
   references to those terms. This catches guides and ADRs that discuss the same
   feature area as the code change (e.g., `docs/guides/sdlc-pipeline.md` when SDLC
   code changes).
7. **PR creation** — If updates are needed, creates a PR with the documentation changes.
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

## Documentation Onboarding

**Slash command:** `/onboarding-docs` ([`sandbox/.claude/commands/onboarding-docs.md`](../../sandbox/.claude/commands/onboarding-docs.md))

Complementary to the incremental doc-updater, the onboarding capability generates comprehensive documentation for an entire repository from scratch. This is useful for bootstrapping documentation on existing codebases or creating complete documentation sets for new projects.

### How It Works

Unlike the doc-updater (which reacts to individual commits), onboarding is a one-time or periodic "bootstrap" that documents an entire codebase:

1. **Repository discovery** — Scans the repository to understand structure:
   - Directory tree (depth 3)
   - Language distribution by file extension
   - Configuration and build files
   - Entry points (main files, CLIs)
   - Existing documentation and READMEs

2. **Prompt generation** — Builds a comprehensive prompt instructing Claude to:
   - Survey the codebase systematically
   - Plan the documentation structure
   - Write structured docs (index.md, STRUCTURE.md, architecture/README.md, component READMEs, guides)
   - Incorporate existing documentation rather than replacing it
   - Cross-reference and validate all links

3. **SDLC execution** — The generated prompt is fed into the SDLC pipeline via the orchestrator, running the documentation task through the standard refine-plan-implement cycle.

### Usage via Slash Command

Inside the sandbox:

```
/onboarding-docs [owner/repo]
```

The command will:
- Ask for the repository if not provided
- Clone the repository if needed
- Run the prompt builder
- Create and start an SDLC pipeline
- Stream live progress via `egg-pipeline-watch`

### Scope Limiting

Use environment variables to limit scope for large repositories:

- `DRY_RUN=true` — Analyze only, describe what docs would be created
- `INCLUDE_PATTERN="gateway/**"` — Only document files matching this glob
- `EXCLUDE_DIRS="legacy,tmp"` — Additional directories to skip

### Output Structure

The onboarding process creates the same documentation structure that the incremental doc-updater maintains:

```
docs/
├── index.md                      # Master navigation index
├── architecture/
│   └── README.md                 # System design, components, data flow
├── development/
│   └── STRUCTURE.md              # Directory layout and conventions
├── guides/
│   ├── quickstart.md             # Getting started
│   ├── deployment.md             # Deployment options
│   └── <topic>.md                # Additional guides
└── adr/
    └── README.md                 # ADR index

<component>/README.md             # Per-component READMEs
```

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

### Optional Repository Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `EGG_AUTHORIZED_USERS` | Comma-separated list of GitHub users authorized to trigger review feedback via reviews or @mentions | `jwbron` |

This variable controls who can trigger the Address Review Feedback workflow through human reviews or @mentions. The bot itself is always authorized to trigger via automated reviews.

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
