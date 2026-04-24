---
name: babysit-pr
description: "Run a one-off implement-phase BRC cycle against an open GitHub PR."
disable-model-invocation: true
argument-hint: "<pr-number-or-url> [--repo owner/name]"
---

# Babysit-PR

You are guiding the user through a single implement-phase BRC cycle against an
existing GitHub pull request. The pipeline reuses the same role-typed
producers (coder, tester, documenter), role-typed reviewers (`reviewer_code`),
file-scoped writes, and Broadcast-Review-Converge consensus that the
[full SDLC pipeline](../sdlc/SKILL.md) uses — it just drops refine/plan and
targets the PR diff instead of a GitHub issue.

This skill is the replacement for the legacy `egg-babysit` CLI, which drove
an untyped fixer/reviewer polling loop with no BRC consensus. The legacy CLI
has been removed; this skill is the **only** supported way to invoke a
babysit run against an existing PR.

## Argument Parsing (before any phase)

Parse the arguments provided after `/babysit-pr`:

| Input | Interpretation |
|-------|----------------|
| `/babysit-pr 42` | PR number (bare integer) |
| `/babysit-pr #42` | PR number (with hash) |
| `/babysit-pr https://github.com/jwbron/egg/pull/42` | Full PR URL — parse owner, repo, and number |
| `/babysit-pr 42 --repo owner/name` | Explicit repo override |

### PR URL detection

Any argument starting with `http://` or `https://` is treated as a PR URL.
Extract `owner`, `repo`, and `pr_number` from URLs matching
`https://github.com/<owner>/<repo>/pull/<N>`. If parsing fails, ask the user
to supply a bare PR number instead.

### Repo detection

If a bare PR number is supplied, auto-detect the repo the same way
`/sdlc` does:

1. Run `git -C "$EGG_REPO_PATH" remote get-url origin 2>/dev/null` (or fall
   back to `git remote -v` from the working directory).
2. Parse the `owner/name` from the URL (e.g. `https://github.com/my-org/my-repo.git`
   → `my-org/my-repo`).
3. If a `--repo` flag was passed, use that instead.

Only ask for the repo if detection fails AND no `--repo` flag was provided.

## Phase 1 — Seed

Collect the **PR number** and **repository**. Your goal is **zero questions**
on the happy path and **at most one question to get started** otherwise.

If no arguments were supplied, ask a **single** `AskUserQuestion`:

- **Question**: "Which PR should be babysat? Paste a PR URL or type a bare PR number below."
- **Header**: "PR"
- **Options**:
  - **"Browse recent PRs"** — description: "List recent open PRs to pick from"

Handle each response:

- **Other (starts with `http://` / `https://`)** → Treat as a PR URL. Parse and proceed.
- **Other (integer or `#N`)** → Treat as a PR number. Proceed (repo auto-detected).
- **Browse recent PRs** → Run `gh pr list --repo <repo> --state open --limit 10 --json number,title,baseRefName,isDraft` and present the results as a second `AskUserQuestion` with each PR as an option. Draft PRs should be flagged with `[draft]` in the option label.

## Phase 1.5 — PR readiness check

Before submitting, fetch the PR's current state with:

```bash
gh pr view <pr_number> --repo <repo> --json state,baseRefName,headRefOid,isDraft,mergeable,isCrossRepository,mergedAt,closedAt
```

Inspect the response and bail out early on any of these conditions:

| State | Action |
|-------|--------|
| `state == "MERGED"` | Inform the user the PR is already merged; exit without submitting. |
| `state == "CLOSED"` | Inform the user the PR is closed; offer to reopen it manually, then exit. |
| `isCrossRepository == true` (fork PR) | Inform the user the gateway cannot push to fork branches; exit. |
| `isDraft == true` | Ask the user to confirm (`AskUserQuestion`) before proceeding — draft PRs are supported, but cheaper to mark ready-for-review first. |

