# Plan: AI-Powered Code Review Bots

**Issue:** [#134](https://github.com/jwbron/egg/issues/134)
**Related:** #70 (security linters in CI), #77 (autofixers in CI)

## Executive Summary

Implement AI-powered code review as a GitHub Action that runs automatically
on PRs. Start with a single-agent reviewer that combines security,
standards, and quality checks in one pass. Use the existing egg Action
infrastructure (`action/action.yml`, gateway + sandbox orchestration) to
avoid building new bot infrastructure.

## Current State

egg already has:
- A production GitHub Action (`action/`) that orchestrates gateway + sandbox
- An @mention workflow (`on-mention.yml`) that builds context-rich prompts
  from GitHub events and runs Claude Code
- Gateway API allowlists for all PR/review endpoints (files, comments,
  reviews, reactions)
- Claude Code runner with streaming output, timeout handling, and error
  classification
- `build-mention-prompt.sh` that fetches PR metadata, changed files, and
  recent comments

What's missing:
- A workflow that triggers automatically on PR open/update (not just
  @mention)
- A review-specific prompt template
- Review comment posting logic (inline + summary)
- Configuration for review scope and behavior

## Design Principles

1. **Single-agent architecture** — one reviewer that handles multiple
   concerns per pass. Multi-agent pipelines add complexity without
   proportional benefit for code review.
2. **GitHub Actions-native** — no webhook servers, no persistent
   infrastructure. Triggered by `pull_request` events.
3. **Reuse existing infrastructure** — the egg Action already handles
   container orchestration, credential isolation, and Claude Code execution.
4. **Incremental value** — ship the simplest useful thing first, iterate
   based on real usage.
5. **Low false-positive tolerance** — a noisy reviewer gets ignored. Bias
   toward fewer, higher-signal comments.

## Architecture

### Single-Agent Reviewer (Recommended Starting Point)

```
PR opened/updated
  │
  ▼
on-pull-request.yml workflow
  │
  ├── Build review prompt (action/build-review-prompt.sh)
  │   ├── Fetch PR diff (gh api pulls/{id}/files)
  │   ├── Fetch PR description & metadata
  │   ├── Fetch file contents for changed files (full context)
  │   ├── Load review rules from .egg/review-rules.md (if exists)
  │   └── Assemble structured prompt
  │
  ├── Run egg Action (jwbron/egg/action@main)
  │   └── Claude Code reviews diff with review prompt
  │
  └── Post results
      ├── Inline comments on specific lines (gh api)
      └── Summary comment on PR
```

### Why Not Multi-Agent

The issue references Anthropic's Bughunter (find → verify → promote
pipeline). Multi-agent makes sense when verification is expensive or when
specialization improves accuracy significantly. For code review:

- A single Claude pass can handle security, standards, and quality together
- Verification of review comments doesn't need a separate agent — the
  reviewer can self-verify by reading surrounding code
- Multi-agent adds latency (sequential passes), cost (multiple API calls),
  and debugging complexity
- If review quality proves insufficient, we can add a verification pass
  later without changing the trigger/posting infrastructure

### Why Not a Webhook Server

The issue asks whether this should be a GitHub Action, pre-commit hook, or
egg-native feature. Recommendation: **GitHub Action**.

- egg already runs as a GitHub Action — the infrastructure exists
- No persistent server to maintain, no webhook endpoint to secure
- GitHub Actions handles concurrency, retries, and audit logging
- Same security model (gateway sidecar, credential isolation) applies
- Pre-commit hooks run locally and can't access the full PR context
  (description, linked issues, other files). They're better for formatting
  and simple lint (already handled by ruff, shellcheck, etc. in
  `.pre-commit-config.yaml`)

## Implementation Plan

### Phase 1: Single-Pass Reviewer (MVP)

**Goal:** Automatically review every PR with a single Claude pass that
covers security, standards, and quality.

#### 1.1 Review Prompt Builder (`action/build-review-prompt.sh`)

New script that constructs the review prompt. Separate from
`build-mention-prompt.sh` because the context and instructions differ.

**Inputs (from GitHub event context):**
- PR number, title, description
- Base and head branches
- Changed files with diffs (from `gh api pulls/{id}/files`)
- Full file contents for changed files (for surrounding context)
- Repository-level review rules (`.egg/review-rules.md` if present)

