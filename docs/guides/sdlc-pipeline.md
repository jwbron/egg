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
- **Post-agent auto-commit**: Uncommitted work is automatically preserved when agent containers exit, with phase-restricted files restored (not committed) using `check_phase_file_restrictions()` and allowed files pushed via the gateway API
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

> **Note**: The architecture below describes the standard **issue mode** pipeline. For the **babysit mode** (PR review/fix loop), see the [Babysit-PR Guide](babysit-pr.md).

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
| **Implement** | Execute tasks on draft PR with CI and review feedback | `git push`, `egg-contract add-commit/update-notes` | All checks pass (CI + PR review) |
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

The implement phase uses concurrent BRC execution, where specialized agents run simultaneously and coordinate via the message bus.

**Agent Roles:**

| Role | Responsibilities | Can Write |
|------|-----------------|-----------|
| **Coder** | Implement code changes based on plan tasks | Source code files (`**/*.py`, `**/*.ts`, etc.) |
| **Tester** | Find gaps in implementation, write tests, run linters and report issues | Test files (`tests/`, `**/*_test.py`, `**/*.test.ts`, etc.) and pytest infrastructure (`**/conftest.py`) |
| **Documenter** | Update documentation for the changes | Documentation files (`docs/`, `**/*.md`) |
| **Reviewer (Code)** | Review code for security, correctness, robustness | Review verdicts only |
| **Reviewer (Contract)** | Verify task completion and acceptance criteria | Review verdicts only |

**File Access Enforcement:**
The gateway enforces file access patterns for each agent role via `gateway/agent_restrictions.py`. For example, the Coder agent cannot modify documentation files, and the Tester agent cannot modify source code. This prevents agents from overstepping their responsibilities.

**Handoff Data:**
Agents communicate via handoff data stored in `.egg-state/agent-outputs/{identifier}-{role}-output.json` (where `{identifier}` is the issue number or pipeline ID). For example, the Coder agent outputs a list of changed files, which the Tester and Documenter agents read to focus their work. The identifier prefix prevents merge conflicts when concurrent pipelines merge to main.

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
│   ├── {identifier}-analysis.md     # Refine phase draft
│   └── {identifier}-plan.md         # Plan phase draft
└── reviews/
    ├── {identifier}-refine-refine-review.json        # Refine review verdict
    ├── {identifier}-refine-agent-design-review.json   # Agent-design review verdict
    ├── {identifier}-plan-plan-review.json             # Plan review verdict
    ├── {identifier}-implement-code-review.json        # Code review verdict
    └── {identifier}-implement-contract-review.json    # Contract review verdict
```

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
| `.egg-state/drafts/` | Draft analysis and plan documents | Feature branches only |
| `.egg-state/reviews/` | Internal review verdicts (JSON) | Feature branches only |

### Conflict-Resistant Contract Updates

The local orchestrator handles concurrent contract updates through `orchestrator/state_store.py`, which uses git-backed state management. When multiple agents modify the same contract file simultaneously, the state store handles conflict resolution automatically through its commit-based approach.

### Contract Schema

```json
{
  "schemaVersion": "1.0",
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
  "workflow_owner": "jwbron",
  "audit_log": []
}
```

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

This approach follows a "focus, don't starve" philosophy: agents get enough context to make good decisions without being distracted by irrelevant detail. Full context is always accessible on demand via CLI commands and file paths.

The context is built by `_build_role_context()` in `orchestrator/routes/pipelines.py`, which replaces the previous pattern of embedding `pipeline.prompt` verbatim into every agent prompt.

## Multi-Agent Orchestration

The refine, plan, and implement phases use concurrent BRC execution to parallelize work across specialized agents. All agents within a phase run simultaneously, communicating via the orchestrator message bus and reaching consensus through the BRC protocol. Additional phases can be enabled for concurrent execution via the `concurrent_phases` config. This reduces context window pollution and improves first-pass implementation quality.

Agents are organized into five categories (execution, analysis, review, utility, interface) with role definitions consolidated in `shared/egg_contracts/agent_roles.py`. See the [Agent Roles Reference](../reference/agent-roles.md) for the complete roster.

### Agent Roles

| Role | Category | Purpose | File Access |
|------|----------|---------|-------------|
| **Coder** | Execution | Implements code changes | `src/`, `lib/`, `shared/` |
| **Tester** | Execution | Finds gaps, writes tests, runs linters and reports issues to coder | `tests/`, `test_*.py`, `*.test.ts`, `**/conftest.py` |
| **Documenter** | Execution | Updates documentation | `docs/`, `*.md`, `README*` |
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

**What gets synced:**

- Resolved decisions with `decision_type != "phase_gate"` (substantive choices, not process gates) — via `_sync_pipeline_decisions_to_contract()`
- Phase gate approvals that include context or feedback — via `_persist_phase_gate_resolution()`. When a human approves a phase gate with notes, the context is added to the contract as a `[Phase gate: <phase>]`-prefixed decision and appended to the phase draft file as a `## HITL Resolution` section
- Decision question, options, resolution, and resolved_at are carried over; resolved_by is set to `"human"`
- Decisions already present in the contract (matched by question text) are skipped to avoid duplicates

