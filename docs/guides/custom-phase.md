# Custom-Phase Guide (`run_agent_task`)

Run a single SDLC phase against a repository with an explicitly chosen
subset of that phase's agent roles, without going through a full
issue-to-PR pipeline. `run_agent_task` is the MCP primitive that replaces
the legacy interactive mode (`bin/egg`) — hosts drive one-off agent work
through the MCP server instead of dropping into a sandboxed interactive
Claude session.

> Issue: [#1762](https://github.com/jwbron/egg/issues/1762).
> See also
> [SDLC Pipeline Guide](sdlc-pipeline.md),
> [Agent Roles Reference](../reference/agent-roles.md).

## What It Does

`run_agent_task` creates a `CUSTOM`-mode pipeline: a pipeline that runs
**one** phase (`refine`, `plan`, or `implement`) against a repository with
a caller-chosen subset of that phase's roles. BRC consensus (Broadcast →
Review → Converge) applies unchanged. Degenerate rosters —
single-producer-no-reviewers — short-circuit to consensus on the first
proposal via `ApprovalMatrix.is_fully_acked()`. Reviewer-only rosters are
rejected at the route boundary.

A few common shapes:

| Use case | Call |
|---|---|
| Research-only refiner pass | `run_agent_task(phase="refine", roles=["refiner"], repo=..., description=...)` |
| Single-coder drive-by change | `run_agent_task(phase="implement", roles=["coder"], repo=..., description=...)` |
| Implement with default roster (coder + tester + documenter + reviewers) | `run_agent_task(phase="implement", repo=..., description=...)` |
| PR improvement against an existing PR | `run_agent_task(phase="implement", pr_number=N, repo=...)` |

`run_agent_task` is **phase-scoped**: a selected role must belong to the
chosen phase's roster. Cross-phase roles (`overseer`, `autofixer`,
`conflict_resolver`, `inspector`) are rejected.

## Why It Exists