**Prompt structure:**
```
You are reviewing PR #{number}: "{title}" in {owner}/{repo}.

## PR Description
{description}

## Review Rules
{contents of .egg/review-rules.md, or default rules}

## Changed Files
{for each file: filename, status (added/modified/deleted), patch/diff}

## Full File Context
{for each modified file: complete current file contents}

## Instructions

Review this PR for:
1. Security issues (vulnerabilities, unsafe patterns, credential leaks)
2. Correctness (logic errors, edge cases, error handling gaps)
3. Code quality (readability, maintainability, naming)
4. Standards compliance (project conventions per review rules)

For each issue found, output a structured JSON block:
{
  "file": "path/to/file",
  "line": <line_number_in_file>,
  "severity": "critical|warning|suggestion",
  "category": "security|correctness|quality|standards",
  "comment": "Description of the issue and suggested fix"
}

The "line" field must be the actual line number in the file (as shown
in the GitHub file viewer on the HEAD commit), NOT a diff-relative
position.

If the PR looks good, output:
{"summary": "No significant issues found.", "comments": []}

Rules for comments:
- Only comment on things that are actually wrong or risky
- Do not comment on style preferences already handled by linters
- Do not repeat what ruff, mypy, shellcheck, or bandit would catch
- Focus on issues that require human judgment to detect
- Be specific: reference the exact line and explain why it's a problem
- Suggest a fix when possible
```

**Truncation limits:**
- Individual file diffs: 15,000 chars (skip very large generated files)
- Full file contents: 30,000 chars per file
- Overall prompt: 100,000 chars (Claude's context window is large, but
  cost scales with input size)
- Skip binary files, lock files, generated files (`.lock`, `.min.js`,
  `package-lock.json`, etc.)

**Prompt output mechanism:** The assembled prompt can be large (up to
100K chars). `$GITHUB_OUTPUT` uses multiline heredoc syntax and has
practical size limits (~1 MB) where large multiline values become
fragile. Instead of writing the prompt to `$GITHUB_OUTPUT`, the script
writes it to a temp file (`$RUNNER_TEMP/review-prompt.txt`) and outputs
only the file path:
```bash
PROMPT_FILE="$RUNNER_TEMP/review-prompt.txt"
# ... assemble prompt into $PROMPT_FILE ...
echo "prompt-file=$PROMPT_FILE" >> "$GITHUB_OUTPUT"
```
The workflow step then passes the file path to the egg Action, which
reads the prompt from the file. This avoids `GITHUB_OUTPUT` size limits
entirely and matches how `build-mention-prompt.sh` handles large
prompts. The egg Action's `prompt` input would accept either inline text
or a `file://` path (or a new `prompt-file` input could be added).

#### 1.2 Review Workflow (`.github/workflows/on-pull-request.yml`)

```yaml
name: "egg: Code Review"

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, reopened]

jobs:
  review:
    name: AI Code Review
    runs-on: ubuntu-latest
    # Skip draft PRs, bot PRs, and PRs with [skip-review] in title
    if: >-
      !github.event.pull_request.draft &&
      github.event.pull_request.user.login != 'james-in-a-box' &&
      github.event.pull_request.user.login != 'james-in-a-box[bot]' &&
      !contains(github.event.pull_request.title, '[skip-review]')

    permissions:
      contents: read
      pull-requests: write

    concurrency:
      group: egg-review-${{ github.event.pull_request.number }}
      cancel-in-progress: true  # Cancel stale reviews on new pushes

    steps:
      - name: Generate bot token
        id: bot-token
        uses: actions/create-github-app-token@v1
        with:
          app-id: ${{ secrets.BOT_APP_ID }}
          private-key: ${{ secrets.BOT_APP_PRIVATE_KEY }}

      - name: Checkout (trusted main for prompt building)
        uses: actions/checkout@v4
        with:
          ref: main

      - name: Build review prompt
        id: prompt
        run: bash action/build-review-prompt.sh
        env:
          GH_TOKEN: ${{ steps.bot-token.outputs.token }}
          PR_NUMBER: ${{ github.event.pull_request.number }}

      - name: Checkout PR branch
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.ref }}

      - name: Run egg review
        id: egg
        uses: jwbron/egg/action@main
        with:
          prompt-file: ${{ steps.prompt.outputs.prompt-file }}
          model: ${{ steps.prompt.outputs.model }}
          anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
          bot-app-id: ${{ secrets.BOT_APP_ID }}
          bot-app-private-key: ${{ secrets.BOT_APP_PRIVATE_KEY }}
          bot-app-installation-id: ${{ secrets.BOT_APP_INSTALLATION_ID }}
          bot-username: james-in-a-box
          timeout: 10  # Reviews should be fast

      - name: Post review comments
        if: always() && steps.egg.outputs.exit-code == '0'
        env:
          GH_TOKEN: ${{ steps.bot-token.outputs.token }}
        run: bash action/post-review-comments.sh
```

