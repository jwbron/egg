# Babysit-PR Guide

Run a one-off implement-phase BRC cycle against an existing GitHub pull
request. Role-typed producers (coder, tester, documenter) improve the PR,
a role-typed reviewer (`reviewer_code`) gates the result via the Broadcast-
Review-Converge consensus protocol, and the final consensus commit is
pushed to the PR head in a single force-free push.

## What It Does

`babysit-pr` takes an open PR and runs it through the same implement-phase
machinery the [SDLC pipeline](sdlc-pipeline.md) uses — minus the refine and
plan phases, and targeted at the PR's diff instead of a contract from a
plan document.

Concretely, one `babysit-pr` invocation:

1. Validates the PR is open, same-repo (not a fork), non-empty relative to
   its base branch, and has no existing `pr-<N>` pipeline.
2. Creates a staging branch rooted at the PR head.
3. Spawns `coder`, `tester`, `documenter`, and `reviewer_code` agents in
   their own per-role worktrees off the staging branch.
4. Runs the full BRC consensus protocol (PROPOSE → ACK/NACK → CONFIRM)
   with role-typed file-access boundaries enforced by the gateway.
5. On consensus, re-verifies the PR head SHA hasn't moved, fast-forwards
   the staging branch into the PR head branch, and pushes one commit.
6. Writes the BRC-history trail to
   `.egg-state/brc-history/pr-<N>-<short-sha>-implement.{md,json}`
   so the PR carries a durable, content-addressed record of what was
   raised and addressed. babysit-pr is one of the
   [non-slice implement runs](concurrent-execution.md#brc-history-link-in-pr-body)
   that emit a single content-addressed file rather than the
   per-slice `<id>-implement-slice-<N>.{md,json}` files that
   issue-mode pipelines now produce after
   [#2548](https://github.com/jwbron/egg/issues/2548).

The intent is **quality / consistency improvement, not just gating.** A PR
that passes CI and has no reviewer blockers should still come out of a
babysit cycle with better tests, clearer docs, and tighter code than it
went in with — because the producers are given room to improve it, not
just fix it.

## Usage

### `/babysit-pr` MCP skill (recommended)

The entry point is the [`/babysit-pr` skill](../../skills/babysit-pr/SKILL.md).
Invoke it from inside a Claude Code session:

```
/babysit-pr 42
/babysit-pr https://github.com/jwbron/egg/pull/42
/babysit-pr 42 --repo owner/name
```

The skill walks through seed → readiness-check → confirm → submit → monitor
→ complete in one flow. Full behavioural reference:
[`skills/babysit-pr/SKILL.md`](../../skills/babysit-pr/SKILL.md).

### Direct orchestrator API

For scripted invocations, call the orchestrator REST API directly:

```bash
curl -X POST "${EGG_ORCHESTRATOR_URL:-http://localhost:9849}/api/v1/pipelines" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "babysit",
    "pr_number": 42,
    "repo": "owner/name"
  }'
```

On success, the response carries the auto-derived `pipeline_id` (`pr-42`).
See the [SDLC Pipeline Guide](sdlc-pipeline.md) for the full response schema
and monitoring endpoints (`GET /api/v1/pipelines/<id>/status`, etc.) — the
schema is identical to issue-mode pipelines.

## How the Cycle Works

```
┌─────────────────────────────────────────────────────────────────┐
│                    BABYSIT-PR CYCLE                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  /babysit-pr <N>                                                │
│       │                                                         │
│       ▼                                                         │
│  ┌──────────────────────┐                                       │
│  │ Client readiness ck  │ merged/closed/fork/empty diff? → exit │
│  └──────────┬───────────┘                                       │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ POST /api/v1/pipe…   │ → 201 (pr-<N>) | 400 early-exit      │
│  │ mode=babysit         │    | 409 duplicate                   │
│  └──────────┬───────────┘                                       │
│             ▼                                                   │
│  ┌──────────────────────┐   orient on base…head                 │
│  │ Spawn implement-     │   (reviewers), rebase & resolve      │
│  │ phase roster:        │   own-role conflicts (producers)      │
│  │ coder + tester +     │                                       │
│  │ documenter +         │                                       │
│  │ reviewer_code        │                                       │
│  └──────────┬───────────┘                                       │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │  BRC consensus loop  │  PROPOSE → ACK/NACK → CONFIRM         │
│  │  on STAGING branch   │  (force-pushes stay on staging)       │
│  └──────────┬───────────┘                                       │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ Final-push head-move │ PR head moved? → abort + HITL         │
│  │ guard                │ unchanged? → push once                │
│  └──────────┬───────────┘                                       │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ BRC history written  │                                       │
│  │ to branch            │                                       │
│  └──────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────┘
```

### Orientation

- **Reviewers** (`reviewer_code`) orient by reading `base…head` of the PR
  against the PR's configured base branch (from `pr.base.ref`, **not**
  hardcoded to `main`). This seeds their mental model with the change
  under scrutiny so they can react quickly to the first proposal. This
  happens in parallel with producer orientation.
- **Producers** (coder, tester, documenter) each check out the PR branch
  into their own worktree rooted at the staging branch. As their first
  orientation step, each producer rebases / merges `pr.base.ref` into
  their worktree and resolves conflicts **within the files in their own
  role's scope.** Cross-role overlap (rare — the coder/tester/documenter
  file scopes are strictly disjoint) is detected by the producer during
  conflict resolution and escalated on-demand to the `conflict_resolver`
  role.