**Key files:**

- `orchestrator/routes/pipelines.py` — `_sync_pipeline_decisions_to_contract()` and `_persist_phase_gate_resolution()` implementations
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
2. **After phase completion** — Pushes statefiles (drafts, reviews, check results, contract updates) so the next phase's agents don't have unpushed `.egg-state/` files in their diff
3. **On pipeline failure** — Best-effort failsafe push to preserve in-progress work

All pushes use `GatewayClient.push_worktree_branch()`, which registers a temporary session token, pushes the branch, and cleans up the session. Failures are logged as warnings and don't block pipeline progress.

**Implementation**: See `orchestrator/routes/pipelines.py:_run_pipeline()` and `orchestrator/gateway_client.py:push_worktree_branch()` for the push logic.

## Failure Handling

### Pipeline Failure Recovery

When a pipeline phase fails (container exit code non-zero), the orchestrator:

1. **Sets pipeline status to FAILED**: Marks the phase and pipeline as failed during phase execution
2. **Emits failure event**: Sends `pipeline.failed` event to terminate SSE streams
3. **Best-effort push**: Attempts to push the worktree branch to remote as a backup (using a temporary session token)
4. **Preserves worktree**: Skips cleanup in the `finally` block so in-progress work is not lost

**Restart behavior**:
- `egg-sdlc` CLI detects failed pipelines and automatically restarts from the failed phase (preserving worktrees)
- Orchestrator API `POST /api/v1/pipelines/{id}/start` resets the failed phase to pending and resumes execution
- Worktrees remain intact across restarts, so agents can continue from their last commit

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

**PR Metadata**: The plan should include a `pr:` section in the YAML appendix with a title, description, test plan, and manual steps for the pull request. The `test_plan` field is required — describe both automated test coverage and manual verification steps. The `manual_steps` field lists any pre- or post-merge actions (migrations, config changes, deployments); use an empty string if none. The pipeline uses this metadata when creating and finalizing the PR. If not provided, the PR title defaults to the issue title, and the PR description is built from commit messages.

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

This happens in the plan phase itself (before human approval) to provide early validation of the plan format. The implement phase also runs task population as a fallback in case the plan phase step failed or was skipped.

The PR metadata (title and description) from the plan is stored in the contract's `pr` field and used by the orchestrator to auto-create the PR when the implement phase completes. The orchestrator builds the PR body from the contract's `pr` metadata, the git commit log, diff stats, and a Pipeline Context section (pipeline ID and issue number). The gateway injects a machine-parseable `<!-- egg-pipeline-context ... -->` HTML comment and applies `egg` and `agent:orchestrator` labels to the PR — no agent is spawned for PR creation.

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

### Built-in Checks

| Check | ID | Purpose | Fixable |
|-------|-----|---------|---------|
| **Draft Validation** | `check-draft-validation` | Validates refine phase analysis document | No |
| **Plan YAML** | `check-plan-yaml` | Validates plan phase YAML appendix | No |
| **Merge Conflict** | `check-merge-conflict` | Detects conflicts with base branch | No |
| **Lint** | `check-lint` | Runs `make lint` if available | Yes |
| **Test** | `check-test` | Runs `make test` or pytest | No |
| **Deployment Validation** | `check-deployment` | Validates changes against locally running devserver (opt-in via `.egg/deployment.yml`) | No |
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
- Deployment validation (optional, 1 retry, requires `.egg/deployment.yml`)

**PR phase:**
- No checks
- PR is auto-created by the orchestrator (no agent spawned). The PR title and description are sourced from the contract's `pr` field (populated by the plan agent), with commit log and diff stats appended automatically.
- If PR creation returns no URL, the pipeline is marked **FAILED** immediately. The overseer also runs a safety-net check at pipeline completion: if `current_phase=pr` but no `pr_url` is in the phase artifacts, it creates a HITL decision and Slack notification to prevent stranded branch work from going unnoticed.

### Deployment Validation

The deployment validation check (`check-deployment`) runs agent-modified code against a locally running devserver to catch integration issues before merge. This check is **opt-in** and requires target repositories to provide a `.egg/deployment.yml` configuration file.

