# GitHub Automation Guide

egg includes GitHub Actions workflows that automate development tasks. Each workflow
runs inside the sandbox with full security controls — the agent cannot access
credentials, merge PRs, or push outside its branch namespace.

**Using these workflows in external repositories?** See the [Reusable Workflows guide](reusable-workflows.md)
for how to call egg's workflows from your own repositories.

**Want a one-off BRC cycle against an existing PR instead of event-driven workflows?** See the [Babysit-PR Guide](babysit-pr.md) — it runs an implement-phase BRC cycle (role-typed `coder` + `tester` + `documenter` producers, `reviewer_code` reviewer) against the PR diff, pushing a single final consensus commit to the PR head. Entry point is the `/babysit-pr` MCP skill.

## Workflows Overview

| Workflow | Trigger | What It Does |
|----------|---------|--------------|
| [AI Code Review](#ai-code-review) | PR opened/updated | Reviews code changes, posts feedback via `gh pr review` |
| [Address Review Feedback](#address-review-feedback) | Review posted on bot/authorized-user PR, or human @mention | Automatically addresses review feedback, enabling review loops |
| [Design Review](#design-review) | PR opened/updated (specialized) | Applies project-specific review rules via the same reusable framework |
| [Contract Verification](#contract-verification) | PR with sdlc:pr label or new contract file added | Verifies implementation matches SDLC contract |
| [Check Autofixer](#check-autofixer) | CI check failure on a PR | Diagnoses failures, auto-fixes or reports |
| [Conflict Resolver](#conflict-resolver) | Push to main / schedule / manual | Resolves merge conflicts via merge commits |
| [Doc Updater](#doc-updater) | Push to main | Checks if code changes require documentation updates |

### Shared Prompt Criteria

Review criteria for each workflow are defined in `shared/prompts/` as markdown files. Both the GitHub Actions prompt builder scripts (`action/build-*-prompt.sh`) and the local orchestrator (`orchestrator/routes/pipelines.py`) read from the same shared files, ensuring consistent review behavior across both flows.

| File | Used By |
|------|---------|
| `shared/prompts/code-review-criteria.md` | AI Code Review, orchestrator reviewers |
| `shared/prompts/agent-design-criteria.md` | Design Review, orchestrator reviewers |
| `shared/prompts/autofixer-rules.md` | Check Autofixer |
| `shared/check-fixers.yml` | Check Autofixer (per-job config: non-LLM fixes, retries, model) |
| `shared/prompts/contract-review-criteria.md` | Contract Verification, orchestrator reviewers |
| `shared/prompts/onboarding-docs-prompt.md` | Documentation Onboarding (`egg-onboarding-docs`) |

Repositories can override criteria by placing a custom file in `.egg/` (e.g., `.egg/review-rules.md` overrides code review criteria, `.egg/onboarding-rules.md` overrides onboarding documentation rules).

**Keeping reviewers in sync**: The PR reviewer (GitHub Action) and SDLC reviewer (orchestrator) share criteria files but have separate inline fallbacks and conventions. See [`shared/prompts/REVIEWER-SYNC.md`](../../shared/prompts/REVIEWER-SYNC.md) for the sync guide and modification checklist.

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
   the agent uses `git log <last-commit>..HEAD --not origin/<base> -p` (preceded by a
   `git fetch origin <base>` nudge) to focus only on PR-side commits pushed since the
   last review, while excluding any commits that reached the branch via a base-branch
   merge. `<base>` resolves to the PR's base ref (from `pr-meta` → `BASE_REF`, default
   `main`), so deltas are computed against the correct upstream even on non-`main` PRs.
   This prevents the reviewer from attributing merged-in base-branch work to the PR
   itself (see [#1758](https://github.com/jwbron/egg/issues/1758)). First-cycle reviews
   (no prior marker) still use the full-PR `git diff origin/<base>...HEAD` form.
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
   - Configured via the `EGG_AUTHORIZED_USERS` repository variable (required — the workflow fails fast if the variable is unset, so there is no implicit default)
   - Manual/workflow_call triggers bypass authorization

2. **Filter checks** — Only runs when:
   - PR is open (not closed/merged)
   - PR is from the same repository (not a fork — bot can't push to forks)
   - PR author is the bot or an authorized user (unless manually triggered)
   - PR doesn't have `[skip-review]` marker
   - Review requires action (filtered at job level to prevent runner allocation):
     - Non-approval reviews (request-changes, comment) always trigger
     - Approvals trigger only if they include `<!-- has-suggestions -->` marker
   - Iteration count is below the limit (default: 5 rounds)

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
   - Posts a top-level summary comment with a per-item disposition for every actionable
     feedback item (see [Feedback Contract](#feedback-contract) below); may also reply
     inline on specific threads

8. **Contract verification** — Scans the agent's response comments (posted since run
   start) for forbidden phantom-follow-up phrases and verifies that every
   `deferred-to #NNNN` reference points to a real GitHub issue created during this run.
   On violation, posts a flag comment naming the offenses and fails the workflow.

9. **Result comment** — Posts status with link to run logs. Possible outcomes:
   - *Feedback addressed* — agent succeeded and contract passed
   - *Feedback contract violated* — agent ran but the contract guard detected a violation (see flag comment)
   - *Cancelled* — agent was cancelled mid-run
   - *Failed* — agent failed to address feedback
   - *Workflow failed before running egg* — an earlier step failed (e.g. trusted-prompt build), so `steps.egg.outcome` was `skipped` or unset

### Iteration Limiting

To prevent infinite feedback loops, the workflow limits rounds to 5 by default:
- Counts previous feedback-addressing runs via `<!-- egg-feedback-addressing -->` markers
- When limit is reached, posts a comment requesting human review
- Manual `workflow_dispatch` triggers bypass the limit for debugging

There's a small race window where concurrent runs could exceed the limit by one round, but the concurrency group (`cancel-in-progress: true`) mitigates this for most cases.

### Feedback Contract

Every actionable review item must receive one of three dispositions in the agent's
top-level response comment:

| Disposition | Usage |
|-------------|-------|
| `fixed-in-PR (commit <SHA>)` | Change was made in this PR. Must cite the commit SHA. |
| `deferred-to #<NNNN>` | Not fixing in this PR. Agent must file the issue with `gh issue create` *before* posting the response; `#NNNN` must be the resulting issue number created during this run. |
| `disagree (<reasoning>)` | Agent disagrees the change is needed. Must explain why. |

**Default to in-PR fixes.** Deferral is allowed only when the fix would balloon PR scope,
requires design alignment that can't be reached here, or the reviewer explicitly asked for
a follow-up. Phantom follow-ups — promises to file an issue after posting the response, or
`deferred-to` references to non-existent or pre-existing issues — are forbidden and
detected by a post-run guard that fails the workflow.

**Posting a response is mandatory.** Failing to post any top-level response comment is
itself a contract violation; the verifier flags `count == 0` and fails the run. There
is no "silent skip" path — pure style suggestions handled by linters or subjective
preferences without technical justification still appear in the response with a
`disagree (style preference, no technical impact)` tag or similar.

**Post-run contract verification:** After the agent runs, the workflow automatically
scans the agent's response comments for forbidden phrases and verifies every
`deferred-to #NNNN` reference points to a real GitHub issue (not a PR) created during
this run (with a 60s clock-skew grace window). Quoted reviewer text, fenced code
blocks, and inline `` `code` `` spans are excluded from the phrase scan to avoid
false positives. If violations are found, a flag comment is posted naming the
offenses and the workflow run fails.

**Note:** Reviewers can include non-blocking suggestions in approval reviews by adding `<!-- has-suggestions -->` anywhere in the review body. This marker signals that the approval includes suggestions the agent should address, triggering the feedback workflow even though the review state is "approved".

### Security

The workflow follows the trusted prompt build pattern:
1. Prompt script runs from `main` to prevent PR-based prompt injection
2. The contract verifier (`action/verify-feedback-contract.sh`) is stashed from the
   `main` checkout into `RUNNER_TEMP` before switching to the PR branch, preventing
   a malicious PR from shipping a modified verifier that polices its own run
3. Agent runs in the sandbox with no credential access
4. Gateway enforces branch ownership and blocks merges

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
- `sandbox/bin/**` — Sandbox CLI tools
- `shared/prompts/**` — Shared prompt criteria
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
| Direct LLM API calls outside sandbox | Calling the Anthropic API from orchestrator, gateway, or shared code instead of delegating to sandbox containers (enforced by EGG200 linter) |
| Direct API calls bypassing the Agent SDK | Using raw HTTP calls to the Anthropic API instead of `run_agent()` (in-sandbox) or `build_agent_command()` (orchestrator-spawned containers), which provide tool access and consistent configuration |
| Hardcoded model identifiers | Using full model IDs like `claude-sonnet-4-20250514` instead of short aliases (`sonnet`, `opus`, `haiku`) which auto-adopt the latest version (enforced by EGG201 linter) |

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
2. **Contract file detection** — PR adds a new file to `.egg-state/contracts/` (detected via the PR files diff API)

This dual-trigger approach ensures contract verification runs even when the label is missing but a new contract file is being introduced by the PR.

### How It Works

1. **Trigger check** — Determines if verification should run:
   - Fetches PR metadata (labels and branch name) in a single API call
   - Extracts issue number from branch name, PR body, or contract filename (used downstream for contract lookup)
   - For labeled PRs, runs immediately
   - For unlabeled PRs, queries the PR files API to check if any new file was added under `.egg-state/contracts/`
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
**Framework:** [`.github/workflows/reusable-check-fixer.yml`](../../.github/workflows/reusable-check-fixer.yml)
**Config:** [`shared/check-fixers.yml`](../../shared/check-fixers.yml)

Triggers when `Lint`, `Test`, or `Integration Tests` workflows fail on a PR, or via `workflow_dispatch`.
Uses a per-check fixer loop where CI validates after each fix attempt.

### How It Works

1. **Skip check** — Skips PRs with `[skip-autofix]` in the title.
2. **Identify failed jobs** — Queries the GitHub API for which specific jobs failed
   in the triggering workflow run (e.g., Python, Shell within Lint).
3. **Read autofix state** — Reads retry counts from a `<!-- egg-autofix-state -->`
   PR comment to track how many times each check has been attempted.
4. **Comment cleanup** — Minimizes previous status comments to reduce clutter.
5. **Build fix plan** — Runs `build-check-fixer-prompt.sh` from `main` (trusted),
   which reads `check-fixers.yml` config and determines:
   - Which checks have non-LLM fixes available (ruff, shfmt, etc.)
   - Which checks need the LLM fixer
   - Which checks have exceeded max retries (escalation needed)
6. **Phase 1: Non-LLM fixes** — On first attempt, runs mechanical fixes (e.g.,
   `ruff format`, `shfmt`) for applicable checks. If changes are produced, commits,
   pushes, and **exits** — CI re-runs with fresh context.
7. **Phase 2: LLM fixer** — If non-LLM fixes didn't apply or didn't resolve the
   issue, runs a focused LLM agent with a prompt listing only the specific failed
   jobs. The agent fixes and pushes but does **not** re-run checks locally.
8. **State update** — Increments attempt counts for each failed check in the
   state comment.
9. **Escalation** — When any check exceeds its `max_retries`, posts an escalation
   comment listing the checks that need human attention.

### CI-Driven Loop

The fixer operates in a loop driven by CI:
```
CI fails → fixer fixes → pushes → CI re-runs → still fails? → fixer re-invoked
```

The fixer does **not** run checks locally. This avoids wasting agent compute
on re-running checks that CI already handles. Each push triggers CI, which
re-triggers the fixer if checks still fail.

### Non-LLM Fixes

Mechanical fixes run before the LLM fixer on first attempt. Configured per job
in `check-fixers.yml`:

| Check | Non-LLM Fix |
|-------|-------------|
| Lint / Python | `ruff check --fix --unsafe-fixes` + `ruff format` |
| Lint / Shell | `shfmt` formatting |
| Lint / YAML | Trailing whitespace removal + final newline |

If non-LLM fixes produce changes, they are committed and pushed immediately.
CI re-runs, and if the check still fails, the LLM fixer handles it on the
next attempt.

### Retry and Escalation

Each check has a configurable `max_retries` (default: 3). State is tracked
in a PR comment with a JSON payload. When a check exceeds its max retries,
an escalation comment is posted requesting human intervention.

### Auto-Fix vs Report

The agent follows these rules (customizable via `.egg/autofixer-rules.md`):

| Action | When |
|--------|------|
| **Auto-fix** | Lint errors, formatting, import order, type errors with clear fixes, simple test failures |
| **Report only** | Complex logic errors, security issues, unclear requirements, missing environment config |

### Concurrency

Fixers serialize per PR using `cancel-in-progress: false`. When both Lint and
Test fail simultaneously, the two `workflow_run` events queue and execute
sequentially rather than one canceling the other.

### Configuration

Per-job settings in `check-fixers.yml`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `model` | `sonnet` | LLM model for this check |
| `timeout` | `15` | Minutes before timeout |
| `max_retries` | `3` | Max fix attempts before escalation |
| `non_llm_fix` | (none) | Shell commands for mechanical fixing |

Repos can override by placing `.egg/check-fixers.yml` in their repository.

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
   references to those terms. This catches guides and architecture docs that discuss the same
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

**CLI script:** `egg-onboarding-docs` ([`sandbox/bin/egg-onboarding-docs`](../../sandbox/bin/egg-onboarding-docs))
**Prompt file:** [`shared/prompts/onboarding-docs-prompt.md`](../../shared/prompts/onboarding-docs-prompt.md)

Complementary to the incremental doc-updater, the onboarding capability generates
index-based documentation for an entire repository from scratch. While the doc-updater
maintains documentation incrementally as code changes, onboarding creates the initial
structure — the `docs/index.md` navigation hub, component READMEs, and cross-references
that the doc-updater then keeps current.

### How It Works

The `egg-onboarding-docs` script reads the documentation standard from
`shared/prompts/onboarding-docs-prompt.md` and passes it to `egg-sdlc --prompt`.
The prompt defines the "pull, not push" documentation philosophy — `docs/index.md` as
a navigation hub, component READMEs alongside code, task-specific guide tables, and
writing style guidelines.

### Usage

```
egg-onboarding-docs [OPTIONS] <repo_dir>
```

| Flag | Purpose |
|------|---------|
| `--dry-run` | Survey the codebase and report what documentation would be created, without writing files or opening a PR |
| `--scope <pattern>` | Limit documentation to files matching the pattern (e.g. `"src/api/**"`) |
| `-h, --help` | Show help message |

Examples:

```bash
egg-onboarding-docs my-project              # Full onboarding
egg-onboarding-docs --dry-run my-project    # Survey only
egg-onboarding-docs --scope "lib/" my-project  # Scope to lib/
```

### Per-Repository Customization

Place a `.egg/onboarding-rules.md` file in the target repository to override or
extend the default documentation standard. These rules are appended to the prompt
and take precedence over the defaults.

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
| `EGG_AUTHORIZED_USERS` | Comma-separated list of GitHub users authorized to trigger review feedback via reviews or @mentions | `alice,bob` |

`EGG_AUTHORIZED_USERS` controls who can trigger the Address Review Feedback workflow through human reviews or @mentions — the bot itself is always authorized to trigger via automated reviews. The workflow fails fast at the validation step if any required variable is unset, so there is no implicit default.

Reusable workflows called via `workflow_call` receive these values as inputs from the caller instead.

### Per-Repository Customization

| File | Purpose |
|------|---------|
| `.egg/review-rules.md` | Custom review focus areas (overrides defaults) |
| `.egg/autofixer-rules.md` | Custom auto-fix vs report rules (overrides defaults) |
| `.egg/check-fixers.yml` | Custom per-check fixer config (overrides `shared/check-fixers.yml`) |
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
