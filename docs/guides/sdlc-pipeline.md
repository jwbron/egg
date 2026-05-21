# SDLC Pipeline Architecture

> Structurally enforced agent checkpoints and verification gates for autonomous software development.

This document describes the SDLC (Software Development Lifecycle) pipeline that enables autonomous agents to work on issues while maintaining quality through structural enforcement—not just prompts.

For the conceptual foundation of this pipeline—the feedback loop model that drives quality—see [The Agentic Feedback Loop](../architecture/agentic-feedback-loop.md).

For the architecture document with threat model and security properties, see [SDLC Pipeline Architecture](../architecture/sdlc-pipeline.md).

## Guiding Principles

### 1. Structural Enforcement Over Prompt Compliance

Agents cannot be trusted to self-police via prompts alone. The pipeline enforces constraints at multiple infrastructure layers:

- **Filesystem-level readonly mounts**: Phase-protected directories (e.g., `.egg-state/contracts/`, `.egg-state/drafts/`, `.egg-state/pipelines/` during implement) are mounted readonly, preventing modification at the OS level. The `.egg-state/reviews/` directory is readonly for most agents but writable for reviewer agents who need to write verdict files. `.egg-readonly` marker files explain the restriction to agents.
- **Branch lock**: Pipeline agents are locked to their assigned worktree branch—branch switching is blocked by the gateway
- **Commit-time validation**: The gateway validates staged files against phase restrictions before allowing `git commit`
- **Push-time operation filtering**: The gateway blocks operations not permitted in the current phase
- **Push-target enforcement**: Pipeline agents must push to their assigned branch only—the gateway rejects pushes to any other branch (HTTP 403), preventing agents from improvising branch names on push failure. Killswitch: `PUSH_TARGET_ENFORCEMENT=false`
- **Agent-role file restrictions**: Each agent role (coder, tester, documenter, etc.) has allowed and blocked file patterns enforced at push time. Enforced by default; set `EGG_AGENT_RESTRICTIONS_ENFORCE=false` for warn-only mode with audit logging
- **Role-based field ownership**: Contract mutations are validated against caller role
- **Completion signal branch verification**: When an agent signals completion with a commit SHA, the orchestrator verifies the commit exists on the pipeline's expected branch (HTTP 409 on mismatch)
- **Per-command timeout**: Shell commands in the sandbox are wrapped with a configurable timeout (default 300s) to prevent runaway commands like `grep -rn / ` from hanging the container. Configurable via `BASH_COMMAND_TIMEOUT`
- **Per-agent worktree isolation**: Each agent runs in its own git worktree, preventing agents from overwriting each other's uncommitted work
- **Per-agent git identity**: Agents commit as `egg (<role>) <<role>@egg.local>` for auditability
- **SDK tool interception**: `Write`, `Edit`, and `NotebookEdit` are checked against role boundaries before execution (Agent SDK only)
- **HITL recovery for uncommitted work**: When agents exit with uncommitted changes, a HITL decision is created for recovery or discard (replaces auto-commit-and-push)
- **Separate context windows**: Each agent invocation runs in a separate GitHub Actions job with fresh context

### 2. Contract-as-Code

All pipeline state is stored in JSON contracts at `.egg-state/contracts/{identifier}.json` and committed to the feature branch (not main), where `{identifier}` is the issue number for issue-driven pipelines or the pipeline ID for prompt-driven pipelines. This provides:

- Auditable history of all state changes
- Recovery from failures without losing progress
- Clear handoff between phases and agents

### 3. Role-Based Access Control

The pipeline enforces role-based field ownership in contracts:

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| **Implementer** | `commit`, `notes`, `files_affected` | `status`, `verified`, `review_feedback` |
| **Reviewer** | `status`, `review_feedback`, `current_phase` | `commit`, task definitions |
| **Human** | All fields | — |

Code reviews are performed by the existing PR review workflow (`reusable-review.yml`), which provides line-level feedback on draft PRs created during the implement phase.

### 4. Human-in-the-Loop at Critical Points

The pipeline pauses for human approval at phase transitions (refine and plan). The orchestrator's decision queue handles approval and supports requesting changes with a circuit breaker (`max_review_cycles`, default 3) to prevent unbounded revision loops.

## Pipeline Architecture

> **Note**: The architecture below describes the standard **issue mode** pipeline. For the **babysit mode** — a one-off implement-phase BRC cycle against an existing PR — see the [Babysit-PR Guide](babysit-pr.md). Babysit mode reuses the implement-phase machinery below (producers, reviewers, BRC consensus) but drops refine/plan and operates on the PR diff instead of a contract.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           SDLC PIPELINE                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐  │
│  │   REFINE    │───▶│    PLAN     │───▶│  IMPLEMENT  │───▶│ CREATE   │  │
│  │  (cycles)   │    │  (cycles)   │    │  (cycles)   │    │   PR     │  │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘  │
│        │ ╎                │                  │                  │       │
│        ▼ ╎                ▼                  ▼                  ▼       │
│   ┌─────────┐ ╎      ┌─────────┐        ┌─────────┐        ┌─────────┐  │
│   │ REVIEW  │ ╎      │ REVIEW  │        │ REVIEW  │        │  HUMAN  │  │
│   │ (auto)  │ ╎      │ (auto)  │        │ (auto)  │        │  MERGE  │  │
│   └────┬────┘ ╎      └────┬────┘        └─────────┘        └─────────┘  │
│        │      ╎           │                                             │
│        ▼      ╎           ▼                                             │
│   ┌─────────┐ ╎      ┌─────────┐                                        │
│   │ HITL    │ ╎      │ HITL    │                                        │
│   │ Approve │ ╎      │ Approve │                                        │
│   └─────────┘ ╎      └─────────┘                                        │
│               ╎                                                         │
│                                                                        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Pipeline Status Visualization

The orchestrator provides pipeline status visualization through the following endpoints:

**1. Static Visualization**: `GET /api/v1/pipelines/<pipeline_id>/visualization`

Returns a snapshot of the current pipeline state.

**Query parameters**:
- `format`: Output format - `full` (default), `compact`, `text`, or `json`
- `ascii`: Use ASCII-only characters (`true` or `false`, default: `false`)

**Response formats**:

1. **`full` (default)**: Returns JSON with full DAG visualization, compact status, and progress bar
2. **`compact`**: Single-line phase status with symbols
3. **`text`**: Plain text DAG visualization
4. **`json`**: Structured JSON report with phase details

**2. Status Polling**: `GET /api/v1/pipelines/<pipeline_id>/status`

Returns the current pipeline status for polling-based monitoring.

**Response**:
```json
{
  "success": true,
  "message": "Status retrieved",
  "data": {
    "id": "issue-123",
    "status": "running",
    "current_phase": "implement",
    "pending_decisions": 0,
    "updated_at": "2026-02-12T10:30:00Z"
  }
}
```

Once a pull request is created during the PR phase, two additional fields appear in `data`:

- `pr_url` — full GitHub URL of the created PR (e.g. `"https://github.com/owner/repo/pull/42"`)
- `pr_number` — integer PR number parsed from the URL (e.g. `42`); omitted if the URL has an unexpected shape

This avoids a separate `gh pr list` call by monitoring clients.