**Key design choices:**
- **`cancel-in-progress: true`** — when a new push arrives, cancel the
  running review. The old review is stale anyway.
- **Separate from on-mention.yml** — different trigger, different prompt,
  different purpose. Keeps workflows focused.
- **Skip bot's own PRs** — prevent self-review loops.
- **`[skip-review]`** — escape hatch for trivial PRs (docs-only, typo
  fixes).
- **10-minute timeout** — reviews should complete fast. If Claude needs
  more than 10 minutes, the PR is probably too large to review well.
- **Trusted checkout for prompt building** — same security pattern as
  `on-mention.yml`. Build the prompt from main branch code, then checkout
  the PR branch for the actual review.

#### 1.3 Review Comment Poster (`action/post-review-comments.sh`)

Parses Claude's JSON output and posts review comments via GitHub API.

**Approach:**
1. Read Claude's output from the log file (`${{ steps.egg.outputs.log-file }}`)
2. Extract JSON blocks (structured review comments)
3. For each comment with a file and line number, post as an inline PR
   review comment via `gh api`
4. Post a summary comment with overall assessment

**Log file format and parsing:** The egg Action's `log-file` output
contains Claude's raw response text. Claude may wrap JSON in markdown
code fences (`` ```json ... ``` ``), include preamble/postamble text,
or produce multiple JSON blocks. The parser must handle all of these:

```bash
# Extract JSON blocks from Claude's output, handling:
# 1. JSON wrapped in ```json ... ``` code fences
# 2. Bare JSON objects/arrays in the output
# 3. Preamble/postamble text around JSON
# Use grep + jq to find and validate JSON blocks:
grep -Pzo '(?s)\{[^{}]*"file"[^{}]*\}' "$LOG_FILE" | \
  jq -s '.' 2>/dev/null || echo '[]'
```

For robustness, the prompt asks Claude to wrap all review output in a
single top-level JSON object with a `comments` array. The parser first
tries to extract this structured object. If that fails (Claude deviated
from the format), it falls back to scanning for individual JSON comment
blocks. If all JSON extraction fails, the raw output is posted as a
plain PR comment (the fallback described below).

**GitHub API calls:**

The `gh api` CLI does not support array construction via repeated `-f`
flags. Instead, construct a JSON payload with `jq` and pass it via
`--input`:

```bash
# Build the review payload as JSON
REVIEW_PAYLOAD=$(jq -n \
  --arg event "COMMENT" \
  --arg body "$SUMMARY" \
  --argjson comments "$COMMENTS_JSON" \
  '{event: $event, body: $body, comments: $comments}')

# Post the review
echo "$REVIEW_PAYLOAD" | gh api repos/{owner}/{repo}/pulls/{pr}/reviews \
  -X POST \
  --input -
```

Where `$COMMENTS_JSON` is a JSON array built by the parser:
```json
[
  {
    "path": "src/auth.py",
    "line": 42,
    "side": "RIGHT",
    "body": "**security:** This input is not sanitized before..."
  }
]
```

**Line numbers vs. diff positions:** The GitHub Pull Request Reviews API
supports two modes for positioning inline comments:

1. **`position`** (legacy) — the line index within the diff hunk. This
   requires mapping file line numbers to diff-relative offsets, which is
   fragile and error-prone.
2. **`subject_type: "line"` with `line` and `side`** (available since
   2022) — accepts actual file line numbers. `side: "RIGHT"` refers to
   the new version of the file (head commit), `side: "LEFT"` refers to
   the old version (base commit).

This plan uses the newer `line`/`side` approach. The prompt instructs
Claude to output the file line number (not diff position), and the
comment poster sets `side: "RIGHT"` for all comments (since reviews
comment on the proposed code). This avoids the diff-position mapping
entirely.

The prompt's JSON output schema is updated accordingly:
```json
{
  "file": "path/to/file",
  "line": 42,
  "severity": "critical|warning|suggestion",
  "category": "security|correctness|quality|standards",
  "comment": "Description of the issue and suggested fix"
}
```
Where `line` is the **file line number in the head commit** (the number
shown in the GitHub file viewer), not a diff-relative position.

Using `event: COMMENT` (not `REQUEST_CHANGES` or `APPROVE`) — the bot
provides information, not blocking decisions. Humans decide whether to
act on the feedback.