**How it works:**

1. The orchestrator extracts the `docker-compose.yml` from the committed state (before agent changes)
2. Generates override mounts for agent-modified services based on service-to-source mappings
3. Starts the devserver stack in an isolated Docker network with resource limits
4. The sandbox check runner polls health endpoints and runs validation tests
5. The orchestrator tears down the stack after validation completes

**Configuration (`.egg/deployment.yml`):**

```yaml
compose_file: "docker-compose.yml"  # Path relative to repo root
services:
  - source_dir: "services/api"      # Source directory (agent changes)
    service_name: "api"              # docker-compose service name
    container_mount_path: "/app"    # Mount path inside container
health_endpoints:
  api: "/health"                    # Service name → health check path
validation_tests:
  - service: "api"                  # Target service name
    path: "/users"                  # Request path
    method: "GET"
    expected_status: 200
    description: "API smoke test"
```

**Security guarantees:**

- Devserver containers run in an isolated Docker network (no internet, no access to other containers)
- Resource limits prevent exhaustion attacks (CPU, memory, PIDs)
- Hard timeout of 5 minutes for the entire devserver lifecycle
- No cloud credentials or production secrets are injected
- Suspicious environment variables (AWS_*, GCP_*, AZURE_*, GOOGLE_CLOUD_*, *_SECRET_KEY, *_API_KEY, *_ACCESS_KEY, *_TOKEN, *_PASSWORD, *_CREDENTIALS) are rejected

**When to use:**

- Microservices with docker-compose devserver setups
- Integration testing that requires multiple services running
- Validating API contracts between services

**When not to use:**

- Projects without docker-compose devserver infrastructure
- Simple single-service applications (use `make test` instead)
- Projects where devserver setup is complex or requires external dependencies

The check is optional by default and will skip if `.egg/deployment.yml` is not present. When enabled, it runs with 1 retry on failure.

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
- Works both inside containers and from the host (via `egg --exec`)

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

**Qualifier support**: The `submit_task` MCP tool accepts an optional `"qualifier"` suffix for both issue-driven and JIRA-driven pipelines (e.g. `"qualifier": "backend"` produces pipeline ID `issue-123-backend` / branch `egg/issue-123-backend`). When using the REST API directly, append the qualifier to `pipeline_id` and `branch` manually (e.g. `"pipeline_id": "KORE-1234-backend"`, `"branch": "egg/KORE-1234-backend"`). If the target branch already exists, the orchestrator returns HTTP 409 with a hint to use a qualifier.

Pipeline ID formats:
- `issue-{number}[-qualifier]` — GitHub issue-driven
- `{TICKET}[-qualifier]` — JIRA ticket-driven (e.g. `KORE-1234`, `KORE-1234-backend`)
- `pr-{number}` — babysit mode
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

### Contract CLI Commands

```bash
# View contract state
egg-contract show

# Link commit to task (implementer)
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
reviewer_contract) to run simultaneously during the implement phase,
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
    "consensus_timeout_minutes": 30,
    "agent_idle_timeout_minutes": 60
  }
}
```

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
| `consensus_timeout_minutes` | int | `30` | Timeout before HITL escalation |
| `agent_idle_timeout_minutes` | int | `60` | Agent idle timeout |
| `overseer_enabled` | bool | `true` | Enable the overseer agent for pipeline health monitoring |

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
│  message_type: "PROGRESS" | "QUESTION" | "STATUS"    │
│  subject: "API endpoints complete"                   │
│  body: "Implemented GET/POST/DELETE for /api/users"  │
│  timestamp: "2026-03-11T10:30:00Z"                   │
└─────────────────────────────────────────────────────┘
```

**Message types**:

| Type | Purpose | Example |
|------|---------|---------|
| `PROGRESS` | Notify about completed work | Coder: "API endpoints committed" |
| `QUESTION` | Ask another agent for clarification | Tester: "Expected status for invalid input?" |
| `RESPONSE` | Reply to a question | Coder: "400 Bad Request" |
| `STATUS` | Share current activity | Documenter: "Documenting API section" |
| `AGENT_FAILED` | System notification of failure | System: "Tester agent crashed" |

**CLI commands**:

```bash
# Send a message to another agent
egg-orch message send --to tester --type PROGRESS --subject "API done" --body "..."

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
5. Timeout (`consensus_timeout_minutes`, default 30) triggers HITL escalation
   - Options: **Continue waiting**, **Accept current state**, **Abort phase**
   - Phase falls back to exit-code-based completion while awaiting the decision