These checks mirror the orchestrator's early-exit logic. Catching them
client-side avoids a round-trip to create a pipeline that the server would
immediately reject. The **definitive** early-exit check still runs on the
orchestrator side during pipeline creation — this client check exists only
to give the user a fast, clear error.

If `baseRefName` is not `main`, note it — the producers and reviewers will
orient on `baseRefName...headRefOid` instead of `origin/main...HEAD`. No
action needed from the user.

## Phase 1.6 — Confirm

Show a one-screen confirmation with the resolved parameters:

```
Babysit-PR: #<pr_number> — <pr_title>
Repo:      <repo>
Base:      <baseRefName>
Head SHA:  <short_head_sha>
Draft:     <yes|no>

Producers: coder, tester, documenter
Reviewer:  reviewer_code
Mode:      implement-phase BRC cycle (no refine/plan)
```

Ask a `AskUserQuestion`:

- **Question**: "Submit this babysit-pr pipeline?"
- **Header**: "Submit"
- **Options**:
  - **"Submit"** — description: "Create the pipeline and start monitoring"
  - **"Cancel"** — description: "Abort without creating the pipeline"

If the user cancels, exit cleanly. If the user confirms, proceed to Phase 2.

## Phase 2 — Submit

Call the orchestrator REST API directly (there is no `submit_task` MCP
overload for babysit-pr yet — the skill issues a plain `POST` via the
orchestrator endpoint):

```bash
curl -X POST "${EGG_ORCHESTRATOR_URL:-http://localhost:9849}/api/v1/pipelines" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "babysit",
    "pr_number": <pr_number>,
    "repo": "<repo>"
  }'
```

The orchestrator will:

- Re-fetch the PR state and re-validate the early-exit conditions.
- Auto-derive `pipeline_id = "pr-<pr_number>"`.
- Set `has_contract = false` (no SDLC contract exists for a PR-targeted
  cycle — `reviewer_contract` is filtered out of the implement-phase roster).
- Create a staging branch rooted at the PR head and spawn the producers and
  `reviewer_code` against it.

### Response handling