> **Pipeline record fields (issue #1911).** The auto-PR path also writes `pipeline.pr_number` and (best-effort) `pipeline.pr_head_sha` onto the pipeline record itself, not only the `pr_url` phase artifact. Consumers that load the pipeline via `get_pipeline_snapshot` / the pipeline JSON can rely on `pipeline.pr_number` directly — the overseer's `post-consensus-push-stall` detector uses this as one of the three signals that the post-consensus transition succeeded. `pipeline.pr_head_sha` is populated when `gh pr view` returns a valid hex SHA; if the `gh` call fails or propagation is still in flight, the field is left `None` and the PR phase still succeeds.

When `pending_decisions > 0`, the `data` object includes an additional `pending_decision` field with the first pending decision's details, so consumers don't need a second round-trip to fetch it:

```json
{
  "success": true,
  "message": "Status retrieved",
  "data": {
    "id": "issue-123",
    "status": "running",
    "current_phase": "implement",
    "pending_decisions": 1,
    "updated_at": "2026-02-12T10:30:00Z",
    "pending_decision": {
      "id": "decision-1",
      "question": "How should we proceed?",
      "context": "Additional context for the decision",
      "options": ["Option A", "Option B"],
      "created_at": "2026-02-12T11:00:00Z"
    }
  }
}
```

**CLI tools**:
- `egg-sdlc [<issue_number>]` — Interactive SDLC pipeline CLI with DAG visualization and HITL checkpoints
- `egg-pipeline-watch <pipeline_id>` — Monitor a single pipeline via SSE stream (real-time DAG visualization)
- `egg-status` — Monitor all active pipelines via unified SSE stream (real-time updates)

**3. Real-time Streaming**: `GET /api/v1/pipelines/<pipeline_id>/stream`

Returns a Server-Sent Events (SSE) stream for real-time pipeline updates.

**Query parameters**:
- `ascii`: Use ASCII-only characters (`true` or `false`, default: `false`)

**Event types**:
- `snapshot`: Initial pipeline state with full DAG visualization
- `pipeline.*`: Pipeline lifecycle events (created, started, completed, failed, cancelled)
- `phase.*`: Phase transition events (started, completed, failed)
- `agent.*`: Agent lifecycle events (started, completed, failed, timeout)
- `decision.*`: HITL decision events (created, resolved)
- `container.*`: Container lifecycle events (spawned, stopped, removed) — *planned; not yet emitted via SSE*
- `done`: Stream ending (pipeline terminal state or timeout)
- `error`: Error occurred

Each event includes the current visualization data and pipeline status. The stream automatically closes when the pipeline reaches a terminal state or after 1 hour.

**4. Unified Streaming (All Pipelines)**: `GET /api/v1/pipelines/stream`

Returns a Server-Sent Events (SSE) stream for real-time updates across all active pipelines. Unlike the per-pipeline stream, terminal events for individual pipelines do not end the stream.

**Query parameters**:
- `ascii`: Use ASCII-only characters (`true` or `false`, default: `false`)
- `active_only`: Only include active pipelines in snapshot (`true` or `false`, default: `true`)
- `full_dag`: Include full DAG visualization instead of compact status (`true` or `false`, default: `false`)

**Event types**:
- `snapshot`: Initial state of all active pipelines
- `pipeline.*`, `phase.*`, `agent.*`, `decision.*`: Events for individual pipelines
- `done`: Stream is ending (timeout after 1 hour)

**CLI tool**: Use `egg-status` to monitor all pipelines in a live dashboard. Runs on the host and connects to the orchestrator's unified stream endpoint.

**Example JSON response** (`format=full`):
```json
{
  "success": true,
  "message": "Visualization generated",
  "data": {
    "pipeline_id": "issue-123",
    "status": "running",
    "current_phase": "implement",
    "visualization": {
      "dag": ">>> ╔══════════════════════╗\n    │ ▶ Implement          │\n    │   running (2 cycles completed)            │\n    │   ✓ coder  ▶ reviewer│\n    │   [last cycle: 5m0s | total: 15m0s]│\n    ╚══════════════════════╝",
      "compact": "✓Refine → ✓Plan → [▶Implement] → ○PR",
      "progress": "[███████████░░░░░░░░░] 60%"
    },
    "phases": {
      "refine": {"status": "complete", "review_cycles": 2, "containers": 1, "agents": [{"role": "coder", "status": "complete"}]},
      "plan": {"status": "complete", "review_cycles": 1, "containers": 1, "agents": [{"role": "coder", "status": "complete"}]},
      "implement": {"status": "running", "review_cycles": 2, "containers": 2, "agents": [{"role": "coder", "status": "complete"}, {"role": "reviewer", "status": "running"}]},
      "pr": {"status": "pending", "review_cycles": 0, "containers": 0, "agents": []}
    },
    "pending_decisions": 0,
    "updated_at": "2026-02-12T10:30:00Z"
  }
}
```

**Status symbols**:
- `○` - Pending (not started)
- `▶` - Running
- `⏸` - Awaiting human decision
- `✓` - Complete
- `✗` - Failed
- `⊘` - Cancelled

**Timing display**:

The DAG visualization tracks per-cycle and total phase timing:
- **Single-cycle phases**: Display simple duration `[5m0s]`
- **Multi-cycle phases**: Display both last cycle and total work time `[last cycle: 5m0s | total: 15m0s]`
- **Phase detail view**: Shows per-cycle timing breakdown with cycle status (done/running)

Timing starts when actual work begins (`work_started_at`), excluding setup and HITL waiting time.

**Use cases**:
- Monitor pipeline progress from external tools
- Display real-time status in CI dashboards
- Poll for phase completion
- Debug stuck pipelines
- Track cycle performance and identify bottlenecks

### Phases

| Phase | Purpose | Allowed Operations | Exit Requires |
|-------|---------|-------------------|---------------|
| **Refine** | Analyze issue, produce analysis document | `git push`, `egg-contract add-decision` | Auto-review pass + Human approval |
| **Plan** | Create implementation plan with tasks | `git push`, `egg-contract add-decision` | Auto-review pass + Human approval |
| **Implement** | Execute tasks on draft PR with CI and review feedback | `git push`, `egg-contract complete-task/complete-phase` | All checks pass (CI + PR review) |
| **PR** | Finalize PR for human review and merge | `gh pr edit`, `git push` | Human merge (closes issue automatically) |

### Multi-Reviewer Architecture

The orchestrator runs multiple specialized reviewers in parallel, with phase-specific defaults:

| Phase | Reviewers | Focus |
|-------|-----------|-------|
| **Refine** | Refine, Agent-Design | Analysis quality, agent-mode alignment |
| **Plan** | Plan | Plan quality, task breakdown, alignment with analysis |
| **Implement** | Contract, Code | Contract fulfillment, security, correctness |

**Specialized Reviewers:**

| Reviewer | Focus |
|----------|-------|
| **Refine** | Analysis quality, research depth, options evaluation |
| **Plan** | Task breakdown, dependencies, test strategy, alignment with analysis |
| **Agent-Design** | Agent-mode design alignment (anti-patterns) |
| **Contract** | Task completion, acceptance criteria |
| **Code** | Security, correctness, robustness, testing, documentation |

**Verdict Aggregation:**
- All reviewers run in parallel
- Any `needs_revision` from any reviewer → aggregate `needs_revision`
- Blocking feedback (from `needs_revision` verdicts) is combined with per-reviewer section headers and passed to the next revision cycle
- Analysis and suggestions from **all** verdicts (including `approved`) are collected as advisory content and logged for observability
- Failed reviewers are tracked separately and trigger escalation

**Per-Reviewer State Tracking:**

The contract tracks per-reviewer verdicts for debugging:
```json
{
  "implement_reviewer_verdicts": {
    "contract": "needs_revision",
    "code": "approved"
  }
}
```

### Multi-Agent Orchestration

The implement phase runs as a **DAG of independent slices** (#2137). Each slice has its own integration branch (`egg/issue-N/slice-M`), agent team, BRC consensus, and stacked PR targeting the parent slice's branch (or the pipeline branch for root slices). The `SliceScheduler` computes execution waves — slices whose dependencies are satisfied run concurrently (capped at `EGG_ORCH_MAX_PARALLEL_SLICES`, default 2 per pipeline; a process-wide `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` cap, default 4, applies across all pipelines running in the same orchestrator process — #2241); dependent slices wait in subsequent waves. See [Slice-DAG Implement Phase](../architecture/slice-dag.md) for the full model including forest validation, two-tier `max_cycles` accounting, failure cascade, and the stacked-PR reconciler.

Within each slice, concurrent BRC execution runs: specialized agents run simultaneously and coordinate via the message bus.

**Agent Roles:**

| Role | Responsibilities | Can Write |
|------|-----------------|-----------|
| **Coder** | Implement code changes based on plan tasks | Source code files (`**/*.py`, `**/*.ts`, etc.) |
| **Tester** | Find gaps in implementation, write tests, run linters and report issues | Test files (`tests/`, `**/*_test.py`, `**/*.test.ts`, etc.) and pytest infrastructure (`**/conftest.py`) |
| **Documenter** | Update documentation for the changes | Documentation files (`docs/`, `**/*.md`) |
| **Reviewer (Code)** | Review code for security, correctness, robustness | Review verdicts only |
| **Reviewer (Contract)** | Verify task completion and acceptance criteria | Review verdicts only |

**Role-Aware Task Assignment:**
Tasks in the plan's YAML appendix can include an optional `role` field (`coder`, `tester`, or `documenter`) that assigns the task to a specific execution agent based on the files it modifies. During the implement phase, each agent only sees tasks assigned to its role — the coder also picks up any unassigned tasks as a fallback. Role filtering only activates when at least one task has an explicit role, preserving backward compatibility with legacy plans. See [Agent Roles Reference](../reference/agent-roles.md#role-aware-task-assignment) for the file-to-role mapping and examples.

**File Access Enforcement:**
The gateway enforces file access patterns for each agent role via `gateway/agent_restrictions.py`. For example, the Coder agent cannot modify documentation files, and the Tester agent cannot modify source code. This prevents agents from overstepping their responsibilities.

**Handoff Data:**
Agents communicate via handoff data stored in `.egg-state/agent-outputs/{identifier}-{role}-output.json` (where `{identifier}` is the issue number or pipeline ID). For example, the Coder agent outputs a list of changed files, which the Tester and Documenter agents read to focus their work. The identifier prefix prevents merge conflicts when concurrent pipelines merge to main.

**BRC Content Enforcement:**
All BRC consensus messages (proposals, ACKs, NACKs, withdrawals) must carry substantive content — the orchestrator enforces a minimum content floor (≥50 characters, no boilerplate) at the protocol boundary. This ensures ACKs carry the same deliberative weight as NACKs and prevents rubber-stamping.

**Orchestration:**
Multi-agent orchestration is managed by the local orchestrator (`orchestrator/container_spawner.py`). The orchestrator reads the contract state, determines which agents can run based on dependencies, and dispatches them in parallel where possible.

### Refine and Plan Phase Review Cycles

The refine and plan phases include an automated internal review step before human approval. All reviews happen internally without posting to the issue until approval:

1. **Producer agent runs** — The refine/plan agent writes its output to `.egg-state/drafts/{identifier}-{analysis|plan}.md`
2. **Reviewer agents run in parallel** — Each reviewer reads the draft and writes verdict to its own file
3. **Verdicts aggregated** — If any reviewer needs revision, the aggregate verdict is `needs_revision`
4. **If approved** — The final draft is posted to the issue with an approval checkbox for human review
5. **If needs revision** — Producer agent is re-dispatched with combined feedback; cycle repeats without posting to issue

**Key Benefit:** Internal review cycles don't create noise on the GitHub issue. Only the final approved analysis/plan is posted for human review.

**File Structure:**
```
.egg-state/
├── contracts/{identifier}.json      # Contract state
├── drafts/
│   ├── {identifier}-analysis.md     # Refine phase draft (preserved on PR branch)
│   └── {identifier}-plan.md         # Plan phase draft (preserved on PR branch)
├── brc-history/
│   ├── {identifier}-refine.md       # BRC consensus messages from refine phase (human-readable, with YAML metadata)
│   ├── {identifier}-refine.json     # BRC consensus messages from refine phase (machine-readable)
│   ├── {identifier}-plan.md         # BRC consensus messages from plan phase (human-readable, with YAML metadata)
│   ├── {identifier}-plan.json       # BRC consensus messages from plan phase (machine-readable)
│   ├── {identifier}-implement.md    # BRC consensus messages from implement phase (human-readable, with YAML metadata)
│   └── {identifier}-implement.json  # BRC consensus messages from implement phase (machine-readable)
└── reviews/
    ├── {identifier}-refine-refine-review.json        # Refine review verdict
    ├── {identifier}-refine-agent-design-review.json   # Agent-design review verdict
    ├── {identifier}-plan-plan-review.json             # Plan review verdict
    ├── {identifier}-implement-code-review.json        # Code review verdict
    └── {identifier}-implement-contract-review.json    # Contract review verdict
```

**Note:** Draft files are preserved on the PR branch as artifacts of the pipeline's reasoning. The analysis and plan documents produced before implementation are kept alongside the code so reviewers can see the intended scope, and so later debugging can compare the planned approach against what shipped (see #1713).

**Review Verdict JSON Schema:**
```json
{
  "reviewer": "refine" | "plan" | "agent-design" | "contract" | "code",
  "verdict": "approved" | "needs_revision",
  "summary": "Brief summary of findings (1-2 sentences)",
  "analysis": "Detailed analysis of the reviewed work (always populated)",
  "suggestions": "Non-blocking suggestions for improvement (even when approving)",
  "feedback": "Blocking issues requiring revision (empty when approving)",
  "timestamp": "ISO 8601 timestamp"
}
```

**Field guidelines:**
- **analysis**: Always populated regardless of verdict. Describes what was reviewed, what was found, and reasoning. Code reviewers provide file-by-file analysis; contract reviewers provide criterion-by-criterion verification; plan/refine reviewers provide section-by-section evaluation.
- **suggestions**: Non-blocking observations and improvement ideas, included even when approving. These are surfaced as advisory content for observability.
- **feedback**: Reserved for blocking issues only — problems that must be fixed before approval. Empty when the verdict is `approved`.

**Review Conventions:**

All SDLC reviewers follow quality standards aligned with the PR reviewer workflow:

1. **Be comprehensive** — Review the entire scope, not just the obvious parts
2. **Be specific** — Reference exact file paths, line numbers, function names, and code snippets
3. **Be direct** — State issues plainly without hedging or softening language
4. **Suggest fixes** — When identifying a problem, include a concrete suggestion for resolution
5. **Provide context** — Explain *why* something is an issue (impact, risk, or principle violated)

Code reviewers additionally receive "last line of defense" framing and must provide file-by-file analysis of all changed files. Draft-based reviewers (refine, plan) follow expanded procedural steps that require cross-referencing each section against criteria and citing specific evidence.

**Review Criteria for Refine:**
- Does the analysis address the issue description?
- Are options meaningfully different and well-reasoned?
- Are constraints and dependencies identified?
- Are open questions specific enough for a human to answer?
- Are questions actionable?
- Are ALL uncertainties and assumptions surfaced? The analysis should not proceed with unvalidated assumptions when it could ask the human instead.
- Is the recommended approach justified?

**Review Criteria for Plan:**
- Does the plan align with the approved analysis?
- Are tasks broken down with clear acceptance criteria?
- Are dependencies between tasks identified?
- Is the test strategy adequate?
- Is the YAML appendix correct for task extraction?

### Phase-Based Operation Filtering

Each phase has a defined set of permitted operations. The gateway blocks all other operations via session-based phase tracking:

**How it works:**
1. The SDLC pipeline sets `EGG_PIPELINE_PHASE` environment variable when starting agent containers
2. The runtime passes this phase to the gateway during session creation
3. The gateway stores the phase in the session state
4. When operations like `gh pr create` are invoked, the gateway checks the session's phase
5. If the operation is not allowed for that phase (per `.egg/phase-permissions.json`), the gateway returns HTTP 403

**Phase restrictions:**
- **Refine/Plan phases**: `git push` restricted to `.egg-state/` files; cannot `gh pr create`—prevents source code changes before plan approval
- **Implement phase**: Can `git push` to the branch; draft PR is created automatically by the pipeline (not by agent)
- **PR phase**: PR is auto-created by the orchestrator from contract metadata and git log (no agent spawned). Human must merge.

This structural enforcement prevents incidents where agents push code during planning or manually create PRs before implementation is complete.

## Contract System

### Directory Structure

| Directory | Purpose | Committed To |
|-----------|---------|--------------|
| `.egg/schemas/` | Contract JSON schema definitions | `main` |
| `.egg/phase-permissions.json` | Phase operation restrictions | `main` |
| `.egg-state/contracts/` | Per-issue contract instances | Feature branches only |
| `.egg-state/drafts/` | Draft analysis and plan documents (preserved on PR branch for review) | Feature branches only |
| `.egg-state/brc-history/` | Per-phase BRC consensus message logs — `.md` (human-readable with YAML metadata) and `.json` (machine-readable) per phase (re-written in PR phase as safety net) | Feature branches only |
| `.egg-state/reviews/` | Internal review verdicts (JSON) | Feature branches only |

### Conflict-Resistant Contract Updates

The local orchestrator handles concurrent contract updates through `orchestrator/state_store.py`, which uses git-backed state management. When multiple agents modify the same contract file simultaneously, the state store handles conflict resolution automatically through its commit-based approach.

### Contract Schema

```json
{
  "schemaVersion": "1.1",
  "issue": {
    "number": 123,
    "title": "Add feature X",
    "url": "https://github.com/org/repo/issues/123"
  },
  "current_phase": "implement",
  "phases": [
    {
      "id": "phase-1",
      "name": "Core Implementation",
      "status": "in_progress",
      "dependencies": [],
      "tasks": [
        {
          "id": "task-1-1",
          "description": "Create schema",
          "status": "complete",
          "commit": "abc1234",
          "acceptance_criteria": "Schema validates test contracts"
        }
      ],
      "review_feedback": []
    },
    {
      "id": "phase-2",
      "name": "Integration",
      "status": "pending",
      "dependencies": ["phase-1"],
      "tasks": [],
      "review_feedback": []
    }
  ],
  "decisions": [],
  "workflow_owner": "my-org",
  "audit_log": []
}
```

> **Schema 1.1 (#2548)**: The default `schemaVersion` is now `"1.1"`, which
> additively introduces four optional `pr.context_*` fields
> (`context_title`, `context_description`, `context_branch`,
> `context_pr_number`). Pre-1.1 contract JSON loads cleanly — a Pydantic
> `model_validator` silently promotes `"1.0"` to `"1.1"` on load and the
> bumped value is persisted on the next save.

### Role-Based Field Ownership

The `shared/egg_contracts/roles.py` module defines field ownership:

```python
FIELD_OWNERSHIP = {
    # Implementer owns commit and notes
    "phases.*.tasks.*.commit": Role.IMPLEMENTER,
    "phases.*.tasks.*.notes": Role.IMPLEMENTER,

    # Reviewer owns status fields
    "phases.*.tasks.*.status": Role.REVIEWER,
    "phases.*.status": Role.REVIEWER,
    "phases.*.review_feedback.*": Role.REVIEWER,

    # Human owns decisions
    "decisions.*.resolved": Role.HUMAN,
    "decisions.*.resolution": Role.HUMAN,
}
```

The validator rejects unauthorized mutations with clear error messages:

```
Error: Cannot modify field 'phases.*.tasks.*.status'.
Role 'implementer' is not authorized to modify this field.
This field can only be modified by role 'reviewer'.
```

## Implementation Workflow

### Keeping Branches Up-to-Date

The SDLC pipeline automatically merges the latest main branch into the issue branch before starting work in each phase. This prevents agents from working on stale code that conflicts with recent changes.

**Merge Process:**

1. **Check if merge needed** — The pipeline uses `git merge-base --is-ancestor` to check if main has commits not in the issue branch
2. **Perform merge** — If needed, merges `origin/main` into the issue branch with `--no-edit`
3. **Push merge commit** — Pushes the merge commit so reviewers and subsequent steps see an up-to-date branch
4. **Automatic conflict resolution** — If merge conflicts occur:
   - Aborts the conflicted merge
   - Looks up the PR number for the branch
   - Triggers the `on-merge-conflict.yml` workflow
   - Waits for conflict resolution to complete
   - Pulls the resolved changes and continues
   - Fails if no PR exists (required for `workflow_dispatch` targeting) or conflict resolution fails

This ensures agents always work with the latest codebase and conflicts are resolved before work begins, not at PR finalization time.

### Implement and PR-Based Review

The implement phase uses PR-based automated code review:

1. **Main branch merge** — Before starting work, merges latest main into the issue branch (see above)
2. **Implementer executes tasks** — The implementer agent runs, commits changes, and pushes to the branch
3. **Draft PR created** — After implementation succeeds, a draft PR is created automatically with commit messages in the description
4. **CI and review checks** — The pipeline waits for all GitHub check runs (linting, tests, and PR review) to complete
5. **Review feedback** — The `reusable-review.yml` workflow provides line-level code review comments on the draft PR
6. **Re-implementation cycles** — If checks fail or review requests changes, the implementer is re-invoked with feedback
7. **PR finalization** — Once all checks pass and review approves, the draft PR is marked ready for human merge
8. **Issue closure** — When the PR is merged, the original issue is automatically closed (the PR body includes `Closes #<issue>`)

This approach provides:
- Line-level code review comments visible to humans
- Integration with existing PR review workflows
- Human visibility into every implementation cycle
- CI/test validation before review
- Automatic resolution of merge conflicts before work begins

### Context Window Isolation

Each agent invocation runs in a fresh container with no memory of previous runs. All state transfer happens through:

1. The contract JSON in `.egg-state/contracts/`
2. Git commits on the feature branch
3. GitHub issue/PR comments and reviews
4. Checkpoints from prior agent sessions (via `egg-checkpoint`)

This prevents context pollution and ensures reproducible behavior. When the implementer is re-invoked after review feedback, it receives the PR review comments as part of its prompt context. Agents also receive checkpoint discovery hints in their prompts, enabling them to review prior sessions for richer context than handoff data alone.

### Role-Specific Prompt Context

Agent prompts include role-appropriate context rather than embedding the full issue body for every agent. This reduces noise and focuses each agent on its responsibilities.

**Analysis roles** (architect, task_planner, risk_analyst) receive the full issue body, since they need it for problem understanding and planning.

**Execution roles** (tester, documenter) receive:
- A 1-2 sentence background summary extracted from the issue title and first paragraph
- Checkpoint discovery hints for reviewing prior agent sessions (`egg-checkpoint`)
- Pointers to full context on demand (`gh issue view`, handoff data, git diff)
- Role-filtered tasks: only the tasks assigned to their role via the `task.role` field (unassigned tasks default to coder)

This approach follows a "focus, don't starve" philosophy: agents get enough context to make good decisions without being distracted by irrelevant detail. Full context is always accessible on demand via CLI commands and file paths.

The context is built by `_build_role_context()` in `orchestrator/routes/pipelines.py`, which replaces the previous pattern of embedding `pipeline.prompt` verbatim into every agent prompt. Task filtering by role ensures each agent only works on files within its gateway-enforced boundaries.

## Multi-Agent Orchestration

The refine, plan, and implement phases use concurrent BRC execution to parallelize work across specialized agents. All agents within a phase run simultaneously, communicating via the orchestrator message bus and reaching consensus through the BRC protocol. Additional phases can be enabled for concurrent execution via the `concurrent_phases` config. This reduces context window pollution and improves first-pass implementation quality.

Agents are organized into five categories (execution, analysis, review, utility, interface) with role definitions consolidated in `shared/egg_contracts/agent_roles.py`. See the [Agent Roles Reference](../reference/agent-roles.md) for the complete roster.

### Agent Roles

| Role | Category | Purpose | File Access |
|------|----------|---------|-------------|
| **Coder** | Execution | Implements code changes | All files except docs, tests, `.egg-state/`, `.github/` (blocklist-complement; see [Agent Roles Reference](../reference/agent-roles.md#coder)) |
| **Tester** | Execution | Finds gaps, writes tests, runs linters and reports issues to coder | Test files and infrastructure only: `tests/`, `test/`, `**/test_*.py`, `**/*_test.go`, `**/*.test.{ts,tsx,js,jsx}`, `**/*.spec.{ts,tsx,js,jsx}`, `**/conftest.py` (see [Agent Roles Reference](../reference/agent-roles.md#tester)) |
| **Documenter** | Execution | Updates documentation | Documentation and markdown only: `docs/`, `**/*.md`, `**/README.md` (see [Agent Roles Reference](../reference/agent-roles.md#documenter)) |
| **Autofixer** | Utility | Auto-fixes lint/format/type-check issues | Source and config files (no docs or contracts) |
| **Conflict Resolver** | Utility | Resolves merge and inter-agent conflicts | Source, test, doc, and config files (no `.egg-state/`) |
| **Reviewer (Code)** | Review | Reviews code for security, correctness | Review verdicts only |
| **Reviewer (Contract)** | Review | Verifies task completion | Review verdicts only |

### Agent Handoffs

Each agent produces handoff data stored in `.egg-state/agent-outputs/{identifier}-{role}-output.json` (e.g., `871-coder-output.json` for issue #871):

```json
{
  "changed_files": ["src/api.py", "src/models.py"],
  "test_results": {"passed": 42, "failed": 0},
  "summary": "Implemented user authentication endpoints"
}
```

Subsequent agents receive handoff data from their dependencies via the orchestrator.

### Contract CLI Commands

```bash
# View agent execution status
egg-contract agent-status

# Get next agents to dispatch
egg-contract agent-next

# Mark agent as started (pipeline use)
egg-contract agent-start --role coder

# Mark agent as complete with commit
egg-contract agent-complete --role coder --commit abc1234

# Mark agent as failed
egg-contract agent-fail --role tester --error "Tests failed"
```

### Key Files

| File | Purpose |
|------|---------|
| `orchestrator/container_spawner.py` | Agent container lifecycle |
| `orchestrator/concurrent_executor.py` | BRC concurrent execution |
| `orchestrator/peer_consensus.py` | Peer consensus tracking |
| `shared/egg_contracts/agent_roles.py` | Agent role definitions and file access |
| `shared/egg_contracts/orchestration.py` | Orchestration state management |
| `shared/egg_contracts/dependency_graph.py` | Task dependency graphs |
| `shared/egg_contracts/orchestrator.py` | Dispatch logic and handoff management |
| `gateway/agent_restrictions.py` | File access validation per role |

## Circuit Breaker and Escalation

**Note:** Circuit breaker functionality is deprecated as of PR #285. The pipeline now relies on PR-based reviews with human-visible feedback at every cycle, reducing the need for automated escalation thresholds.

### Legacy Circuit Breaker (Deprecated)

The circuit breaker tracked implementation cycles and escalated to humans when thresholds were exceeded:

| Metric | Threshold | Action |
|--------|-----------|--------|
| Per-task review cycles | 3 | Escalate task to human |
| Total pipeline cycles | 10 | Open circuit breaker, pause pipeline |

This functionality has been replaced by the PR-based review workflow, which provides continuous human visibility without requiring explicit escalation triggers.

## Human-in-the-Loop Decisions

For detailed documentation on HITL workflows, see [HITL Decisions](../hitl-decisions.md).

### Decision Sync to Contract

Pipeline decisions made during refine and plan phases are automatically synced to the contract after each phase completes. This ensures implement-phase agents have visibility into substantive choices (e.g., database selection, API style, config handling) made earlier in the pipeline.

**How it works:**

1. Agents create decisions via `OrchClient.create_decision()` or by queueing HITL checkpoints
2. Human resolves the decision (via terminal in prompt-driven mode, or checkbox in issue-driven mode)
3. After the phase completes, `_sync_pipeline_decisions_to_contract()` converts resolved non-phase-gate `HITLDecision` objects to contract `Decision` format
4. For phase gate approvals with context/feedback, `_persist_phase_gate_resolution()` additionally syncs the resolution to the contract and appends it to the phase draft file
5. Synced decisions appear in `.egg-state/contracts/{identifier}.json` under the `decisions` array
6. Implement-phase agents can read these decisions from the contract to understand context

Agents can also register questions directly in the contract via `egg-contract add-decision` / `egg-contract add-feedback`. These writes bypass the orchestrator's decision queue. After phase gate approval, `_queue_and_await_contract_decisions()` promotes any unresolved contract HITL decisions and feedback into the orchestrator queue so they are surfaced to the human. Resolutions are written back to the contract before the next phase starts.

**`/sdlc` skill auto-resolution.** When the refiner embeds agent-created `choice`/`feedback` decisions directly in the analysis/plan draft as `<!-- egg-hitl-decision id=cq-N -->` markers, the `/sdlc` Claude Code skill surfaces them upfront during the phase_gate's Step 5 alongside the draft review. Once those same questions subsequently register as standalone contract decisions (promoted to the orchestrator queue by `_queue_and_await_contract_decisions()`), the skill consults its session-scoped `resolved_questions_map` and auto-submits the captured answer — with a one-line `Auto-resolved <decision_id>: selected '<option>' from captured context.` note — instead of re-prompting. Rewordings, or answers incompatible with a `choice` decision's option list, fall through to the normal prompt flow. See [HITL Decisions § `/sdlc` Skill: Auto-Resolving Repeated Questions](../hitl-decisions.md#sdlc-skill-auto-resolving-repeated-questions) for the full protocol.

**What gets synced:**

- Resolved decisions with `decision_type != "phase_gate"` (substantive choices, not process gates) — via `_sync_pipeline_decisions_to_contract()`
- Phase gate approvals that include context or feedback — via `_persist_phase_gate_resolution()`. When a human approves a phase gate with notes, the context is added to the contract as a `[Phase gate: <phase>]`-prefixed decision and appended to the phase draft file as a `## HITL Resolution` section
- Decision question, options, resolution, and resolved_at are carried over; resolved_by is set to `"human"`
- Decisions already present in the contract (matched by question text) are skipped to avoid duplicates

**Key files:**

- `orchestrator/routes/pipelines.py` — `_sync_pipeline_decisions_to_contract()`, `_persist_phase_gate_resolution()`, and `_queue_and_await_contract_decisions()` implementations
- `orchestrator/models.py` — `HITLDecision` model (pipeline state)
- `shared/egg_contracts/models.py` — `Decision` model (contract state)

### Checkbox-Based Interface

HITL decisions render as checkboxes in bot comments. The `<!-- egg-hitl-decision id=... -->` marker identifies each decision:

```markdown
## Human Decision Required

<!-- egg-hitl-decision id=decision-1 -->

**How should we proceed with this task?**

- [ ] Provide additional context or requirements below
- [ ] Adjust the acceptance criteria
- [ ] Break this task into smaller sub-tasks
- [ ] Mark current tasks as complete (override review)
- [ ] Skip remaining tasks in this phase
- [ ] Cancel the pipeline for this issue
- [ ] Other (explain in reply)
```

### Debounce Mechanism

When a checkbox is checked:

1. 30-second countdown starts
2. Comment updates to show: "Selection received. Confirming in 25 seconds..."
3. Additional changes reset the timer
4. After debounce expires: "Decision finalized. Processing now..."

This prevents accidental double-clicks and allows humans to change their mind.

### Detection Workflow

The local orchestrator's decision queue (`orchestrator/decision_queue.py`):

1. Monitors for decision responses
2. Parses checkbox state using `hitl.py`
3. Validates debounce period
4. Updates contract with resolution
5. Resumes pipeline from paused state

## Worktree State Synchronization

The orchestrator pushes worktree state (including `.egg-state/` files) to the remote branch at key pipeline checkpoints. This ensures agents always see the latest contract, drafts, and review verdicts without working on stale data.

**Push points:**

1. **After contract initialization** — Pushes initial contract and analysis/plan drafts so the first agents in the next phase see them
2. **After phase completion** — Pushes statefiles (drafts, reviews, BRC history, check results, contract updates) so the next phase's agents don't have unpushed `.egg-state/` files in their diff
3. **Before PR creation** — Pushes BRC history re-writes and any pending statefiles. This is a safety net for cases where post-phase pushes (point 2) failed silently. Push outcomes are logged at INFO level with the number of local commits ahead of remote (see [PR-Phase State File Troubleshooting](#pr-phase-state-file-troubleshooting))
4. **On pipeline failure** — Best-effort failsafe push to preserve in-progress work

All pushes use `GatewayClient.push_worktree_branch()`, which authenticates directly with the launcher secret (orchestrator-trusted) rather than registering a temporary session token. This bypasses the agent-targeted pipeline-push enforcement so the orchestrator's programmatic pushes are never blocked by #2028-style pipeline-session guards. On non-fast-forward rejection, it automatically performs a `git fetch` + `git rebase` in the worktree and retries the push once before giving up. The call returns a `PushResult` dataclass (truthy on success) whose `category` and `detail` fields describe the underlying git error — so contract-init failures surface an operator-actionable message like `"non_fast_forward: ! [rejected] ... (fetch first)"` instead of the historical opaque `"push_worktree_branch returned False"`.

**Contract init push (point 1) is required**: If it fails after one retry, the pipeline is marked `FAILED` and aborted. Agents must not start before the contract is on the remote — otherwise their diffs would include `.egg-state/` files outside their allowed file boundaries. Post-phase and failsafe pushes (points 2–3) log warnings but do not block pipeline progress.

**Implementation**: See `orchestrator/routes/pipelines.py:_run_pipeline()` and `orchestrator/gateway_client.py:push_worktree_branch()` for the push logic.

## Failure Handling

### Pipeline Failure Recovery

When a pipeline phase fails (container exit code non-zero), the orchestrator:

1. **Sets pipeline status to FAILED**: Marks the phase and pipeline as failed during phase execution
2. **Emits failure event**: Sends `pipeline.failed` event to terminate SSE streams
3. **Best-effort push**: Attempts to push the worktree branch to remote as a backup (using the launcher secret for orchestrator-trusted auth)
4. **Preserves worktree**: Skips cleanup in the `finally` block so in-progress work is not lost

**Restart behavior**:
- `egg-sdlc` CLI detects failed pipelines and automatically restarts from the failed phase (preserving worktrees)
- Orchestrator API `POST /api/v1/pipelines/{id}/start` resets the failed phase to pending and resumes execution
- Worktrees remain intact across restarts — `spawn_agent_container()` calls the gateway's idempotent `create_worktrees` API which rediscovers existing worktrees keyed by `{pipeline_id}-{role}`, so agents resume with all prior committed work
- Uncommitted changes from the previous container are not preserved in the restart flow; agents should commit work incrementally

**Manual recovery via MCP tools**:

When automatic restart is insufficient (e.g., phase stuck in transition, HITL gate not created), use the phase management MCP tools for manual intervention:
- `advance_phase` with `force=true` — force-advance past a stuck phase (stops running containers first to prevent SIGTERM cascading)
- `start_phase` — mark a phase RUNNING (does not spawn agents — agent spawning is driven by the `_run_pipeline` loop)
- `complete_phase` — mark a phase COMPLETE (does not advance the pipeline — call `advance_phase` next)
- `populate_contract` — populate the contract from plan artifacts when it's empty after manual phase setup

See [Phase Management MCP Tools](../reference/orchestrator-cli.md#phase-management-mcp-tools) for full parameter reference and recovery workflow examples.

### External Failure Handling

The pipeline handles external failures gracefully:

| Failure Type | Detection | Handling |
|--------------|-----------|----------|
| **Rate limit** | HTTP 403 with `X-RateLimit-Remaining: 0` | Sleep until `X-RateLimit-Reset`, retry |
| **Network failure** | Timeout, DNS failure | Exponential backoff (1s, 2s, 4s, max 30s), 3 retries |
| **Workflow timeout** | Job exceeds 6-hour limit | Checkpoint state at T-10 minutes |
| **Gateway unavailable** | HTTP 502/503/504 | Retry with backoff, escalate if down >5 min |

### Timeout Checkpointing

The implement job monitors remaining time:

```yaml
- name: Check for timeout checkpoint
  run: |
    REMAINING_MINUTES=$((JOB_TIMEOUT_MINUTES - ELAPSED_MINUTES))
    if [[ $REMAINING_MINUTES -le 10 ]]; then
      # Save state and exit gracefully
      jq '.audit_log += [{"action": "checkpoint", ...}]' contract.json
      git commit -m "Checkpoint state before timeout"
    fi
```

## Document Standards

### Analysis Document (Refine Phase Output)

Path: `docs/issues/{number}-analysis.md`

Template sections:
- Problem Statement
- Current Behavior
- Constraints
- Options Considered
- Recommended Approach
- Open Questions

### Plan Document (Plan Phase Output)

Path: `docs/issues/{number}-plan.md`

Template sections:
- Summary
- Implementation Phases (with tasks and acceptance criteria)
- Test Strategy
- Rollback Plan
- Risk Assessment

**Task ID Format**: Tasks must use `[TASK-{phase}-{number}]` markers for extraction:

```markdown
- [TASK-1-1] Create contract JSON schema — Acceptance: Schema validates test contracts
- [TASK-1-2] Add role validation — Acceptance: Unauthorized mutations rejected
```

**PR Metadata**: The plan should include a `pr:` section in the YAML appendix with a title, description, test plan, and manual steps for the pull request. The `test_plan` field is required — describe both automated test coverage and manual verification steps. The `manual_steps` field lists any pre- or post-merge actions (migrations, config changes, deployments); use an empty string if none. The pipeline uses this metadata when creating and finalizing the PR. If not provided, the orchestrator falls back to the issue title (or a generic stub) and opens the PR as a **draft** with a warning banner in the body that lists any parse errors from the plan draft, so reviewers cannot silently merge a PR whose planner metadata is missing (see #1975).

### Phase Completion Comments

When a phase is complete and ready for human approval, agents post a comment using the [Phase Completion Template](../templates/phase-completion.md). This format includes the `<!-- egg-phase-approval -->` marker which the HITL workflow uses to detect approval checkbox changes.

### Task Population

Tasks are automatically extracted from the plan document and populated into the contract during the plan phase, after the plan document is validated.

The orchestrator's pipeline routes (`orchestrator/routes/pipelines.py`):
1. Fetches the plan document from the draft files
2. Parses task markers and PR metadata using `shared/egg_contracts/plan_parser.py`
3. Writes phases, tasks, and PR metadata into `.egg-state/contracts/{identifier}.json`
4. Validates the contract against the JSON schema
5. Commits the updated contract to the feature branch

This happens in the plan phase itself (before human approval) to provide early validation of the plan format. The implement phase also runs task population as a fallback in case the plan phase step failed or was skipped. For manual recovery via `advance_phase`, the populate step is also run automatically when transitioning out of the plan phase, so `contract.pr` is populated even when a force-advance bypasses the normal phase completion path.

The PR metadata (title and description) from the plan is stored in the contract's `pr` field and used by the orchestrator to auto-create the PR when the implement phase completes. The orchestrator builds the PR body from the contract's `pr` metadata, the git commit log, diff stats, and a Pipeline Context section (pipeline ID and issue number). The gateway injects a machine-parseable `<!-- egg-pipeline-context ... -->` HTML comment and applies `egg` and `agent:orchestrator` labels to the PR — no agent is spawned for PR creation. If neither the contract nor the plan draft on disk contains a `pr.title`, the PR falls through to a stub (issue title or pipeline ID) and is opened as a **draft** with a warning banner listing parse failures so reviewers can diagnose and repair before merging.

## Phase Checks

Each SDLC phase can run automated checks before completion. The check system provides a framework for validating phase outputs and code quality.

### Check Framework

Phase checks are defined in `shared/egg_contracts/phase_defaults.py` and executed by the local orchestrator. Check results have three statuses:
- `PASS`: Check succeeded
- `FAIL`: Check failed (may be fixable)
- `SKIP`: Check skipped (e.g., no test infrastructure found)

### Per-Repository Check Commands

For local orchestrator mode, you can configure explicit check commands per repository in `~/.config/egg/repositories.yaml`:

```yaml
repo_settings:
  your-org/web-app:
    checks:
      - name: lint
        command: npm run lint
      - name: test
        command: npm test
```

When configured, the tester runs these commands sequentially instead of auto-discovering test/lint commands. This is useful when:
- Auto-discovery doesn't find the right commands
- You want to run checks in a specific order
- You need to run custom validation scripts

If not configured, the tester falls back to auto-discovery (scanning for Makefile, package.json, pyproject.toml, etc.). See [Configuration](../../config/README.md#per-repo-check-commands) for setup details.

### Per-Repository Role Patterns

Non-Python repositories often have different file layout conventions (Go uses `*_test.go`, JavaScript uses `__tests__/`, etc.). Without overrides, the default patterns may misroute files to the wrong role. You can configure per-repo role-file conventions in `repositories.yaml`:

```yaml
repo_settings:
  your-org/example-go-repo:
    role_patterns:
      tests_globs: ["**/*_test.go", "**/testdata/**"]
      code_globs:  ["**/*.go"]
      docs_globs:  ["**/*.md", "docs/"]
```

All three keys (`tests_globs`, `code_globs`, `docs_globs`) are optional. Unset keys fall back to the built-in defaults (Python/Go/JS/TS patterns). Each value must be a list of non-empty glob strings.

**Set keys replace defaults — they do not extend them.** If you set `tests_globs`, the configured list completely replaces the built-in defaults; the defaults are not merged in. So a polyglot Python+Go repo that sets `tests_globs: ["**/*_test.go"]` will silently lose `**/*_test.py`, `**/test_*.py`, `**/conftest.py`, and every JS/TS test pattern — those tests would then misroute to the coder. List every convention your repo uses (e.g. `["**/*_test.py", "**/test_*.py", "**/conftest.py", "**/*_test.go"]` for a Python+Go repo).

**What each key controls:**

| Key | Affects roles | Default includes |
|-----|--------------|-----------------|
| `tests_globs` | coder (blocked), tester (allowed) | `tests/`, `**/*_test.py`, `**/*_test.go`, `**/*.test.ts`, etc. |
| `code_globs` | documenter (blocked), autofixer (allowed) | `**/*.py`, `**/*.go`, `**/*.ts`, etc. |
| `docs_globs` | coder (blocked), tester (blocked), documenter (allowed), autofixer (blocked) | `docs/`, `**/*.md` |

The conflict-resolver role's allow list is the union of all three glob lists, so any of these keys also widens what the conflict-resolver can write.

**Security boundary:** Security-relevant blocklists (`.egg-state/contracts/`, `.github/`) are hard-coded and cannot be relaxed by repo config. Only the language-convention globs are configurable.

The orchestrator pre-resolves the override at spawn time and passes it to sandbox containers via the `EGG_PIPELINE_REPO_PATTERNS_JSON` environment variable. The gateway reads the override directly from `repositories.yaml` at push time. Validation behavior differs slightly between paths: `config/repo_config.py::get_repo_role_patterns` (the orchestrator/gateway path that reads `repositories.yaml`) emits a WARNING log on invalid root type, unknown keys, non-list values, and non-string list entries; `shared/egg_restrictions/patterns.py::load_repo_pattern_override` (the env-var path used inside the sandbox) only logs on invalid JSON and silently filters the rest. In practice the operator still sees diagnostic warnings at orchestrator spawn time because the orchestrator runs `get_repo_role_patterns` before serializing into the env var.

### Built-in Checks

| Check | ID | Purpose | Fixable |
|-------|-----|---------|---------|
| **Draft Validation** | `check-draft-validation` | Validates refine phase analysis document | No |
| **Plan YAML** | `check-plan-yaml` | Validates plan phase YAML appendix | No |
| **Merge Conflict** | `check-merge-conflict` | Detects conflicts with base branch | No |
| **Lint** | `check-lint` | Runs `make lint` if available | Yes |
| **Test** | `check-test` | Runs `make test` or pytest | No |
| **Auto-Fixer** | `check-fixer` | Attempts to auto-fix failed checks | N/A |

### Phase Default Configurations

Default checks for each phase are defined in `shared/egg_contracts/phase_defaults.py`:

**Refine phase:**
- Draft validation (required)

**Plan phase:**
- Plan YAML validation (required)

**Implement phase:**
- Merge conflict check (required)
- Lint check (required, 1 retry)
- Test check (required)
- Auto-fixer (optional)

**PR phase:**
- No checks
- PR is auto-created by the orchestrator (no agent spawned). The PR title and description are sourced from the contract's `pr` field (populated by the plan agent), with commit log and diff stats appended automatically. When BRC consensus was active, a one-line pointer to the committed per-phase BRC history transcripts is included in the PR body (linked from `.egg-state/brc-history/`). See [Concurrent Execution — BRC History Link in PR Body](concurrent-execution.md#brc-history-link-in-pr-body) for details.
- **Agent-outputs cleanup**: At PR-phase entry, the orchestrator removes `.egg-state/agent-outputs/` from the branch via `_cleanup_agent_outputs_for_pr()`. These files are ephemeral coder→tester handoff artifacts (e.g., `coder-test-changes.patch`) that the tester has already consumed. Leaving them causes merge conflicts in concurrent pipelines and pollutes the PR diff. Cleanup is best-effort — failures are logged but do not block PR creation.
- **BRC history safety net**: Before PR creation, the orchestrator re-writes BRC history files for all completed phases via `_write_brc_history()`. This is a safety net — BRC history is normally written at each phase boundary, but per-phase pushes can fail silently. Re-writing in the PR phase ensures BRC history files are always present in the PR diff. All functions in this chain emit INFO-level diagnostic logs at entry, exit, and each early-return path (see [PR-Phase State File Troubleshooting](#pr-phase-state-file-troubleshooting)).
- **Pre-PR-open rebase** (#2224): Immediately before calling `gh pr create`, the orchestrator rebases the pipeline branch against the current `origin/<base_branch>` via `_refresh_pipeline_branch_against_current_base()`. Phase-start rebases (`_rebase_pipeline_branch_onto_base`) only run once per phase iteration; if `base_branch` advances *during* the PR phase, the pipeline branch ends up behind. This step closes that gap so the PR opens with a clean linear diff. The operation is best-effort — on any failure (rebase conflict, push rejection, transient gateway error) the PR still opens against the un-rebased tip and the divergence is visible to the human reviewer. Only the pipeline branch is ever written to; `base_branch` is read-only here.
- **Draft preservation**: Pipeline-specific draft files (`.egg-state/drafts/{id}-analysis.md`, `.egg-state/drafts/{id}-plan.md`) are **preserved** on the PR branch as artifacts of the pipeline's reasoning. Reviewers can compare the planned approach against the shipped code, and post-hoc debugging has the analysis and plan available as a baseline (see #1713). The PR phase used to remove these files to keep diffs focused; that behavior was reverted because the audit value outweighs the diff noise.
- If PR creation returns no URL, the pipeline is marked **FAILED** immediately. The overseer also runs a safety-net check at pipeline completion: if `current_phase=pr` but no `pr_url` is in the phase artifacts, it creates a HITL decision and Slack notification to prevent stranded branch work from going unnoticed.

### Customizing Phase Checks

Contracts can override phase defaults via the `phase_configs` field:

```json
{
  "phase_configs": {
    "implement": {
      "checks": [
        {
          "id": "check-custom",
          "name": "Custom Check",
          "script": "custom_check.py",
          "required": true,
          "retry_on_fail": false,
          "max_retries": 0
        }
      ],
      "max_review_cycles": 5,
      "human_review_mechanism": "PR_REVIEW"
    }
  }
}
```

When `phase_configs.{phase}.checks` is specified, it completely replaces the default checks for that phase.

### Writing Custom Checks

Custom checks can be configured per-repository in `~/.config/egg/repositories.yaml` (see above) or by adding check definitions to `shared/egg_contracts/phase_defaults.py`.

### Check DAG Configuration

The implement phase runs checks in a directed acyclic graph (DAG) order to optimize execution:

```
merge-fix ─┬─> lint ──┬─> fixer ─> review
           └─> test ──┘
```

**Execution order:**

1. **merge-fix** - Resolves merge conflicts with base branch (blocking)
2. **lint** and **test** - Run in parallel after merge-fix completes
3. **fixer** - Attempts auto-fixes for any failed checks
4. **review** - PR-based code review (only runs if checks pass)

This parallel execution reduces cycle time by running independent checks concurrently. The fixer step allows the agent to attempt automated corrections before requiring human intervention.

### Tester Check and Fix Loop

The tester handles lint, type-checks, and test execution alongside writing tests. After the coder completes, the tester:

1. **Runs all checks** — Discovers and executes test/lint commands (or uses configured commands)
2. **Fixes test files inline** — Attempts auto-fixable repairs in test files only; source code issues are reported to the coder
3. **Repeats up to 3 times** — Re-runs checks after each fix attempt until all pass or attempts are exhausted

**Flow:**
```
work → tester (run checks → fix → re-run, up to 3x) → review
```

## Implementation Reference

### Key Files

| File | Purpose |
|------|---------|
| `orchestrator/container_spawner.py` | Agent container lifecycle |
| `orchestrator/decision_queue.py` | HITL decision handling (typed decisions) |
| `orchestrator/models.py` | Pipeline and HITLDecision models (decision_type, questions) |
| `orchestrator/state_store.py` | Git-backed pipeline state |
| `orchestrator/routes/pipelines.py` | Pipeline API, prompt building, JSON resolution parsing |
| `sandbox/egg_lib/sdlc_hitl.py` | Type-aware terminal HITL handler |
| `sandbox/egg_lib/orch_client.py` | Orchestrator API client (create_decision with type support) |
| `.github/workflows/reusable-review.yml` | PR-based code review workflow |
| `sandbox/scripts/gh` | gh wrapper with self-review fallback |
| `shared/egg_contracts/models.py` | Pydantic models for contract (includes CheckDefinition, CheckResult, PhaseConfig) |
| `shared/egg_contracts/agent_roles.py` | Agent role definitions and file access patterns |
| `shared/egg_contracts/orchestration.py` | Orchestration state management |
| `shared/egg_contracts/dependency_graph.py` | Dependency graph and wave computation |
| `shared/egg_contracts/orchestrator.py` | Dispatch logic and handoff management |
| `shared/egg_contracts/phase_defaults.py` | Default check configurations per phase |
| `shared/egg_contracts/plan_parser.py` | Parses plan documents for task extraction |
| `shared/egg_contracts/roles.py` | Role definitions and field ownership |
| `shared/egg_contracts/validator.py` | Mutation validation |
| `shared/egg_contracts/hitl.py` | Checkbox parsing and debounce |
| `.egg/schemas/contract.schema.json` | JSON schema definition |
| `.egg/phase-permissions.json` | Phase operation restrictions |

### egg-sdlc CLI

The `egg-sdlc` CLI provides an interactive terminal interface for driving SDLC pipelines. It replaces the previous Claude-as-collaborator approach with direct user control.

**Usage:**

```bash
# Issue-driven: start/attach to pipeline for a GitHub issue
egg-sdlc -r <repo_dir> -i <issue_number>
egg-sdlc -r <repo_dir> <issue_number>        # Short form (positional issue)
egg-sdlc --private -r <repo_dir> -i <issue_number>  # Private mode (network lockdown)
egg-sdlc -r <repo_dir> -i <issue_number> --base develop  # Custom base branch for PR

# Prompt-driven: interactive pipeline (no GitHub issue)
egg-sdlc
```

**Note:** Issue-driven pipelines require the `-r/--repo` flag specifying the repository directory name under `~/repos/` (e.g., `egg`). The flag also accepts full `owner/repo` format for direct specification. Repo autodetection was removed in favor of explicit specification.

Use `--base <branch>` to target a non-default base branch for the auto-created PR (e.g., `--base develop`). When omitted, the PR targets the repo's default branch.

**Features:**
- Real-time DAG visualization (reuses `egg-pipeline-watch` SSE patterns)
- Type-aware HITL checkpoints that render differently based on `decision_type`:
  - **Phase gate** (`phase_gate`): Full document in pager with view, edit, approve, and request-changes options; surfaces pending contract decisions via `[q]` option
  - **Choice** (`choice`): Numbered options for discrete selection
  - **Feedback** (`feedback`): Per-question prompts with review-before-submit
  - **Generic fallback**: Legacy 5-option menu for unknown decision types
- Universal options on every checkpoint: general feedback (`[f]`), change approach (`[a]`), cancel (`[c]`)
- JSON resolution payloads for structured intent parsing (see [HITL Decisions](../hitl-decisions.md))
- Automatic reconnection on SSE timeouts
- Works both inside containers and from the host

**Host-side:** `bin/egg-sdlc` launches a container with TTY passthrough for interactive features.

**In-container:** `sandbox/bin/egg-sdlc` runs the Python CLI directly.

### Triggering the Pipeline

**Via egg-sdlc** (recommended for interactive use):
```bash
egg-sdlc -r egg -i 123
```

The SDLC pipeline can also be triggered via the local orchestrator API:

```bash
# Via orchestrator API — GitHub issue-driven
curl -X POST http://localhost:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 123, "repo": "owner/repo", "branch": "egg/issue-123"}'

# Via orchestrator API — JIRA ticket-driven (pipeline ID and branch derived from ticket)
curl -X POST http://localhost:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id": "KORE-1234", "repo": "owner/repo", "branch": "egg/KORE-1234", "prompt": "Add auth middleware"}'

# Via egg-orch CLI
egg-orch pipeline create --issue 123
```

**JIRA ticket-based pipelines**: Pass `jira_ticket` (e.g. `KORE-1234`) to the `submit_task` MCP tool, which translates it into `pipeline_id` and `branch` for the API. When using the REST API directly, pass `"pipeline_id": "KORE-1234"` and `"branch": "egg/KORE-1234"` explicitly (as shown above).

**JIRA Epic mode (issue #1557)**: When `jira_ticket` resolves to a Jira **Epic**, the pipeline runs in epic mode — the refine output is shaped as the epic's Description body, and the plan output decomposes into one Jira child ticket per plan node. `submit_task` accepts an optional `mode` parameter that selects the epic flow:

| `mode` | Behaviour |
|--------|-----------|
| `auto` (default) | Detect the epic's existing children at submit time: if any are present, run **reassess**; otherwise run **fresh**. |
| `fresh` | Treat the epic as having no usable children — the planner ignores existing tickets and proposes a clean slate of new children. |
| `reassess` | Force the reassess flow — requires the ticket to be an Epic with at least one existing child (the orchestrator rejects `reassess` on a non-epic ticket with HTTP 400). |

`mode` is only meaningful in combination with `jira_ticket`; passing it without one returns an error. The orchestrator forwards it as the wire field `epic_mode` on the create-pipeline API so it doesn't collide with the existing `mode` field (`PipelineMode`: `issue` / `babysit` / `custom`). At runtime the orchestrator exports two derived env vars into the agent sandboxes: `EGG_IS_EPIC` (`'true'` / `'false'`) and `EGG_EPIC_MODE` (canonical `ticket` / `github_issue` / `epic-fresh` / `epic-reassess`); the refiner / task-planner / applier prompts switch on these to pick the right mode block. See [`plugins/refine-plan/skills/refine-plan/agents/refiner.md`](../../plugins/refine-plan/skills/refine-plan/agents/refiner.md) for the mode-switch table.

**Qualifier support**: The `submit_task` MCP tool accepts an optional `"qualifier"` suffix for both issue-driven and JIRA-driven pipelines (e.g. `"qualifier": "backend"` produces pipeline ID `issue-123-backend` / branch `egg/issue-123-backend`). When using the REST API directly, append the qualifier to `pipeline_id` and `branch` manually (e.g. `"pipeline_id": "KORE-1234-backend"`, `"branch": "egg/KORE-1234-backend"`). If the target branch already exists and an active pipeline is running for that ID, the orchestrator returns HTTP 409 with a hint to use a qualifier. Branches from prior terminal (cancelled/failed/complete) pipelines are reused automatically.

Pipeline ID formats:
- `issue-{number}[-qualifier]` — GitHub issue-driven
- `{TICKET}[-qualifier]` — JIRA ticket-driven (e.g. `KORE-1234`, `KORE-1234-backend`)
- `pr-{number}` — babysit mode (one-off implement-phase BRC cycle against a PR; triggered via the `/babysit-pr` MCP skill with `mode=babysit` and `pr_number=N`)
- `local-{8hex}` / `pipeline-{8hex}` — prompt-driven

**Short-flow pipelines** — skip refine/plan phases and start directly at implement by passing `start_phase: implement` in `config`, along with pre-generated `analysis` and `plan` content. The orchestrator writes these to draft files and parses the plan's `yaml-tasks` appendix to populate the contract:

````bash
curl -X POST http://localhost:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 123,
    "repo": "owner/repo",
    "branch": "egg/issue-123",
    "config": {"start_phase": "implement", "hitl_gates": false},
    "analysis": "# Analysis\n...",
    "plan": "# Plan\n...\n```yaml\n# yaml-tasks\n...\n```"
  }'
````

The `analysis` and `plan` fields are also accepted by the `submit_task` MCP tool. Both are cleared from pipeline state after the first run once the draft files are pushed to the feature branch.

**Source branch artifact loading** — instead of passing large `analysis` and `plan` content inline (which can be 50-80KB+ and strain MCP transport limits), pass a `source_branch` parameter pointing to a prior run's branch. The orchestrator reads artifacts server-side from the source branch during pipeline setup:

- `.egg-state/drafts/{prefix}-plan.md` → pipeline plan (parsed to populate the contract)
- `.egg-state/drafts/{prefix}-analysis.md` → pipeline analysis
- `.egg-state/contracts/{identifier}.json` → SDLC contract including resolved HITL decisions (rebind to new pipeline ID before writing)

The contract pull is best-effort: if the source branch contract is missing, invalid, or unreachable, the orchestrator falls back to creating a fresh contract. This preserves HITL decisions (database selection, API style, config choices, etc.) resolved in the prior run so they are not lost on resubmission.

Unlike the plan/analysis drafts above, the contract `{identifier}` is **not** subject to the prefix-resolution fallback below. The helper canonicalizes `issue_number` to `issue-<N>` when present, falling back to `pipeline_id` only when `issue_number` is None — qualifier-keyed contracts (e.g. `issue-123-v3.json`) are not tried.

Prefix resolution order for exact-path lookup:

1. `source_artifact_prefix` (explicit override, e.g. `"issue-123-v3"`) — if set, only this prefix is tried before the fallback
2. `pipeline_id` (includes qualifier, e.g. `"issue-123-v7"`) — tried when it differs from the bare issue prefix
3. Bare issue prefix (e.g. `"issue-123"`)

If none of the exact prefixes match, the orchestrator falls back to listing available draft files via `git ls-tree` and reads the first matching `*-plan.md` / `*-analysis.md` for the same issue number. Inline `analysis` and `plan` values always take precedence over `source_branch` — explicit content wins.

````bash
# Via REST API — load artifacts from a prior run's branch
curl -X POST http://localhost:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 123,
    "repo": "owner/repo",
    "branch": "egg/issue-123-v2",
    "config": {"start_phase": "implement"},
    "source_branch": "egg/issue-123-v1"
  }'
````

Use `source_artifact_prefix` when the source branch used a different pipeline ID or qualifier than the current one:

````bash
curl -X POST http://localhost:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": 123,
    "repo": "owner/repo",
    "branch": "egg/issue-123-v4",
    "config": {"start_phase": "implement"},
    "source_branch": "egg/issue-123-v1",
    "source_artifact_prefix": "issue-123-v1"
  }'
````

The `source_branch` and `source_artifact_prefix` parameters are also accepted by the `submit_task` MCP tool:

```json
{
  "description": "Implement auth middleware",
  "repo": "owner/repo",
  "issue_number": 123,
  "qualifier": "v2",
  "config": {"start_phase": "implement"},
  "source_branch": "egg/issue-123-v1",
  "source_artifact_prefix": "issue-123-v1"
}
```

**Branch reuse for resubmissions** — when resubmitting a pipeline after a prior run was cancelled or failed, `create_pipeline` checks two conditions before allowing branch reuse:

1. **Active pipeline conflict**: if the branch exists and an active (non-terminal) pipeline holds it, the call returns HTTP 409 with `reason: branch_exists`. Use a qualifier to create a parallel pipeline, or cancel the existing one first.
2. **Stale branch guard** (#2222): if the branch exists, no active pipeline holds it, but the branch tip differs from `origin/<base_branch>`, the call returns HTTP 409 with `reason: stale_branch`. This prevents a new pipeline from inheriting commits left by a prior failed/cancelled run — which would contaminate the resulting PR via the push-reconcile path. The response body includes a `hint` field pointing to the resolution: `cancel_task(task_id='<id>', cleanup=true)` deletes the stale branch and pipeline state so the resubmission can proceed cleanly. When the call is made without a `pipeline_id` (rare — most callers carry one), the `hint` falls back to the generic `"Delete the stale branch and any associated pipeline state, then resubmit."` form since `cancel_task` has no task to target.

`<base_branch>` is the request's `base_branch` parameter when supplied, otherwise the orchestrator's auto-detected default (`origin/HEAD` → `origin/main` → `origin/master`, in that order). Branch reuse proceeds silently only when the branch tip equals `origin/<base_branch>` (a fresh branch carrying no prior-pipeline commits).

Other 409 conditions on `create_pipeline` (`pr_merged`, `pr_closed`, `pr_empty_diff`, and a `StateStoreError` "already exists" catch-all) are unrelated to branch reuse and are surfaced with their own `reason` codes.

### Contract CLI Commands

Update the contract incrementally after each commit — do not batch updates at the end.

```bash
# View contract state
egg-contract show

# Mark task done and link commit (implementer)
egg-contract complete-task --task task-1-1 --commit abc1234

# Mark phase done and link commit (implementer)
egg-contract complete-phase --phase phase-1 --commit abc1234

# Link commit to task without marking done (implementer)
egg-contract add-commit --task task-1-1 --commit abc1234

# Add implementation notes (implementer)
egg-contract update-notes --task task-1-1 --notes "Completed validation"

# Create HITL decision point (plain text)
egg-contract add-decision --question "Should we proceed with approach X?"

# Create HITL decision point with markdown checkbox format (for GitHub comments)
egg-contract add-decision --question "Which approach?" --options "A" "B" --format markdown

# Create feedback comment for open-ended questions
egg-contract add-feedback --question "What is the expected request volume?" --question "Should we support legacy browsers?" --format markdown
```

---

## Concurrent Execution Mode

Concurrent execution mode enables all agents (coder, tester, documenter, reviewer_code,
reviewer_contract, reviewer_security, reviewer_concurrency) to run simultaneously during the implement phase,
collaborating via a polling-based message bus hosted by the orchestrator.

### Configuration

Enable concurrent execution with the `--concurrent` CLI flag:

```bash
# Issue-driven
egg-sdlc -r egg -i 999 --concurrent

# Prompt-driven
egg-sdlc -r egg -p "Add feature X" --concurrent

# Via egg-orch directly
egg-orch pipeline create --repo owner/repo --issue 999 --branch egg/issue-999 --concurrent
```

Or pass it in the pipeline config JSON (e.g. via the API):

```json
{
  "config": {
    "concurrent_execution": true,
    "max_concurrent_agents": 6,
    "message_poll_hint_seconds": 30,
    "agent_idle_timeout_minutes": 60
  }
}
```

Leave `consensus_timeout_minutes` unset to use the calibrated per-phase
defaults below (refine 30 / plan 60 / implement 90). To tune a single
phase, set the per-phase override — `consensus_timeout_minutes_implement: 120`
to give implement extra runway without touching refine/plan. Setting the
legacy global (`consensus_timeout_minutes`) overrides *every* phase, so a
value of `30` would shrink plan from 60→30 and implement from 90→30; prefer
per-phase overrides unless that uniform behaviour is intended.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `parallel_agents` | bool | `true` | Run independent agents in parallel |
| `max_review_cycles` | int | `3` | Max agentic review cycles per phase |
| `max_hitl_review_cycles` | int | `3` | Max HITL revision cycles per phase |
| `hitl_gates` | bool | `true` | Pause for human approval after refine and plan phases |
| `concurrent_execution` | bool | `false` | Enable concurrent mode (opt-in) |
| `concurrent_phases` | list[str] | `["refine", "plan", "implement"]` | Phases where BRC is active when `concurrent_execution` is `false` |
| `start_phase` | str | `null` | Skip earlier phases and start execution from `"plan"` or `"implement"`. When set to `"implement"`, pass top-level `analysis`/`plan` fields to seed the contract (see Short-flow pipelines above). |
| `max_concurrent_agents` | int | `6` | Maximum agents running simultaneously |
| `message_poll_hint_seconds` | int | `30` | Suggested polling interval for agents |
| `consensus_timeout_minutes` | int \| null | `null` | Global consensus timeout. When set, applies to every phase. When `null` (the default), each phase falls back to the calibrated per-phase default below. |
| `consensus_timeout_minutes_refine` | int \| null | `null` (effective `30`) | Per-phase consensus timeout for refine. Wins over the legacy global. |
| `consensus_timeout_minutes_plan` | int \| null | `null` (effective `60`) | Per-phase consensus timeout for plan. Wins over the legacy global. |
| `consensus_timeout_minutes_implement` | int \| null | `null` (effective `90`) | Per-phase consensus timeout for implement. Wins over the legacy global. |
| `brc_consensus_progress_gate_seconds` | int | `300` | Defer the consensus-timeout `OVERSEER_ALERT` while BRC bus or container heartbeats are active. Set to `0` to disable. |
| `post_consensus_iteration_budget_seconds` | int | `3600` | Per-iteration wait budget after consensus timeout. Resets on each new `CONSENSUS_PROPOSE` from a producer. |
| `post_consensus_max_total_seconds` | int | `14400` | Hard ceiling on total post-timeout wait. Must be ≥ `post_consensus_iteration_budget_seconds`. |
| `agent_idle_timeout_minutes` | int | `60` | Agent idle timeout |
| `overseer_enabled` | bool | `true` | Enable the overseer agent for pipeline health monitoring |
| `spawn_max_retries` | int | `2` | Max additional retry attempts for transient gateway worktree-creation failures during agent spawn. Total attempts = `spawn_max_retries + 1`. Set to `0` to disable retry. |
| `spawn_retry_initial_backoff_seconds` | float | `2.0` | Initial backoff between spawn retries. Subsequent attempts scale by 2.5× (e.g. 2 s, 5 s, 12.5 s). |
| `phase_spawn_max_retries` | int | `2` | Max phase-level retry attempts when one or more roles fail with a transient spawn error (e.g. gateway cold start). Total phase-level attempts = `phase_spawn_max_retries + 1`. Only failed roles are respawned; survivors continue running. Per-role retries (`spawn_max_retries`) run first; this outer budget covers longer outages (~30 s+). Set to `0` to disable. |
| `phase_spawn_retry_initial_backoff_seconds` | float | `30.0` | Initial backoff before the first phase-level spawn retry. Subsequent attempts scale by 3× (e.g. 30 s, 90 s). |

All phases use concurrent BRC execution by default.

### Message Protocol

Agents communicate via the orchestrator message bus using structured envelopes:

```
┌─────────────────────────────────────────────────────┐
│  Message Envelope                                    │
│  id: "msg-abc123"                                    │
│  pipeline_id: "issue-999"                            │
│  from_role: "coder"                                  │
│  to_role: "tester" | "all"                           │
│  message_type: "PROGRESS" | "STATUS" | "HANDOFF" | "HEARTBEAT" │
│  subject: "API endpoints complete"                   │
│  body: "Implemented GET/POST/DELETE for /api/users"  │
│  timestamp: "2026-03-11T10:30:00Z"                   │
└─────────────────────────────────────────────────────┘
```

**Message types**:

| Type | Purpose | Example |
|------|---------|---------|
| `PROGRESS` | Notify about completed work | Coder: "API endpoints committed" |
| `STATUS` | Share current activity | Documenter: "Documenting API section" |
| `HANDOFF` | Signal a role-boundary artifact for another agent | Coder: "Test scaffolding ready — tester should create test files" |
| `HEARTBEAT` | Typed agent state transition (`WORKING`/`WAITING_ON_ROLE`/`WAITING_FOR_EVENT`/`PROPOSED`/`IDLE`) emitted via `egg-orch message heartbeat` (`WAITING_FOR_EVENT` is auto-emitted by `egg-orch message wait-loop`) | Tester: `state=WAITING_ON_ROLE`, `waiting_on=coder` |
| `AGENT_FAILED` | System notification of failure | System: "Tester agent crashed" |

> `QUESTION` was removed in [#1897](https://github.com/jwbron/egg/issues/1897) — it had no reliable respondent. Reviewer questions go in `NACK` rationales; producer-to-producer requests go in `HANDOFF`; "I'm blocked on peer X" goes in a `HEARTBEAT` with `state=WAITING_ON_ROLE`. See [Agent Wait Patterns](../reference/agent-wait-patterns.md#anti-pattern-4--question-bus-messages-as-informal-status).

**CLI commands**:

```bash
# Send a progress update to another agent
egg-orch message send --to tester --type PROGRESS --subject "API done" --body "..."

# Send a role-boundary handoff
egg-orch message send --to tester --type HANDOFF --subject "Test files ready" --body "See commit abc1234"

# Poll for new messages
egg-orch message poll [--since msg-abc123] [--limit 50]

# Check message bus status
egg-orch message status
```

### Consensus Protocol

Phase completion in concurrent mode uses a consensus-based approach:

1. Each agent works independently on its tasks
2. When an agent completes its work, it signals `READY`
3. Phase completes when **all** agents signal `READY`
   - The orchestrator polls every 5 seconds and stops containers immediately on consensus
4. Any agent can object (signal `OBJECTING`) to block completion
   - A HITL decision is created with options: **Override objections**, **Wait for resolution**, **Abort phase**
5. Timeout (per-phase: refine 30 / plan 60 / implement 90 by default; configurable via `consensus_timeout_minutes_<phase>` or the legacy global `consensus_timeout_minutes`) publishes a non-blocking `OVERSEER_ALERT` (subject `consensus-timeout: <agent_role> [<priority>]`) rather than gating on a HITL decision — see [issue #2264](https://github.com/jwbron/egg/issues/2264)
   - The `/sdlc` skill surfaces the alert (Check agent logs / Acknowledge / Cancel pipeline)
   - The orchestrator continues polling for consensus under the post-timeout budget; operators can intervene with `cancel_task`, `restart_phase`, or `provide_input`
6. If a container exits cleanly without signaling `READY`, the consensus wrapper restarts it with a recovery prompt (up to `MAX_CONSENSUS_RESTARTS`, default 2). After exhausting restarts, the wrapper performs a final consensus check — if consensus has already been reached (`is_complete=True`), it exits with code 0 (success). Only if consensus is genuinely incomplete does it exit with code 1, triggering the single-agent failure path (HITL decision: retry, abort, or continue without). See [Concurrent Execution: Consensus Wrapper](concurrent-execution.md#consensus-wrapper).
7. **Consensus gates phase advancement unconditionally.** When all containers have exited — whether with failures or cleanly — the orchestrator performs a final consensus recheck before returning success. If BRC consensus is incomplete, the phase fails (exit code 1) regardless of individual container exit codes. This prevents a PR from being opened when agents exit code 0 without completing the full BRC lifecycle. See [Concurrent Execution: All-Container-Exit Consensus Recovery](concurrent-execution.md#all-container-exit-consensus-recovery).

**Readiness states**:

| State | Meaning |
|-------|---------|
| `WORKING` | Agent is actively working (initial state) |
| `READY` | Agent has completed its work |
| `BLOCKED` | Agent cannot proceed (awaiting input/dependency) |
| `OBJECTING` | Agent disagrees with phase completion |

Agents can transition from `READY` back to `WORKING` if new information requires
additional work (e.g., a message from another agent reveals an issue).

**CLI commands**:

```bash
# Signal readiness
egg-orch signal readiness --state READY --reason "All tests pass"

# Signal objection
egg-orch signal readiness --state OBJECTING --reason "Found failing test"
```

### Agent Behavior

Each agent role has specific behavior patterns in concurrent mode:

**Coder**: Implements code and sends `PROGRESS` messages when key interfaces are
committed. Answers tester/documenter clarifications through proposal summaries and
commit messages (and, where the reviewer pass surfaces an ambiguity, by addressing
the `NACK` rationale on re-propose). Signals `READY` after all implementation tasks
are committed.

**Tester**: Begins scaffolding tests early. Polls for coder `PROGRESS` to know when
code is ready. Raises ambiguities through the review cycle — either via `NACK`
rationale when reviewing the coder, or by emitting a `HEARTBEAT` with
`state=WAITING_ON_ROLE --waiting-on coder` so the overseer can see the block.
Signals `READY` after tests pass.

**Documenter**: Starts documentation based on the plan. Refines as implementation
solidifies. Polls for `PROGRESS` from coder/tester. Signals `READY` after docs cover
all changes.

**Reviewer (code/contract)**: Reviews committed code or contract artifacts. Polls for
`PROGRESS` from coder. Signals `READY` after review is complete.

### Branch Model

For the **refine** and **plan** phases, all concurrent agents operate on the pipeline's
shared branch (e.g., `egg/issue-999`) — they commit directly to a single shared history
and coordinate via the message bus to sequence commits and avoid conflicts (for example,
the coder signals `HANDOFF` when its changes are committed so downstream agents know it
is safe to pull and build on top).

For the **implement** phase, the pipeline branch is no longer shared across the whole
team. Tasks are split into a DAG of slices, and each slice runs on its own integration
branch (`egg/issue-999/slice-M`); the shared-history coordination above applies *within*
a slice's agent team. See [Slice-DAG Implement Phase](../architecture/slice-dag.md).

### Failure Handling

**Single agent failure**:
1. Error is logged
2. `AGENT_FAILED` message sent to all other agents
3. HITL decision created with options: **Retry** (respawn), **Abort phase** (stop all),
   or **Continue without** (proceed without the failed agent)

**Multiple simultaneous failures** (2+ agents within 60 seconds):
- Phase is immediately aborted
- All remaining agents are stopped
- HITL decision created for human investigation

**Failure during consensus** (after READY signal):
- Agent's READY signal is revoked
- Treated as a single agent failure (above)

### Monitoring

Pipeline status includes concurrent execution data when the feature is enabled:

```bash
egg-orch pipeline status issue-999
```

Response includes a `concurrent` section:

```json
{
  "concurrent": {
    "enabled": true,
    "max_concurrent_agents": 6,
    "messages": {
      "total": 12,
      "by_type": {"PROGRESS": 5, "HEARTBEAT": 3, "STATUS": 4, "HANDOFF": 0}
    },
    "consensus": {
      "agents": {
        "coder": {"state": "READY", "reason": "Implementation complete"},
        "tester": {"state": "WORKING", "reason": null},
        "documenter": {"state": "READY", "reason": "Docs updated"},
        "reviewer_code": {"state": "WORKING", "reason": null},
        "reviewer_contract": {"state": "READY", "reason": "Contract approved"}
      },
      "is_complete": false,
      "blocking_agents": ["tester", "reviewer_code"]
    }
  }
}
```

Inter-agent message history is also captured in agent checkpoints and visible via:

```bash
egg-checkpoint show ckpt-<id>
```

### Troubleshooting

**Agent not receiving messages**: Check that the agent is polling with the correct
role. Messages are filtered by `to_role` — only targeted messages and broadcasts
(`to_role: "all"`) are returned.

**Consensus timeout**: If agents don't reach consensus within the resolved per-phase
budget (`consensus_timeout_minutes_<phase>` if set, else the legacy global
`consensus_timeout_minutes`, else the calibrated default — refine 30 / plan 60 /
implement 90), the orchestrator publishes an `OVERSEER_ALERT` (subject
`consensus-timeout: <agent_role> [<priority>]`, matching the SDLC skill's
`<anomaly_type>: <agent_role> [<priority>]` convention so "Check agent logs" can
extract the role) rather than gating the pipeline on a `choice` decision
(see [issue #2264](https://github.com/jwbron/egg/issues/2264)). The SDLC skill surfaces the alert via
its existing notification flow (Check agent logs / Acknowledge / Cancel pipeline). Check agent
states via `egg-orch pipeline status` to identify blocked or stuck agents; intervene with
`cancel_task` or `restart_phase` if you want to act.

**Message bus empty**: Verify the pipeline has `concurrent_execution: true` in its
config. The message bus is only active for concurrent pipelines.

**Commit conflicts**: Within a single team's branch (the pipeline branch for refine/plan,
or a slice's integration branch for implement), concurrent agents coordinate commits via
the message bus to avoid conflicts. If an agent encounters a conflict when pushing, it
should pull, rebase, and retry. If conflicts persist, the agent signals `BLOCKED` and a
HITL decision is created. Consider adding role-based file restrictions to minimize overlap.

## Agent MCP tools (`EGG_MCP_TOOLS` flag)

**Default: on since [#1942](https://github.com/jwbron/egg/issues/1942)** — set `EGG_MCP_TOOLS=false` per pipeline to opt out.

Sandbox agents call pipeline lifecycle operations (BRC consensus, HITL decisions, phase context, progress signals, task completion, checkpoint browsing) through first-class Claude Agent SDK MCP tools rather than shelling out to `egg-contract` / `egg-orch` / `egg-checkpoint` via `Bash`. The tools run **in-process** via `claude_agent_sdk.create_sdk_mcp_server` — no new network service, no new auth layer, no new process. See the [Agent MCP Tools reference](../reference/agent-tools.md) for the full 29-verb inventory across 6 namespaces (`mcp__sdlc__*`, `mcp__brc__*`, `mcp__phase__*`, `mcp__progress__*`, `mcp__task__*`, `mcp__checkpoint__*`), schemas, and architecture.

**Opt out per-pipeline.** Iteration 1 (#1765) shipped this default-off; #1942 flipped the default after the wire-up stabilised. Set `EGG_MCP_TOOLS` to a falsy value (`false`, `0`, `no`, `off`) on your sandbox pod env to disable:

```bash
# Per-pipeline opt-out via submit-task / pipeline-create payload
{
  "config": {
    "env": {"EGG_MCP_TOOLS": "false"}
  }
}
```

Or export it in a local-quickstart shell before running `egg-sdlc`. When the flag is opted out, `shared/egg_agent/client.py::run_agent_async` runs the pre-#1765 code path verbatim — no `mcp_servers` registration, no system-prompt changes, no import cost.

Iteration 2 (#1917) shipped peer-read, checkpoint, overseer-alert, task-gap, and additional contract/phase verbs; anchor verbs remain deferred to iteration 3. The existing `sandbox/bin/egg-*` CLIs continue to work unchanged (decision-4).

## Pipeline Health Monitoring

Pipeline health monitoring uses a **two-tier architecture** to detect and remediate agent stalls, errors, and anomalies without human intervention when possible.

### Tier 1: Orchestrator Deterministic Rules

The orchestrator runs five deterministic tripwire checks against live telemetry:

- **Heartbeat timeout**: No heartbeat/progress within the phase-aware threshold triggers escalation to the overseer (or HITL if overseer disabled). The implement phase uses `orchestrator_implement_heartbeat_timeout_seconds` (default 600s); all other phases use `orchestrator_heartbeat_timeout_seconds` (default 120s). Reviewer-only agents correctly idle in BRC protocol (waiting for upstream producers to propose) are excluded from these checks.
- **Container exit**: Unexpected container death triggers immediate HITL escalation.
- **Repeated errors**: Identical error repeated `orchestrator_error_repeat_threshold` times (default 3) escalates.
- **Message volume spike**: Messages exceeding `orchestrator_message_rate_limit` per minute (default 20) triggers auto-throttle.
- **Progress stall**: No structured progress events within the threshold triggers escalation to the overseer/HITL.

Agents emit structured progress via `egg-orch progress emit --step <text> --state <working|blocked|complete>`. Query progress with `egg-orch progress query`.

### Tier 2: LLM Overseer

When Tier 1 escalates an anomaly (and `overseer_enabled` is true in PipelineConfig), the overseer uses a two-model approach:

1. **Haiku classifier** — fast, cheap classification: `stuck`, `legitimate_work`, `recoverable`, `fatal`, `loop_detected`, `off_track`
2. **Sonnet/Opus decision maker** — deeper analysis producing actions: `redirect` (send corrective message), `file_issue` (create GitHub issue with `agent:overseer` label plus the matching priority label `p0`/`p1`/`p2`/`p3` per issue [#1962](https://github.com/jwbron/egg/issues/1962)), `escalate_hitl` (create HITL decision), or `no_action`

### Escalation Ladder

Tier 1 (orchestrator) escalates directly to the overseer/HITL on heartbeat/progress timeout. The overseer's corrective action ladder is: nudge/redirect → HITL → issue → Slack. Each step is tried before escalating further. `overseer_max_redirects_before_escalation` (default 2) controls how many redirect attempts before HITL.

### Troubleshooting

- **Query health alerts**: `egg-orch health alerts`
- **Query progress events**: `egg-orch progress query --agent <role>`
- **View oversight logs**: Check `.egg-state/oversight/` in the pipeline branch
- **Override thresholds**: Set fields on `PipelineConfig` (e.g., `orchestrator_heartbeat_timeout_seconds`, `orchestrator_implement_heartbeat_timeout_seconds`, `overseer_max_redirects_before_escalation`)

### Phase Gate Troubleshooting

**"No draft was found on the work branch"**: The phase gate could not locate the draft file in the worktree. The orchestrator checks two paths in order: the issue-specific path (e.g., `.egg-state/drafts/1553-analysis.md`) then the generic fallback (e.g., `.egg-state/drafts/analysis.md`). If neither exists, this warning is displayed. Common causes:
- The agent failed before writing the draft
- The worktree sync didn't bring the file into the local worktree (fetch failure, worktree on wrong branch)
- The file was written to a different path than expected

Use `git show origin/<branch>:.egg-state/drafts/` to list draft files on the remote branch and verify the expected file exists.

**"No materialised worktree found for phase gate persistence; falling back to main repo path. Contract write may silently no-op."**: The orchestrator entered this warning path during `AWAITING_HUMAN` recovery — the human resolved the phase gate but the pipeline's worktree could not be located. Common causes:
- The worktree was cleaned up between the pipeline entering `AWAITING_HUMAN` and the human resolving the decision (cleanup race)
- The orchestrator restarted between those two events and the worktree directory was not preserved

**Impact**: Phase gate context/feedback (e.g., "Use adapter pattern") may not reach the next phase's agents — `_persist_phase_gate_resolution()` writes to `.egg-state/` under the worktree, and without a worktree the write targets the orchestrator's main repo. The contract write typically no-ops there because the contract file is absent; it could in principle land against the orchestrator's tree if a same-id contract happens to exist, but that's against the wrong tree and still won't reach the pipeline branch. The push is also skipped.

**Recovery**: If the context is important, add the resolution manually to the contract before the next phase starts. This applies to phase gates that fire *after* the plan phase has populated contract tasks (e.g., implement→test, test→pr):
```bash
egg-contract update-notes --task <id> --notes "[Phase gate: <phase>] <resolution context>"
```

Note: `update-notes` writes to a task's `notes` field, not `contract.decisions`, so it's a workaround rather than a true mirror of `_persist_phase_gate_resolution()` — there is no CLI command that adds a *resolved* decision (`egg-contract add-decision` only creates unresolved HITL decisions). Next-phase agents will see the context in task notes but not as a structured resolved decision.

For pre-plan phase gates (refine→plan), there are no contract tasks yet to attach notes to. In that case, prepend the resolution context directly to the next phase's draft once it materialises (e.g., `.egg-state/drafts/<issue>-plan.md`) so the planner picks it up.

### PR-Phase State File Troubleshooting

The PR phase runs three operations before creating the PR: agent-outputs cleanup, BRC history re-write, and a final push. Each operation has diagnostic INFO-level logging to help identify failures.

**BRC history files missing from PR** (`.egg-state/brc-history/` absent):

Look for these log entries in chronological order:

1. `_rewrite_brc_history_for_pr: entering` — Confirms the function was called. Includes `total_phases`, `completed_phase_count`, and `completed_phases` list. If this log is missing, the PR-phase handler did not reach the call site (check for exceptions earlier in `_run_pipeline`).
2. `_write_brc_history: entering` — One per completed phase. Shows `pipeline_id`, `phase`, and `identifier`. If missing for a specific phase, that phase was skipped or errored.
3. Early-return paths (one of):
   - `_write_brc_history: early return — message store unavailable` — The message store factory returned `None`.
   - `_write_brc_history: early return — failed to retrieve messages` — Exception calling `store.get_messages()`. Includes `error` detail.
   - `_write_brc_history: early return — no messages in store` — Store returned an empty list.
   - `_write_brc_history: early return — no BRC messages for phase` — Messages exist but none match `BRC_HISTORY_TYPES` (the `CONSENSUS_*` types plus `STATUS`, `HANDOFF`, `AGENT_FAILED`, `NUDGE`, `OVERSEER_ALERT`, `HEARTBEAT`) for the specified phase. Includes `total_messages` count. `QUESTION` was dropped from this set in [#1897](https://github.com/jwbron/egg/issues/1897).
4. `Wrote BRC history file` — The history file was written to disk. Includes `path` and `message_count`. If this log is missing after step 2, an early-return was taken (check step 3).
5. `_commit_statefiles_to_worktree: glob match results` — Shows `match_count` and `matched_paths` for `.egg-state/` files found by the pipeline-scoped glob. If `match_count` is 0, the BRC history file was not written to disk (check step 4 above).
6. `_commit_statefiles_to_worktree: nothing staged — skipping commit` — The `git diff --cached --quiet` check returned 0, meaning `git add --force` did not stage anything. Possible causes: file permissions, `.gitignore` override, or the file was already committed identically.
   - `_commit_statefiles_to_worktree: staged changes detected — committing` — Changes were staged successfully and a commit is being created.
7. `_commit_statefiles_to_worktree: commit succeeded` — Confirms the commit was created. If this log appears but files are still missing from the PR, the push likely failed (see "Both issues" below).
8. `_rewrite_brc_history_for_pr: commit step completed successfully` / `_rewrite_brc_history_for_pr: exiting` — Confirms the full function completed.

**Draft files present in PR** (`.egg-state/drafts/{id}-*.md`): This is the expected state. Draft files are deliberately preserved on the PR branch as artifacts of the pipeline's reasoning (see #1713). Earlier pipeline versions removed them via `_cleanup_drafts_for_pr()`; that helper has been removed.

**Both issues — state file commits not reaching the PR**:

If the commit logs show success but files are missing/present in the PR diff, the push failed:

1. `PR-phase push succeeded` — Push completed. Includes `commits_ahead` showing how many local commits were ahead of remote before the push.
2. `Push attempt failed — caller may retry via reconcile` (INFO) followed by `Push rejected — attempting fetch+rebase+retry to reconcile divergence` (WARNING) — Initial push was rejected; `GatewayClient` is attempting a fetch+rebase reconcile and a second push automatically.
3. `Push reconcile: rebase succeeded but autostash pop produced conflicts` (ERROR) — The rebase itself succeeded, but the post-rebase autostash pop hit a merge conflict (`reconcile_autostash_pop_conflict`). The autostash entry is preserved in `git stash list` on the orchestrator worktree for manual recovery. The conflicting paths are listed in the log's `conflicting_paths` field.
4. `PR-phase push failed after reconcile — falling back to PR against remote HEAD; orchestrator housekeeping commits dropped` (WARNING) — The reconcile+retry also failed. The PR is still created against the current remote HEAD — agent commits are preserved, but orchestrator housekeeping commits (BRC history rewrite, cleanup) are not included. This is preferable to failing the whole pipeline.
5. `PR-phase push skipped` — The push was not attempted. The `reason` field explains why: `"worktree_repo_path == repo_path"` (no separate worktree to push from) or `"no branch set"` (pipeline has no branch configured).
6. Check the gateway health: `curl http://egg-gateway:9848/api/v1/health`.

**Quick diagnostic checklist**:

```bash
# Check if BRC history files exist on the remote branch
git show origin/egg/issue-<N>:.egg-state/brc-history/ 2>&1

# Check that draft files are present on the remote branch (they should be — see #1713)
git show origin/egg/issue-<N>:.egg-state/drafts/ 2>&1

# Search orchestrator logs for the pipeline's PR-phase activity
# (adjust log source for your deployment)
grep -E "(rewrite_brc_history|commit_statefiles|PR-phase push|Push attempt failed|Push rejected|Push reconcile)" /path/to/orchestrator.log | grep "<pipeline-id>"
```

---

*See also: [The Agentic Feedback Loop](../architecture/agentic-feedback-loop.md), [SDLC Pipeline Architecture](../architecture/sdlc-pipeline.md), [Analysis Template](../templates/analysis.md), [Plan Template](../templates/plan.md), [GitHub Automation](github-automation.md)*