Before #1762, the `egg` CLI's default path was an interactive mode —
`bin/egg` → `egg_lib.cli.main` → `run_claude()` — that spawned a
sandboxed Claude Code session after bringing up the gateway and
orchestrator via Docker Compose. That predated the MCP server and the
Kubernetes migration ([#1553](https://github.com/jwbron/egg/issues/1553)).
With k8s as the runtime for gateway+orchestrator, compose was already
half-broken (no `docker-compose.yml` in tree), and hosts already drove
agents through the MCP server directly.

`run_agent_task` collapses that workflow into a single MCP tool:

- **Headless.** All agents run in orchestrator-managed Kubernetes jobs.
  No interactive terminal is ever attached.
- **Phase-scoped.** One phase per invocation. No implicit phase
  advancement. Callers opt into specific phases for specific purposes.
- **Subset-selectable.** A single `refiner` is valid. A single `coder`
  is valid. Any subset in between is valid. The default roster (when
  `roles` is omitted) matches the "short pipeline"
  (`submit_task --start-phase=<phase>`).
- **BRC-native.** The same consensus protocol used by the full SDLC
  pipeline. Degenerate rosters short-circuit; normal rosters run the
  full PROPOSE → ACK/NACK → CONFIRM cycle.

## Usage

### MCP tool call (primary interface)

From any MCP client that can reach the egg orchestrator (Claude Code
configured against `orchestrator.egg-system.svc.cluster.local:9849`, or
a local `kubectl port-forward`):

```
run_agent_task(
  phase = "refine",
  roles = ["refiner"],
  repo = "owner/repo",
  description = "Investigate how concurrent_executor filters the review graph"
)
```

Minimal form — default roster, no branch, no PR, no upstream contract:

```
run_agent_task(
  phase = "implement",
  repo = "owner/repo",
  description = "Fix typo in README.md under ## Quickstart"
)
```

### Input schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `phase` | `"refine" \| "plan" \| "implement"` | yes | — | Which phase to run. Only one phase per call. |
| `repo` | `"owner/name"` | yes | — | Must be in the gateway's `repositories.yaml` allowlist. |
| `description` | `str` | yes | — | Free-form task description. Passed to agents as the prompt. |
| `roles` | `list[str]` | no | Full phase roster | Subset of the phase's roles. Must contain ≥1 producer. Cross-phase roles rejected. |
| `branch` | `str` | no | `egg/custom-<pipeline_id>` | Target branch. Created from `base_branch` if it doesn't exist. |
| `base_branch` | `str` | no | Repo default branch | Base for the target branch. |
| `pr_number` | `int` | no | — | Target an existing PR. Uses per-role staging-branch / head-move guard semantics. |
| `issue_number` | `int` | no | — | Issue context. The CUSTOM pipeline's contract file is still keyed by `pipeline_id` (not `issue-<N>.json`) so concurrent ISSUE-mode pipelines on the same issue don't collide. |
| `analysis` | `str` | no | — | Pre-populated analysis draft. Written to `.egg-state/drafts/` on first run. Producers may overwrite. |
| `plan` | `str` | no | — | Pre-populated plan draft. Same semantics as `analysis`. |
| `qualifier` | `str` | no | — | Suffix (`[a-z0-9]+(-[a-z0-9]+)*`) that disambiguates the `pipeline_id` when multiple CUSTOM runs target the same `issue_number` or `pr_number`. Pipeline id becomes `issue-<N>-<qualifier>` / `pr-<N>-<qualifier>` when set. |
| `config` | `object` | no | `{}` | Forwarded to `PipelineConfig`. Notably `config={"hitl_gates": false}` opts out of human-in-the-loop gates (parity with ISSUE mode — see [HITL Decisions](../hitl-decisions.md)). |

#### Pipeline id generation

The handler derives `pipeline_id` from the caller inputs (see
`_handle_run_agent_task` in `orchestrator/mcp_tools.py`):

| Inputs | `pipeline_id` |
|---|---|
| `issue_number=N`, `qualifier=Q` | `issue-<N>-<Q>` |
| `issue_number=N`, no `qualifier` | `issue-<N>-custom` |
| `pr_number=N`, `qualifier=Q` | `pr-<N>-<Q>` |
| `pr_number=N`, no `qualifier` | `pr-<N>` |
| Neither | `custom-<hex>` (synthetic) |

The branch default `egg/custom-<pipeline_id>` inherits the same id — so
for a PR-targeted call with no qualifier the branch is `egg/custom-pr-<N>`.

### Response

```json
{
  "task_id": "custom-ab12cd34",
  "status": "started",
  "message": "CUSTOM pipeline started: phase=implement, roles=['coder']"
}
```

The `task_id` is the `pipeline_id`. Monitor status via:

- `GET /api/v1/pipelines/<pipeline_id>/status`
- `egg-orch pipeline get <pipeline_id>` (or `egg-orch pipeline status <pipeline_id>` for the status-only view)
- The standard [SDLC Pipeline Guide](sdlc-pipeline.md#monitoring) endpoints.

Retrieve drafts and artifacts from the pipeline branch (`branch`, or the
auto-generated `egg/custom-<pipeline_id>`) with `git show` — same
retrieval pattern as ISSUE-mode pipelines.

## Role Selection Rules

1. **Phase-scoped.** `roles` must be a subset of
   `_PHASE_ROLES[phase] ∪ _PHASE_REVIEWERS[phase]` (after `has_contract`
   and `EGG_ONLY_REVIEWERS` filtering) in
   `shared/egg_contracts/agent_roles.py`. Roles outside the phase's
   roster are rejected with HTTP 400. See
   [Agent Roles Reference](../reference/agent-roles.md) for per-phase
   rosters.
2. **At least one producer.** Reviewer-only rosters (e.g.
   `roles=["reviewer_code"]`) are rejected with HTTP 400. Reviewers
   have nothing to review without a producer in the same roster.
3. **Cross-phase roles rejected.** `overseer`, `autofixer`,
   `conflict_resolver`, and `inspector` are not selectable through
   `run_agent_task` — they exist as cross-cutting utilities spawned by
   the pipeline itself, not as phase participants.
4. **`reviewer_contract` auto-handling.** When an upstream contract
   artifact is present (via `analysis`, `plan`, or an inherited
   `issue_number`), `reviewer_contract` is eligible for inclusion and is
   **automatically added** to the default roster via
   `get_roles_for_phase(phase, include_reviewers=True, repo=repo,
   has_contract=has_contract)`. The route computes `has_contract` from
   the presence of `analysis` / `plan` / an existing
   `.egg-state/contracts/<pipeline_id>.json` (TASK-2-2 in the plan;
   `orchestrator/routes/pipelines.py:957`). When no artifact is
   available, `reviewer_contract` is rejected as an explicit selection
   (`reviewer_contract_without_artifact`) and filtered out of the
   default roster — same filter as `has_contract=False` in
   `get_roles_for_phase()`.
5. **Explicit over implicit.** The resolved roster is persisted on the
   pipeline record as `Pipeline.active_roles` and used by
   `_run_concurrent_phase` to seed `ConcurrentPhaseExecutor` and filter
   the review graph. This keeps the pipeline self-describing across
   role-roster version bumps of in-flight pipelines.

## How the Cycle Works

```
┌─────────────────────────────────────────────────────────────────┐
│                   run_agent_task CYCLE                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  run_agent_task(phase=<P>, roles=<R>, repo, description, ...)   │
│           │                                                     │
│           ▼                                                     │
│  ┌─────────────────────────────────────────┐                    │
│  │ MCP handler (orchestrator/mcp_tools.py) │                    │
│  │   validate description+repo             │                    │
│  │   resolve pipeline_id                   │                    │
│  │   POST /api/v1/pipelines (mode=custom)  │                    │
│  └────────────────┬────────────────────────┘                    │
│                   ▼                                             │
│  ┌─────────────────────────────────────────┐                    │
│  │ Route (orchestrator/routes/pipelines.py)│                    │
│  │   validate roles subset                 │                    │
│  │   reject empty / reviewer-only / cross  │                    │
│  │   resolve branch (auto-gen if absent)   │                    │
│  │   persist Pipeline.active_roles         │                    │
│  │   spawn phase via _run_concurrent_phase │                    │
│  └────────────────┬────────────────────────┘                    │
│                   ▼                                             │
│  ┌─────────────────────────────────────────┐                    │
│  │ ConcurrentPhaseExecutor                 │                    │
│  │   reads pipeline.active_roles           │                    │
│  │   spawns only selected roles            │                    │
│  │   filters review graph to edges         │                    │
│  │     inside the roster                   │                    │
│  └────────────────┬────────────────────────┘                    │
│                   ▼                                             │
│  ┌─────────────────────────────────────────┐                    │
│  │ BRC protocol                            │                    │
│  │   PROPOSE → ACK/NACK → CONFIRM          │                    │
│  │   ApprovalMatrix.is_fully_acked()       │                    │
│  │     short-circuits empty reviewer list  │                    │
│  └────────────────┬────────────────────────┘                    │
│                   ▼                                             │
│               CONSENSUS_REACHED                                 │
│        (pipeline terminates, CUSTOM is single-phase)            │
└─────────────────────────────────────────────────────────────────┘
```

Key properties:

- **Single-phase termination.** After consensus on the sole phase, the
  pipeline transitions to `COMPLETE`. There is no automatic phase
  advancement — a `run_agent_task(phase="refine", ...)` does not fall
  through into `plan`.
- **Degenerate short-circuit is free.**
  `ApprovalMatrix.is_fully_acked()` iterates
  `critical_reviewers_for(producer)`. An empty list returns `True` on
  the first proposal, which fires `CONSENSUS_REACHED` immediately. No
  special casing in the route or executor.
- **HITL gates match ISSUE mode.** `PipelineConfig.hitl_gates` defaults
  to `true`. Any HITL decisions raised by agents during the phase flow
  through the existing `provide_input` tool. Pass
  `config={"hitl_gates": false}` to opt out.

## Common Patterns

### Research-only refiner

Get a requirements analysis written to a draft without spawning a full
plan or implement phase. Useful for "look at X and tell me how hard it
would be."

```
run_agent_task(
  phase = "refine",
  roles = ["refiner"],
  repo = "owner/repo",
  description = "Evaluate cost of migrating integration tests off compose"
)
```

Single-producer-no-reviewers roster → reaches `CONSENSUS_REACHED` on the
first propose. Retrieve the draft with:

```bash
git show egg/custom-<pipeline_id>:.egg-state/drafts/<pipeline_id>-analysis.md
```

### Single-coder drive-by change

When the change is small enough that a full implement phase would be
overkill (no tester needed, no documenter needed):

```
run_agent_task(
  phase = "implement",
  roles = ["coder"],
  repo = "owner/repo",
  description = "Fix log-level typo in orchestrator/routes/pipelines.py line 42"
)
```

Still gets you a consensus-blessed commit on
`egg/custom-<pipeline_id>`, just without tester/documenter/reviewer
participation.

### Coder + reviewer only

Minimal quality gate — one producer, one reviewer, no tester or
documenter churn:

```
run_agent_task(
  phase = "implement",
  roles = ["coder", "reviewer_code"],
  repo = "owner/repo",
  description = "Refactor _handle_submit_task to share a common validator helper with _handle_run_agent_task"
)
```

### PR-targeted custom phase

With `pr_number`, `run_agent_task` runs PR pre-flight checks (PR open,
same-repo non-fork, non-empty diff) and uses per-role staging branches.

```
run_agent_task(
  phase = "implement",
  pr_number = 1234,
  repo = "owner/repo",
  description = "Improve test coverage on the PR's new validator helper"
)
```

### Pre-populated analysis / plan

When you already have an analysis or plan document and want to feed it
into the phase (skipping the synthesis the producer would otherwise
do):

```
run_agent_task(
  phase = "plan",
  repo = "owner/repo",
  description = "Plan the refactor of _run_concurrent_phase",
  analysis = "<markdown body of the analysis>"
)
```

The `analysis` string is written to
`.egg-state/drafts/<pipeline_id>-analysis.md` on first run. Producers
may overwrite it during the phase — same semantics as
`submit_task --start-phase=plan` today.

## Error Responses

Validation errors returned by `validate_roles_for_custom_phase`
(`shared/egg_contracts/agent_roles.py`) are surfaced by the route via
the shape `{"details": {"reason": "<reason>"}}` (see TASK-2-1). The
`<reason>` strings below match exactly what the helper returns, so
programmatic callers can switch on them.

| Scenario | HTTP | `details.reason` |
|---|---|---|
| Reviewer-only roster (no producer selected; deadlocks BRC) | 400 | `reviewer_only_roster` |
| Cross-phase role (`overseer`, `autofixer`, `conflict_resolver`, `inspector`) | 400 | `cross_phase_role` |
| Unknown role string, role outside phase roster, or egg-only reviewer on non-egg repo | 400 | `invalid_roles` |
| `reviewer_contract` requested without an upstream contract artifact | 400 | `reviewer_contract_without_artifact` |
| `phase` not one of `refine` / `plan` / `implement` | 400 | `invalid_phase` |
| `pr_number` on merged / closed / fork / empty PR | 400 / 409 | Structured `{"details": {"reason": "pr_merged" / "pr_closed" / "pr_from_fork" / "pr_empty_diff"}}` |
| Repo not in allowlist (gateway `repositories.yaml`) | 400 | (gateway-surfaced; shape matches `submit_task`) |
| Existing pipeline with same id | 409 | (route-surfaced; shape matches `submit_task`) |

All four route-level role-validation reasons (`reviewer_only_roster`,
`cross_phase_role`, `invalid_roles`, `reviewer_contract_without_artifact`)
are the exact strings compiled into
`validate_roles_for_custom_phase` at `shared/egg_contracts/agent_roles.py`
(lines ~1135–1188 in
[`b18c645b1`](https://github.com/jwbron/egg/commit/b18c645b1)).

## Artifact Retrieval

Every CUSTOM pipeline has a branch (`branch`, or
`egg/custom-<pipeline_id>` auto-generated). Producers commit drafts,
contract artifacts, and reviews there during the phase. Retrieve any
artifact with `git show`:

```bash
# Pipeline id returned by run_agent_task — use the value from your
# response, not this placeholder.
PID=custom-ab12cd34

# Analysis draft
git show "egg/custom-${PID}:.egg-state/drafts/${PID}-analysis.md"

# Plan draft (if phase was plan)
git show "egg/custom-${PID}:.egg-state/drafts/${PID}-plan.md"

# Agent outputs
git show "egg/custom-${PID}:.egg-state/agent-outputs/${PID}-refiner-output.json"

# BRC history
git show "egg/custom-${PID}:.egg-state/brc-history/${PID}-<phase>.md"
```

There is no "no-branch, no-writes" path (decision-7). Every CUSTOM
pipeline has a branch; callers always have a durable place to `git
show` from.

## Limitations

- **Single-phase only.** `run_agent_task` does not cascade into later
  phases. Submit a new call for the next phase, or use `submit_task`
  (ISSUE mode) for full refine → plan → implement.
- **MCP-only.** There is no `bin/egg-sdlc custom-phase` subcommand;
  hosts drive `run_agent_task` through the MCP server. (Scripted
  callers can POST directly to the orchestrator's
  `/api/v1/pipelines` route with `mode=custom`.)
- **No rollback to interactive mode.** The `bin/egg` binary, `egg
  --setup`/`--reset`/`--compose`/`--public`/`--private` flags, and the
  Docker Compose deployment path were removed alongside this tool.
  Hosts without an MCP client should use the [GitHub
  Action](../../action/README.md) or `bin/egg-sdlc submit-task` instead.

## Relationship to Other Pipelines

| Pipeline mode | Tool | What triggers it | Phases run |
|---|---|---|---|
| `ISSUE` | `submit_task` | GitHub issue body | refine → plan → implement (full) |
| `ISSUE` (short) | `submit_task --start-phase=<P>` | Issue + explicit start phase | `<P>` and later |
| `CUSTOM` | `run_agent_task` | MCP call with explicit phase + roles | One phase, user-chosen roster |
| `CUSTOM` + `pr_number` | `run_agent_task(pr_number=N, ...)` | Open non-fork non-empty PR | Chosen phase, PR diff as input |

## Related Documentation

- [Agent Roles Reference](../reference/agent-roles.md) — per-phase
  rosters, producer vs. reviewer classifications, file access rules.
- [SDLC Pipeline Guide](sdlc-pipeline.md) — the full three-phase
  pipeline that `submit_task` drives; `run_agent_task` targets one
  phase of the same machinery.
- [Concurrent Execution](concurrent-execution.md) — BRC consensus,
  message bus, directed coordination. `run_agent_task` participates
  unchanged.
- [HITL Decisions](../hitl-decisions.md) — how human-in-the-loop gates
  interact with pipelines; parity between CUSTOM and ISSUE modes.
- [MCP Deployment Tools](../reference/mcp-deployment-tools.md) — sibling
  MCP tools for cluster introspection and rollout.
- Issue [#1762](https://github.com/jwbron/egg/issues/1762) — the
  motivating issue, with the full list of decisions resolved at the
  refine HITL gate.