| Status | Meaning | Action |
|--------|---------|--------|
| `201 Created` | Pipeline created | Store the returned `task_id` / `pipeline_id`, proceed to Phase 3 (Monitor). |
| `400 Bad Request` | Early-exit: fork PR, merged/closed PR, or empty `base...head` diff. Body explains which. | Surface the server message to the user verbatim; exit. No PR comments are posted on early-exit per the babysit-pr design (see [Babysit-PR Guide § Early Exits](../../docs/guides/babysit-pr.md#early-exits)). |
| `409 Conflict` | A `pr-<pr_number>` pipeline already exists (active or not yet cleaned up). Only one babysit cycle can run per PR at a time. | Inform the user: "A babysit-pr pipeline is already running for PR #<N>. Cancel it first with `egg-orch pipeline cancel pr-<N>`, or wait for it to complete." Exit. |
| Other | Unexpected server error | Surface the error and exit. |

Store the returned `pipeline_id` (`pr-<pr_number>`) and confirm submission:

> Babysit-PR pipeline submitted.
> **Pipeline**: `pr-<pr_number>` | **Branch**: `egg/babysit-pr/<pr_number>/<short_sha>/...`
> **Base**: `<baseRefName>` | **Head**: `<short_head_sha>`

## Phase 3 — Monitor

Hand the pipeline off to `egg-pipeline-watch` for live monitoring. This is
the same watcher the `/sdlc` skill uses in its monitoring phase — behaviour
and output are identical:

```bash
egg-pipeline-watch pr-<pr_number>
```

Alternatively, call the `get_status` MCP tool in a poll loop (same pattern
as [`/sdlc` Phase 3 — Monitor](../sdlc/SKILL.md#phase-3--monitor)). The
server-computed `phase_elapsed_seconds` field, overseer-alert handling,
consensus-tracking fallback, and failed-status grace period all apply
unchanged.

Key behavioural differences from `/sdlc`:

- **No refine/plan phases** — the pipeline starts directly at `implement`.
- **No `reviewer_contract`** — the implement-phase roster is filtered when
  `has_contract=false`. Expect `coder`, `tester`, `documenter`, and
  `reviewer_code` only.
- **Staging-branch churn is invisible** — proposers force-push their
  staging branches during BRC rounds. The **PR head branch receives exactly
  one commit** at the end, when consensus is reached.
- **Final-push head-move guard** — if a human commit lands on the PR head
  between consensus and the final push, the push aborts and a HITL
  escalation is raised. The pipeline does **not** force-push over human
  work. Resolve the escalation (typically: cancel the pipeline and start a
  fresh cycle against the new head) via the standard HITL flow.

## Phase 4 — HITL

If the pipeline raises a HITL decision, follow the same handler that
`/sdlc` uses — see [`/sdlc` Phase 4 — HITL](../sdlc/SKILL.md#phase-4--hitl).
Common babysit-specific HITL scenarios:

- **Final-push head-move** — a human committed to the PR head mid-cycle.
  The pipeline's final commit is rejected; the user must decide whether to
  cancel and re-run or merge the staging branch manually.
- **Cross-role file overlap** — producers detected overlap in the file
  scopes of multiple roles and requested the on-demand `conflict_resolver`
  role. Normally resolved by the producers themselves; HITL only fires if
  the resolver also fails.

## Phase 5 — Complete

On successful consensus and final push:

- The PR head branch carries one new commit with the consensus diff.
- `.egg-state/brc-history/pr-<pr_number>-<short_sha>-implement.{md,json}`
  is written on the branch so the PR carries a durable trail of what was
  raised and addressed. The content-addressed suffix (`<short_sha>`) means
  multiple babysit cycles on the same PR over time produce distinct history
  files instead of overwriting one another.
- **No PR comment is posted.** The final commit and the BRC-history
  files on the branch are the only artifacts written back to the PR.
  The orchestrator does not currently mirror the issue-mode "summary
  comment" behaviour for babysit cycles — reviewers consult
  `.egg-state/brc-history/...` on the branch or the pipeline status
  (`egg-orch pipeline status pr-<N>`) to see what was raised and
  addressed.

Inform the user:

> Babysit-PR complete for #<pr_number>.
> **Final commit**: `<short_sha>`
> **BRC history**: `.egg-state/brc-history/pr-<pr_number>-<short_sha>-implement.md`

Exit cleanly.

## Relationship to `/sdlc`

`/sdlc` runs the full refine → plan → implement lifecycle against a GitHub
issue. `/babysit-pr` runs only the implement phase against an existing PR
— think of it as `/sdlc --implement-only` with the PR diff as the input
contract.

Both skills invoke the same orchestrator route (`POST /api/v1/pipelines`)
and the same implement-phase agent roles and BRC protocol. The differences
are:

| | `/sdlc` | `/babysit-pr` |
|-|---------|---------------|
| Input | GitHub issue or JIRA ticket | Existing open PR |
| Phases | refine → plan → implement | implement only |
| Contract | Built by plan phase | None (`has_contract=false`) |
| Reviewers | `reviewer_code` + `reviewer_contract` + others per phase | `reviewer_code` only (in implement phase) |
| Output | New PR | Additional commit on existing PR |
| Pipeline ID | `issue-<N>` / `<JIRA>` | `pr-<N>` |
| Base branch | Usually `main` | Taken from `pr.base.ref` (may be non-`main`) |

## Deprecation note — legacy `egg-babysit` CLI

The standalone `egg-babysit` console script and its `shared/egg_babysit/`
package have been removed. If `uv run egg-babysit --help` or similar
commands appear in any team-local scripts, Makefile targets, or CI
workflows, migrate them to this skill. The replacement is functionally
equivalent at the "run against one PR" granularity — the recurring-cadence
and webhook-driven variants of the old CLI have no counterpart in this
first cut and should be re-opened as a follow-up issue if needed.

See [Babysit-PR Guide](../../docs/guides/babysit-pr.md) for the operational
reference, the full early-exit table, and the contract / decision trace
from issue #1748.