6. If a container exits cleanly without signaling `READY`, the consensus wrapper restarts it with a recovery prompt (up to `MAX_CONSENSUS_RESTARTS`, default 2). After exhausting restarts the wrapper exits with code 1, triggering the single-agent failure path (HITL decision: retry, abort, or continue without). See [Concurrent Execution: Consensus Wrapper](concurrent-execution.md#consensus-wrapper).

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
committed. Responds to `QUESTION` messages from tester/documenter. Signals `READY`
after all implementation tasks are committed.

**Tester**: Begins scaffolding tests early. Polls for coder `PROGRESS` to know when
code is ready. Sends `QUESTION` messages for clarification. Signals `READY` after
tests pass.

**Documenter**: Starts documentation based on the plan. Refines as implementation
solidifies. Polls for `PROGRESS` from coder/tester. Signals `READY` after docs cover
all changes.

**Reviewer (code/contract)**: Reviews committed code or contract artifacts. Polls for
`PROGRESS` from coder. Signals `READY` after review is complete.

### Shared Pipeline Branch

All concurrent agents operate on the pipeline's shared branch (e.g., `egg/issue-999`).
Rather than each agent having an isolated worktree branch, all agents commit directly
to a single shared history. Agents coordinate via the message bus to sequence commits
and avoid conflicts — for example, the coder signals `HANDOFF` when its changes are
committed so downstream agents (tester, documenter) know it is safe to pull and build
on top.

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
      "by_type": {"PROGRESS": 5, "QUESTION": 3, "RESPONSE": 3, "STATUS": 1}
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

**Consensus timeout**: If agents don't reach consensus within `consensus_timeout_minutes`,
a HITL decision is created. Check agent states via `egg-orch pipeline status` to
identify blocked or stuck agents.

**Message bus empty**: Verify the pipeline has `concurrent_execution: true` in its
config. The message bus is only active for concurrent pipelines.

**Commit conflicts**: Since all concurrent agents share a single branch, agents
coordinate commits via the message bus to avoid conflicts. If an agent encounters a
conflict when pushing, it should pull, rebase, and retry. If conflicts persist, the
agent signals `BLOCKED` and a HITL decision is created. Consider adding role-based
file restrictions to minimize overlap.

## Pipeline Health Monitoring

Pipeline health monitoring uses a **two-tier architecture** to detect and remediate agent stalls, errors, and anomalies without human intervention when possible.

### Tier 1: Orchestrator Deterministic Rules

The orchestrator runs five deterministic tripwire checks against live telemetry:

- **Heartbeat timeout**: No heartbeat/progress within `orchestrator_heartbeat_timeout_seconds` (default 120s) triggers an auto-nudge message to the agent.
- **Container exit**: Unexpected container death triggers immediate HITL escalation.
- **Repeated errors**: Identical error repeated `orchestrator_error_repeat_threshold` times (default 3) escalates.
- **Message volume spike**: Messages exceeding `orchestrator_message_rate_limit` per minute (default 20) triggers auto-throttle.
- **Progress stall**: No structured progress events within the threshold triggers a nudge, then escalation if unresolved.

Agents emit structured progress via `egg-orch progress emit --step <text> --state <working|blocked|complete>`. Query progress with `egg-orch progress query`.

### Tier 2: LLM Overseer

When Tier 1 escalates an anomaly (and `overseer_enabled` is true in PipelineConfig), the overseer uses a two-model approach:

1. **Haiku classifier** — fast, cheap classification: `stuck`, `legitimate_work`, `recoverable`, `fatal`, `loop_detected`, `off_track`
2. **Sonnet/Opus decision maker** — deeper analysis producing actions: `redirect` (send corrective message), `file_issue` (create GitHub issue with `overseer-alert` label), `escalate_hitl` (create HITL decision), or `no_action`

### Escalation Ladder

auto-nudge → redirect → HITL → issue → Slack. Each step is tried before escalating further. `overseer_max_redirects_before_escalation` (default 2) controls how many redirect attempts before HITL.

### Troubleshooting

- **Query health alerts**: `egg-orch health alerts`
- **Query progress events**: `egg-orch progress query --agent <role>`
- **View oversight logs**: Check `.egg-state/oversight/` in the pipeline branch
- **Override thresholds**: Set fields on `PipelineConfig` (e.g., `orchestrator_heartbeat_timeout_seconds`, `overseer_max_redirects_before_escalation`)

---

*See also: [The Agentic Feedback Loop](../architecture/agentic-feedback-loop.md), [SDLC Pipeline Architecture](../architecture/sdlc-pipeline.md), [Analysis Template](../templates/analysis.md), [Plan Template](../templates/plan.md), [GitHub Automation](github-automation.md)*