Producers are also given a soft scope-expansion hint in their orient
prompt: *"do not refactor outside the diff unless clearly needed."* There
is no hard cap on bytes or files — if the role prompts produce runaway
scope expansion in practice, that's a signal to revisit the prompts rather
than add a rigid limit.

### First BRC round

Producers emit the first proposal. If the PR had conflicts against its
base branch, this proposal carries the conflict-resolved version. If the
PR was clean and the producers didn't identify any improvements during
orientation, the producers propose the existing head commit unchanged.

Reviewers then ACK/NACK in the normal BRC pattern. Their diff-orientation
in the previous step means they can react quickly to the first proposal.

This keeps BRC vanilla — there is no protocol extension for a
reviewer-first round.

### Converge

Normal BRC loop: producers propose → reviewers ACK/NACK → producers
address → repeat until BRC reaches consensus or triggers HITL escalation.
BRC owns the exit condition; there is no babysit-level iteration cap or
timeout.

**All work happens on a staging branch**, not on the PR branch. The
staging branch is force-pushed during the BRC cycle (internal to the
orchestrator); only the final consensus commit is pushed to the PR head
branch. This avoids racing with human commits to the PR mid-cycle.

### Final push

On `CONSENSUS_CONFIRMED` for the whole roster, the orchestrator:

1. Re-fetches the PR head SHA via `gh pr view --json headRefOid`.
2. Compares it against the anchor SHA (the one used to derive the staging
   branch and BRC-history identifier).
3. If unchanged → fast-forwards the staging branch into the PR head branch
   and pushes once.
4. If changed → aborts the push and raises a HITL escalation. The
   escalation body includes both SHAs and a suggested follow-up (usually:
   cancel the pipeline, wait for the human commits to settle, and re-run
   `/babysit-pr`). This is the only head-move check the orchestrator
   performs — per-NACK polling is avoided intentionally.

## Early Exits

Several PR states bypass pipeline creation entirely. These are checked
both client-side (in the `/babysit-pr` skill) and server-side (in the
pipeline-creation route). The server-side check is authoritative; the
client-side check exists only to give the user a fast, clear error.

| State | Behaviour | Where checked |
|-------|-----------|---------------|
| PR is `MERGED` | HTTP 400 + stderr message. No PR comment posted. | Client + server |
| PR is `CLOSED` (not merged) | HTTP 400 + stderr message. No PR comment posted. | Client + server |
| PR is from a fork (`isCrossRepository`) | HTTP 400 + stderr message explaining the gateway cannot push to fork branches. No PR comment posted. | Client + server |
| `base…head` diff is empty | HTTP 400 + stderr message. No PR comment posted. | Server |
| `pr-<N>` pipeline already exists | HTTP 409 + message instructing the user to cancel the existing pipeline first. | Server |

All early-exit error paths are **stderr-only** — no PR comments are posted
on any error path. This is deliberate: an unhelpful comment on a merged
or fork PR is worse than a silent exit the user can diagnose from their
own terminal.