**Fallback:** If JSON parsing fails (Claude didn't follow the format),
post the raw output as a single PR comment. This ensures the review is
never silently lost.

#### 1.4 Repository Review Rules (`.egg/review-rules.md`)

Optional per-repository configuration file that customizes review behavior.

```markdown
# Review Rules

## Focus Areas
- Security: Pay special attention to input validation in API handlers
- We use Django ORM exclusively — flag any raw SQL queries
- All new API endpoints must have rate limiting

## Ignore
- Don't comment on import ordering (ruff handles this)
- Don't comment on type annotations (mypy handles this)
- Migrations files are auto-generated — skip them

## Project Context
- This is a Django/React application
- Authentication uses Django REST Framework JWT
- We deploy on GCP Cloud Run
```

This file lives in the reviewed repository (not in egg), so each repo
can customize its review rules. The prompt builder reads it if present,
or uses sensible defaults.

### Phase 2: Specialized Review Modes

After the MVP is running and generating useful feedback, add specialized
modes that can be triggered explicitly or run as additional passes.

#### 2.1 Security-Focused Review

A review mode specifically tuned for security:
- Deeper analysis of authentication/authorization changes
- OWASP Top 10 pattern matching
- Dependency vulnerability awareness (cross-reference with
  `pip-audit`/`npm audit`)
- Secrets detection (complementing `trufflehog`/`gitleaks`)

**Trigger:** Automatically for PRs that touch security-sensitive files
(auth, middleware, API handlers, Dockerfiles, CI workflows), or manually
via `@egg security-review`.

**Integration with #70:** The security linters issue covers traditional
SAST tools (bandit, etc.). This AI review catches what static analysis
can't: logic-level auth bypasses, TOCTOU issues, subtle injection
vectors, and insecure-by-design patterns.

#### 2.2 Plan Verification Review

Compares PR changes against the stated plan (issue description, JIRA
ticket, or linked plan document):
- Does the PR implement what was planned?
- Are there changes that weren't in the plan (scope creep)?
- Are planned items missing from the PR?

**Trigger:** When PR description links to an issue or JIRA ticket, fetch
the linked content and include it in the prompt.

#### 2.3 Bounded-Context "Outsider" Review

A reviewer that deliberately operates without internal project knowledge:
- No `.egg/review-rules.md` loaded
- No project-specific context beyond what's in the diff
- Reviews from first principles: "Would a competent engineer unfamiliar
  with this codebase understand this code?"
- Surfaces documentation gaps, unclear naming, implicit assumptions

**Trigger:** Manual via `@egg outsider-review`. Useful for code that will
be maintained by people outside the original team.

### Phase 3: Review Infrastructure Improvements

#### 3.1 False Positive Management

Track which review comments get resolved vs. dismissed:
- Store review feedback outcomes (resolved, won't fix, false positive)
- Use feedback to refine prompts over time
- Add a reaction-based feedback mechanism: maintainer adds thumbs-down
  to false positive comments

**Implementation:** A simple JSON file in the repo
(`.egg/review-feedback.json`) or a GitHub issue that accumulates feedback
patterns. The prompt builder reads this to add "do not flag" patterns.

#### 3.2 Incremental Review

On `synchronize` events (new push to PR), only review the new changes:
- Diff between previous head and new head
- Skip files that haven't changed since last review
- Reference previous review comments to avoid repeating

The `pull_request.synchronize` event payload includes `before` and
`after` SHAs, which provide the previous and new head commits directly.
This means no external state storage is needed between runs — the
incremental diff can be computed as `git diff <before> <after>` using
values from the event payload (`github.event.before` and
`github.event.after`).

This reduces cost and noise for iterative PRs.

#### 3.3 Review Metrics Dashboard

Track review effectiveness:
- Number of comments per PR
- Comment resolution rate (acted on vs. dismissed)
- False positive rate
- Categories of issues found (security vs. quality vs. standards)
- Time to review

Surface via a periodic summary (weekly Slack notification or GitHub
issue).

### Phase 4: AI Lintbot Integration

For checks that are too nuanced for static analysis but too formulaic for
full review:

#### 4.1 Semantic Naming Checker
- Flag variables/functions with misleading names
- Detect name/behavior mismatches (e.g., `is_valid()` that returns a
  string)
- Suggest more descriptive names for single-letter variables in non-trivial
  scope

#### 4.2 Logic Correctness Beyond Types
- Off-by-one errors in loops
- Null/undefined paths that type systems miss
- Race conditions in async code
- Resource leaks (open files, unclosed connections)

#### 4.3 API Usage Antipatterns
- N+1 query patterns in ORM code
- Blocking calls in async contexts
- Unbounded queries without pagination
- Cache invalidation gaps

**Implementation:** These run as additional prompt modes within the same
GitHub Action infrastructure. Each is a specialized prompt template
(`action/prompts/naming.md`, `action/prompts/logic.md`, etc.) that the
workflow selects based on configuration.

## Integration with Existing Issues

### #70 — Security Linters in CI

Issue #70 covers adding traditional security linters (bandit, custom
scripts) to `make lint`. AI review complements this:
- Static linters catch syntactic patterns (hardcoded passwords, dangerous
  function calls)
- AI review catches semantic patterns (logic-level auth bypasses, insecure
  design decisions)
- No overlap: the AI reviewer is explicitly instructed to skip issues that
  bandit/ruff would catch

### #77 — Autofixers in CI

Issue #77 covers automated fixing of lint issues. AI review could feed
into this:
- Phase 1: Review comments are informational only
- Future: For high-confidence suggestions (e.g., "this variable should be
  renamed from `x` to `user_count`"), the bot could open a fix PR
  against the reviewed PR's branch
- This requires careful scoping — autofixes should be limited to
  mechanical changes, not logic rewrites

## Deliverables Summary

| Phase | Deliverable | New Files | Modifies |
|-------|------------|-----------|----------|
| 1 | Review prompt builder | `action/build-review-prompt.sh` | — |
| 1 | Review workflow | `.github/workflows/on-pull-request.yml` | — |
| 1 | Comment poster | `action/post-review-comments.sh` | — |
| 1 | Review rules spec | Documented convention for `.egg/review-rules.md` | — |
| 2 | Security review mode | `action/prompts/security-review.md` | `build-review-prompt.sh` |
| 2 | Plan verification mode | `action/prompts/plan-verify.md` | `build-review-prompt.sh` |
| 2 | Outsider review mode | `action/prompts/outsider-review.md` | `build-review-prompt.sh` |
| 3 | Feedback tracking | `.egg/review-feedback.json` convention | `build-review-prompt.sh` |
| 3 | Incremental review | — | `on-pull-request.yml`, `build-review-prompt.sh` |
| 3 | Metrics dashboard | `action/review-metrics.sh` | — |
| 4 | AI lintbot prompts | `action/prompts/naming.md`, etc. | `on-pull-request.yml` |

## Open Questions

1. **Cost management:** Each review costs an API call. For active repos
   with many PRs, this adds up. Should we add a daily/weekly budget cap?
   Or limit reviews to PRs from certain authors?

2. **Review comment threading:** ~~Should the bot reply to its own previous
   comments when updating a review (on new push), or delete old comments
   and post fresh?~~ **Resolved:** On new pushes, dismiss (resolve) the
   bot's previous review and post a fresh review. The GitHub API supports
   dismissing reviews via `PUT /repos/{owner}/{repo}/pulls/{pr}/reviews/{id}/dismissals`.
   This keeps the PR timeline clean (old reviews are collapsed/resolved)
   while preserving history (dismissed reviews remain visible in the
   timeline if someone wants to see them). The comment poster should
   query for existing bot reviews and dismiss them before posting the new
   one. This is simpler than threading (no need to match old comments to
   new ones) and avoids the clutter of accumulated stale reviews.

3. **Blocking vs. advisory:** The MVP uses `COMMENT` review events
   (advisory only). Should critical security findings use
   `REQUEST_CHANGES` to block merge? This would give the AI reviewer
   veto power, which is a significant trust escalation.

4. **Model selection:** ~~Reviews don't need the most powerful model for
   every PR.~~ **Resolved:** Use the existing `model` input on the egg
   Action to parameterize model selection. Default to Haiku for PRs with
   5 or fewer changed files, Opus for larger PRs. The threshold is
   configurable via `.egg/review-rules.md` (e.g.,
   `model-threshold-files: 10`). The prompt builder counts changed files
   and sets the model accordingly in the workflow output. This keeps
   costs low for small PRs while using a more capable model when the
   review is likely to benefit from it.

5. **Review of egg's own PRs:** Should the egg repo itself use this
   reviewer? Dogfooding would be valuable, but the bot reviewing its own
   codebase creates a feedback loop that needs careful handling.

## Recommendation

Start with Phase 1 (single-pass reviewer via GitHub Action). This:
- Leverages existing infrastructure (egg Action, gateway, Claude Code)
- Requires only 3 new files + 1 workflow
- Delivers value immediately on every PR
- Provides a foundation for all later phases
- Is reversible (disable the workflow if it's not useful)

Phase 1 implementation can be done as a single PR. The deliverables are:
1. `action/build-review-prompt.sh`
2. `action/post-review-comments.sh`
3. `.github/workflows/on-pull-request.yml`
4. Documentation for `.egg/review-rules.md` convention

Authored-by: egg