## Pipeline Metadata

| Field | Value | Notes |
|-------|-------|-------|
| `mode` | `babysit` | Repurposed in issue #1748 — silent semantic swap from the legacy loop meaning to the BRC-cycle meaning. |
| `pipeline_id` | `pr-<N>` | Auto-derived from `pr_number`. |
| `branch` | `egg/babysit-pr/<N>/<short-sha>/<role>` (per role) | Staging branches; one per agent. |
| `phase` | `implement` | No refine or plan. |
| `has_contract` | `false` | Drops `reviewer_contract` from the implement-phase roster. |
| `base.ref` | Taken from `pr.base.ref` | **Not** hardcoded to `main`. Reviewer and producer orient prompts and health checks all honour this. |
| BRC-history id | `pr-<N>-<short-sha>-implement` | Content-addressed — multiple cycles on the same PR over time produce distinct history files. babysit-pr does **not** use the per-slice `<id>-implement-slice-<N>` partition introduced for issue-mode pipelines in [#2548](https://github.com/jwbron/egg/issues/2548): a babysit cycle has no slices, so the run is one of the [non-slice implement runs](concurrent-execution.md#brc-history-link-in-pr-body) that retain the single-file format. |

### Agent roster

| Role | Orient | Writes | Reviews |
|------|--------|--------|---------|
| `coder` | Rebase + resolve conflicts within source-file scope | `**/*.py`, `**/*.ts`, etc. (per role restrictions) | — |
| `tester` | Rebase + resolve conflicts within test scope | `tests/`, `**/*_test.py`, etc. | — |
| `documenter` | Rebase + resolve conflicts within docs scope | `docs/`, `**/README.md`, `**/*.md` | — |
| `reviewer_code` | Read `base…head` of the PR | — | ACK/NACK proposals from all three producers |
| `conflict_resolver` (on-demand) | Invoked by producers when cross-role overlap is detected | Role-agnostic (scoped to the overlap) | — |

Notably absent: `reviewer_contract`, `reviewer_refine`, `reviewer_agent_design`.
`reviewer_contract` is filtered out because `has_contract=false`; the refine
and agent-design reviewers operate on refine/plan artifacts that a babysit
cycle does not produce.

## Orchestrator Integration

`babysit-pr` pipelines appear in the orchestrator exactly like issue-mode
pipelines:

- **`egg-orch pipeline status pr-<N>`** — status snapshot.
- **`GET /api/v1/pipelines/pr-<N>/status`** — status polling.
- **`GET /api/v1/pipelines/pr-<N>/visualization`** — DAG visualization.
- **`egg-orch decision list pr-<N>`** — HITL decisions for the cycle.
- **Health monitoring** — both [Tier-1 deterministic tripwires](pipeline-health-monitoring.md)
  and the [overseer agent](pipeline-health-monitoring.md#overseer) are enabled.
- **Checkpoint browser** — `egg-checkpoint list --pipeline pr-<N>` and
  friends all work.

See the [SDLC Pipeline Guide](sdlc-pipeline.md) for the full API surface.

## Concurrency

Only one babysit cycle can run per PR at a time. The pipeline ID is
`pr-<N>` (not qualified), so a second `/babysit-pr <N>` invocation while
the first is still active returns HTTP 409 with a message instructing the
user to cancel the existing pipeline first:

```bash
egg-orch pipeline cancel pr-<N>
```

This is intentional — a lock file or queue would complicate the flow with
no clear benefit. The user chooses whether to let the first cycle finish
or cancel it and retry.

## Gateway Requirements

The gateway sidecar enforces branch policies. For `babysit-pr` to push the
final consensus commit to a PR branch, one of the following must hold:

- The bot has an open PR on that branch (standard self-owned branch
  policy), **OR**
- The user is in `GATEWAY_TRUSTED_USERS` / `TRUSTED_BRANCH_OWNERS`.

No gateway changes are needed for babysit-pr — this uses existing push
policies. See the [Architecture Overview](../architecture/README.md) for
details on gateway enforcement.

Fork PRs are rejected at both early-exit layers because the gateway
cannot push to fork branches.

## Health Monitoring and Escalation

Babysit cycles inherit the same [two-tier health monitoring](pipeline-health-monitoring.md)
as issue-mode pipelines: Tier-1 deterministic tripwires detect obvious
failures (heartbeat timeouts, progress stalls, phase-output anomalies),
and the Tier-2 overseer agent classifies ambiguous cases with LLM
judgment.

Typical escalation triggers specific to babysit:

- **Final-push head-move** — human commit landed on PR head mid-cycle;
  push aborted.
- **BRC consensus deadlock** — producers and reviewer cannot converge
  after repeated NACK rounds (standard BRC escalation).
- **Cross-role conflict_resolver failure** — on-demand resolver could not
  untangle overlap between coder/tester/documenter file scopes.
- **Unresolvable merge conflict** — a producer cannot rebase / merge
  `pr.base.ref` into their worktree even within their own role's scope.

All escalations route through the orchestrator's DecisionQueue. HITL
decisions are surfaced through the orchestrator's web UI and CLI
(`egg-orch decision list pr-<N>`); the pipeline blocks until a human
resolves them. The queue does **not** automatically post GitHub
comments on the PR — the decision is visible via the orchestrator
surfaces and, if configured, via external notification handlers (e.g.
Slack). The final-consensus commit itself becomes the only automatic
artifact written back to the PR; the durable BRC-history trail lives
on the branch under `.egg-state/brc-history/` (as the single
content-addressed `pr-<N>-<short-sha>-implement.{md,json}` pair, not
the per-slice `<id>-implement-slice-<N>` files an issue-mode pipeline
emits — see [#2548](https://github.com/jwbron/egg/issues/2548)) so
reviewers can read it alongside the diff.

## What Changed (Migration Notes)

Issue #1748 replaced the entire legacy `shared/egg_babysit/` PR-maintenance
loop with the BRC-driven cycle documented above. The swap is effectively a
ground-up rewrite, not a layering:

| Before (legacy loop) | After (babysit-pr BRC cycle) |
|----------------------|------------------------------|
| `egg-babysit <N>` console script | `/babysit-pr <N>` MCP skill (the console script is removed) |
| Untyped "fixer" and "reviewer" agents | Role-typed `coder`, `tester`, `documenter`, `reviewer_code` |
| No file-access restrictions — identity lived only in the prompt | Gateway-enforced per-role file boundaries |
| No BRC consensus; the loop drove decisions unilaterally | Full Broadcast-Review-Converge protocol with HITL escalation |
| Poll-driven state machine: conflict → CI-wait → check-fix → review → feedback → loop | One-shot implement-phase cycle; CI failures are handled by the producers as part of orientation |
| Base branch hardcoded to `main` in prompts and health checks | Base taken from `pr.base.ref` (`get_pr_base_branch()` helper) throughout |
| Pipeline ID `pr-<N>`, mode `babysit`, but ran its own loop | Pipeline ID `pr-<N>`, mode `babysit` (repurposed), runs through the standard implement-phase BRC path |
| CI failures handled by a separate pre-stage with retries | CI failures observed by the coder/tester during orientation and addressed in BRC proposals |

The legacy CLI (`egg-babysit`), its `shared/egg_babysit/` Python package
and tests, and the `egg-babysit` console-script entry in
`shared/pyproject.toml` are removed. There is **no deprecation shim** —
the legacy command exits non-zero with `No matching command "egg-babysit"`
after upgrade.

In-flight pipelines from before the migration have no compatibility path
to the new flow, because the entire state machine is gone. Drain or
cancel any existing `mode=babysit` pipelines before merging the #1748
change.

## Limitations

- **Single-shot per invocation** — one `/babysit-pr <N>` runs one BRC
  cycle. If the cycle completes and a human then commits more changes to
  the PR, a follow-up `/babysit-pr <N>` is required to re-run. A
  recurring / webhook-driven variant is explicitly out of scope for this
  first cut.
- **No PR creation** — `babysit-pr` monitors an existing PR. To create a
  PR from scratch, use `/sdlc`.
- **No force push to PR head** — the final push is a fast-forward only.
  If the PR head moved during the cycle, the push aborts and escalates
  rather than overwriting human work.
- **Concurrent invocations blocked** — only one `pr-<N>` pipeline can be
  active at a time. Cancel the existing one to re-run.

## Contract / Decision Trace

Issue #1748's refine-phase HITL gate resolved 13 open design questions
before implementation began. Each resolution shaped a concrete behaviour
in the flow documented above. For future readers who want to understand
why a particular design choice was made without chasing the contract, the
resolutions are enumerated below with pointers to the behaviour they
drive.

| ID | Resolved answer | Where it shows up |
|----|-----------------|-------------------|
| **D1** | Repurpose `PipelineMode.BABYSIT` — silent semantic swap to mean "babysit-pr" | Pipeline metadata `mode=babysit`; no new enum value required. See [Pipeline Metadata](#pipeline-metadata). |
| **D2** | Add `Pipeline.has_contract` field; `get_roles_for_phase()` reads it to drop `REVIEWER_CONTRACT` when absent | Agent roster (`reviewer_contract` filtered out). See [Agent roster](#agent-roster). |
| **D3** | Lean MCP-skill scope — PR number/URL + single confirmation, no `--short`/full split | `/babysit-pr` skill has one flow (no mode flag). See [`skills/babysit-pr/SKILL.md`](../../skills/babysit-pr/SKILL.md). |
| **D4** | Mid-cycle human commits: ignore until consensus; on final push, if PR head moved, abort push and escalate via HITL | Final-push head-move guard. See [Final push](#final-push). |
| **D5** | `conflict_resolver` policy: on-demand only. Producers detect cross-role overlap during their own conflict resolution and request the resolver | Orientation phase conflict handling. See [Orientation](#orientation). |
| **D6** | Base-branch parameterization: full sweep. Fix every hardcoded `origin/main` in production code (prompts, health checks) | `get_pr_base_branch()` helper used throughout. See [Pipeline Metadata](#pipeline-metadata) (`base.ref` row). |
| **D7** | Don't refactor orient builders up front — inline the babysit branch; revisit when a third mode lands | Implementation-level detail; flow diagram and per-role orient semantics are unchanged by this choice. |
| **F8** | Additional reviewer pre-filters: only `reviewer_contract`. `reviewer_refine` and `reviewer_agent_design` operate on refine/plan artifacts that don't exist in babysit, so they're naturally absent | Agent roster. See [Agent roster](#agent-roster). |
| **F9** | Fork-PR UX: fail-fast with stderr-only error; no PR comment, no HITL | Early-exit table. See [Early Exits](#early-exits). |
| **F10** | Concurrent invocations: share pipeline-id `pr-<N>` and 409 the second. No new uniqueness scheme | Concurrency section. See [Concurrency](#concurrency). |
| **F11** | Scope-expansion guardrails: soft orient-prompt hint only — "do not refactor outside the diff unless clearly needed" | Producer orient prompt. See [Orientation](#orientation). |
| **F12** | BRC-history identifier: `pr-<N>-<short-sha>`. Content-addressed so multiple cycles on the same PR over time don't collide | BRC-history naming. See [Pipeline Metadata](#pipeline-metadata) (BRC-history id row). |
| **F13** | Remove `egg-babysit` CLI entirely; migrate docs in the same PR; no deprecation shim | Migration notes. See [What Changed (Migration Notes)](#what-changed-migration-notes). |

The resolutions themselves live in `.egg-state/contracts/1748.json`
(decision-2 resolution block).

## Related Documentation

- [`/babysit-pr` Skill](../../skills/babysit-pr/SKILL.md) — User-facing
  skill flow and argument reference.
- [GitHub Automation Guide](github-automation.md) — Event-driven GitHub
  Actions workflows (review bots, autofixer, conflict resolver, doc
  updater). These run in parallel to `babysit-pr` and address overlapping
  concerns through different mechanisms.
- [SDLC Pipeline Guide](sdlc-pipeline.md) — Full issue-driven pipeline
  that `babysit-pr` shares the implement-phase machinery with.
- [Pipeline Health Monitoring](pipeline-health-monitoring.md) — Two-tier
  health monitoring applied to all pipeline modes including `babysit`.
- [Concurrent Execution Guide](concurrent-execution.md) — Multi-agent
  coordination and the BRC protocol in full.
- [Agent Roles Reference](../reference/agent-roles.md) — File-access
  boundaries for each role (`coder`, `tester`, `documenter`,
  `reviewer_code`, `conflict_resolver`).
