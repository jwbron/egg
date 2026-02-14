# SDLC Pipeline Architecture

> Structurally enforced agent checkpoints and verification gates for autonomous software development.

This document describes the SDLC (Software Development Lifecycle) pipeline that enables autonomous agents to work on issues while maintaining quality through structural enforcement—not just prompts.

For the conceptual foundation of this pipeline—the feedback loop model that drives quality—see [The Agentic Feedback Loop](../agentic-feedback-loop.md).

For the architectural decision record with threat model and security properties, see [ADR: SDLC Pipeline](../adr/implemented/ADR-SDLC-Pipeline.md).

## Guiding Principles

### 1. Structural Enforcement Over Prompt Compliance

Agents cannot be trusted to self-police via prompts alone. The pipeline enforces constraints at multiple infrastructure layers:

- **Gateway-level operation filtering**: The gateway blocks operations not permitted in the current phase
- **Role-based field ownership**: Contract mutations are validated against caller role
- **Separate context windows**: Each agent invocation runs in a separate GitHub Actions job with fresh context

### 2. Contract-as-Code

All pipeline state is stored in JSON contracts at `.egg-state/contracts/{identifier}.json` and committed to the feature branch (not main), where `{identifier}` is the issue number for issue-mode pipelines or the pipeline ID for local-mode pipelines. This provides:

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

The pipeline pauses for human approval at phase transitions (refine and plan). In issue mode, the `sdlc-hitl.yml` workflow's `handle-approval` job processes checkbox-based approval. In local mode, the orchestrator's decision queue also supports requesting changes, with a circuit breaker (`max_review_cycles`, default 3) to prevent unbounded revision loops.

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           SDLC PIPELINE                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐  │
│  │   REFINE    │───▶│    PLAN     │───▶│  IMPLEMENT  │───▶│ CREATE   │  │
│  │  (cycles)   │    │  (cycles)   │    │  (cycles)   │    │   PR     │  │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘  │
│        │                  │                  │                  │       │
│        ▼                  ▼                  ▼                  ▼       │
│   ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐  │
│   │ REVIEW  │        │ REVIEW  │        │ REVIEW  │        │  HUMAN  │  │
│   │ (auto)  │        │ (auto)  │        │ (auto)  │        │  MERGE  │  │
│   └────┬────┘        └────┬────┘        └─────────┘        └─────────┘  │
│        │                  │                                             │
│        ▼                  ▼                                             │
│   ┌─────────┐        ┌─────────┐                                        │
│   │ HITL    │        │ HITL    │                                        │
│   │ Approve │        │ Approve │                                        │
│   └─────────┘        └─────────┘                                        │
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
- `decision.*`: HITL decision events (created, resolved, timeout)
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
      "dag": ">>> ╔══════════════════════╗\n    │ ▶ Implement          │\n    │   running            │\n    │   ✓ coder  ▶ reviewer│\n    ╚══════════════════════╝",
      "compact": "✓Refine → ✓Plan → [▶Implement] → ○PR",
      "progress": "[███████████░░░░░░░░░] 60%"
    },
    "phases": {
      "refine": {"status": "complete", "review_cycles": 2, "containers": 1, "agents": [{"role": "coder", "status": "complete"}]},
      "plan": {"status": "complete", "review_cycles": 1, "containers": 1, "agents": [{"role": "coder", "status": "complete"}]},
      "implement": {"status": "running", "review_cycles": 0, "containers": 2, "agents": [{"role": "coder", "status": "complete"}, {"role": "reviewer", "status": "running"}]},
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

**Use cases**:
- Monitor pipeline progress from external tools
- Display real-time status in CI dashboards
- Poll for phase completion
- Debug stuck pipelines

### Phases

| Phase | Purpose | Allowed Operations | Exit Requires |
|-------|---------|-------------------|---------------|
| **Refine** | Analyze issue, produce analysis document | `gh issue comment/edit` | Auto-review pass + Human approval |
| **Plan** | Create implementation plan with tasks | `gh issue comment/edit`, `egg-contract add-decision` | Auto-review pass + Human approval |
| **Implement** | Execute tasks on draft PR with CI and review feedback | `git push`, `egg-contract add-commit/update-notes` | All checks pass (CI + PR review) |
| **PR** | Finalize PR for human review and merge | `gh pr edit`, `git push` | Human merge (closes issue automatically) |

### Multi-Reviewer Architecture

The work loop runs multiple specialized reviewers in parallel, with phase-specific defaults:

| Phase | Reviewers | Focus |
|-------|-----------|-------|
| **Refine** | Unified, Agent-Design | Analysis quality, agent-mode alignment |
| **Plan** | Unified, Agent-Design | Plan quality, agent-mode alignment |
| **Implement** | Unified, Agent-Design, Contract, Code | Full coverage: quality, design, contract, security |

**Specialized Reviewers:**

| Reviewer | Script | Focus |
|----------|--------|-------|
| **Unified** | `build-unified-review-prompt.sh` | Phase-specific quality criteria |
| **Agent-Design** | `build-agent-mode-design-review-prompt-workloop.sh` | Agent-mode design alignment (anti-patterns) |
| **Contract** | `build-contract-verification-prompt-workloop.sh` | Task completion, acceptance criteria |
| **Code** | `build-code-review-prompt-workloop.sh` | Security, correctness, robustness |

**Verdict Aggregation:**
- All reviewers run in parallel (matrix job)
- Any `needs_revision` from any reviewer → aggregate `needs_revision`
- Feedback is combined with per-reviewer section headers
- Failed reviewers are tracked separately and trigger escalation

**Per-Reviewer State Tracking:**

The contract tracks per-reviewer verdicts for debugging:
```json
{
  "implement_reviewer_verdicts": {
    "unified": "approved",
    "agent-design": "approved",
    "contract": "needs_revision",
    "code": "approved"
  }
}
```

### Multi-Agent Orchestration

The implement phase supports multi-agent orchestration, where specialized agents (Coder, Tester, Documenter, Integrator) execute in parallel waves based on dependencies. Multi-agent orchestration is enabled by default; single-agent execution can be selected by explicitly disabling it in the contract's `multi_agent_config`.

**Agent Roles:**

| Role | Responsibilities | Depends On | Can Write |
|------|-----------------|------------|-----------|
| **Coder** | Implement code changes based on plan tasks | — | Source code files (`**/*.py`, `**/*.ts`, etc.) |
| **Tester** | Write or update tests for the changes | Coder | Test files (`tests/`, `**/*_test.py`, `**/*.test.ts`, etc.) |
| **Documenter** | Update documentation for the changes | Coder | Documentation files (`docs/`, `**/*.md`) |
| **Integrator** | Run full test suite and validate integration | Coder, Tester | Handoff output only (read-only otherwise) |

**Execution Waves:**
- Wave 1: Coder runs first (no dependencies)
- Wave 2: Tester and Documenter run in parallel (both depend on Coder)
- Wave 3: Integrator runs last (depends on Coder + Tester)

**File Access Enforcement:**
The gateway enforces file access patterns for each agent role via `gateway/agent_restrictions.py`. For example, the Coder agent cannot modify documentation files, and the Tester agent cannot modify source code. This prevents agents from overstepping their responsibilities.

**Handoff Data:**
Agents communicate via handoff data stored in `.egg-state/agent-outputs/{role}-output.json`. For example, the Coder agent outputs a list of changed files, which the Tester and Documenter agents read to focus their work.

**Workflow:**
Multi-agent orchestration is triggered via `.github/workflows/sdlc-multi-agent.yml`. The workflow reads the contract state, determines which agents can run based on dependencies, and dispatches them in parallel where possible.

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
    ├── {identifier}-refine-review.json       # Unified review verdict
    ├── {identifier}-refine-agent-design.json # Agent-design review verdict
    ├── {identifier}-plan-review.json         # Unified review verdict
    ├── {identifier}-plan-agent-design.json   # Agent-design review verdict
    ├── {identifier}-implement-review.json    # Unified review verdict
    ├── {identifier}-implement-agent-design.json
    ├── {identifier}-implement-contract.json
    └── {identifier}-implement-code.json
```

**Review Verdict JSON Schema:**
```json
{
  "reviewer": "unified" | "agent-design" | "contract" | "code",
  "verdict": "approved" | "needs_revision",
  "summary": "Brief summary of findings",
  "feedback": "Detailed feedback (empty if approved)",
  "timestamp": "ISO 8601 timestamp"
}
```

**Review Criteria for Refine:**
- Does the analysis address the issue description?
- Are options meaningfully different and well-reasoned?
- Are constraints and dependencies identified?
- Are open questions specific enough for a human to answer?
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
- **Refine/Plan phases**: Cannot `git push` or `gh pr create`—prevents code changes before plan approval
- **Implement phase**: Can `git push` to the branch; draft PR is created automatically by the pipeline (not by agent)
- **PR phase**: Can `gh pr create/edit` and `git push`; human must merge

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

The SDLC pipeline uses `.github/scripts/push-contract-update.sh` to handle concurrent contract updates without merge conflicts. When multiple workflow jobs modify the same contract file simultaneously, traditional git rebase can fail with conflicts.

The script implements a "reset-and-reapply" pattern:
1. When a push fails, it aborts any failed rebase
2. Resets to the remote HEAD (discarding the conflicted local commit)
3. Re-applies the jq transformation from scratch on the fresh remote state
4. Creates a new commit and retries the push

This approach is idempotent — the jq transformation is applied to whatever the current remote state is, rather than trying to merge conflicting commits. The script accepts either a simple jq filter or a path to a script that performs complex multi-step transformations.

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

This prevents context pollution and ensures reproducible behavior. When the implementer is re-invoked after review feedback, it receives the PR review comments as part of its prompt context.

## Multi-Agent Orchestration

The implement phase can use multi-agent orchestration to parallelize work across specialized agents. This reduces context window pollution and improves first-pass implementation quality.

### Agent Roles

| Role | Purpose | Dependencies | File Access |
|------|---------|--------------|-------------|
| **Coder** | Implements code changes | None | `src/`, `lib/`, `shared/` |
| **Tester** | Creates and runs tests | Coder | `tests/`, `test_*.py`, `*.test.ts` |
| **Documenter** | Updates documentation | Coder | `docs/`, `*.md`, `README*` |
| **Integrator** | Final validation and integration | Coder, Tester | Read-only except `.egg-state/` |

### Execution Waves

Agents execute in waves based on dependencies:

```
Wave 1:  [Coder]           ─── Must complete first
Wave 2:  [Tester, Documenter] ─── Run in parallel
Wave 3:  [Integrator]      ─── Final validation
```

### Enabling Multi-Agent Mode

Multi-agent mode is **enabled by default**. When `multi_agent_config` is absent from the contract, or when `multi_agent_config.enabled` is not specified, the system defaults to multi-agent orchestration.

To explicitly configure multi-agent mode, use the contract's `multi_agent_config`:

```json
{
  "multi_agent_config": {
    "enabled": true,
    "roles_enabled": ["coder", "tester", "documenter", "integrator"],
    "parallel_execution": true
  }
}
```

To disable multi-agent mode and use single-agent execution:

```json
{
  "multi_agent_config": {
    "enabled": false
  }
}
```

### Agent Handoffs

Each agent produces handoff data stored in `.egg-state/agent-outputs/{role}-output.json`:

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
| `.github/workflows/sdlc-multi-agent.yml` | Multi-agent orchestration workflow |
| `shared/egg_contracts/agent_roles.py` | Agent role definitions and file access |
| `shared/egg_contracts/orchestration.py` | Orchestration state management |
| `shared/egg_contracts/dependency_graph.py` | Dependency graph and wave computation |
| `shared/egg_contracts/orchestrator.py` | Dispatch logic and handoff management |
| `action/build-{role}-prompt.sh` | Role-specific prompt builders |
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

The `sdlc-hitl.yml` workflow:

1. Triggers on `issue_comment.edited`
2. Parses checkbox state using `hitl.py`
3. Validates debounce period
4. Updates contract with resolution
5. Resumes pipeline from paused state

## External Failure Handling

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

**PR Metadata**: The plan should include a `pr:` section in the YAML appendix with a title and description for the pull request that will be created. The pipeline uses this metadata when creating and finalizing the PR. If not provided, the PR title defaults to the issue title, and the PR description is built from commit messages.

### Phase Completion Comments

When a phase is complete and ready for human approval, agents post a comment using the [Phase Completion Template](../templates/phase-completion.md). This format includes the `<!-- egg-phase-approval -->` marker which the HITL workflow uses to detect approval checkbox changes.

### Task Population

Tasks are automatically extracted from the plan document and populated into the contract during the plan phase, after the plan document is validated.

The `action/populate-contract-tasks.py` script (issue-mode only):
1. Fetches the plan comment from the GitHub issue
2. Parses task markers and PR metadata using `shared/egg_contracts/plan_parser.py`
3. Writes phases, tasks, and PR metadata into `.egg-state/contracts/{issue-number}.json`
4. Validates the contract against the JSON schema
5. Commits the updated contract to the feature branch

This happens in the plan phase itself (before human approval) to provide early validation of the plan format. The implement phase also runs task population as a fallback in case the plan phase step failed or was skipped.

The PR metadata (title and description) from the plan is stored in the contract's `pr` field and used when creating the draft PR during the implement phase.

## Phase Checks

Each SDLC phase can run automated checks before completion. The check system provides a framework for validating phase outputs and code quality.

### Check Framework

Check scripts inherit from `CheckRunner` base class (`.github/scripts/checks/base.py`) and implement a `run()` method that returns a `CheckResult`:

```python
from .base import CheckResult, CheckRunner, CheckStatus

class MyCheck(CheckRunner):
    @property
    def check_id(self) -> str:
        return "check-my-check"

    def run(self) -> CheckResult:
        # Validation logic here
        return self.create_result(
            status=CheckStatus.PASS,
            message="Check passed",
        )
```

Check results have three statuses:
- `PASS`: Check succeeded
- `FAIL`: Check failed (may be fixable)
- `SKIP`: Check skipped (e.g., no test infrastructure found)

### Running Checks

Checks are executed via `run_check.py`:

```bash
python .github/scripts/checks/run_check.py lint .egg-state/contracts/123.json --repo-root .
```

The script loads the contract, runs the check, and outputs JSON with the result.

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

When configured, the implement phase checker agent runs these commands sequentially instead of auto-discovering test/lint commands. This is useful when:
- Auto-discovery doesn't find the right commands
- You want to run checks in a specific order
- You need to run custom validation scripts

If not configured, the checker falls back to auto-discovery (scanning for Makefile, package.json, pyproject.toml, etc.). See [Configuration](../../config/README.md#per-repo-check-commands) for setup details.

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

To add a new check:

1. Create a Python file in `.github/scripts/checks/` that inherits from `CheckRunner`
2. Implement the `check_id` property and `run()` method
3. Register the check in `CHECK_REGISTRY` in `run_check.py`
4. Add the check to phase defaults or contract-specific `phase_configs`

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

### Autofix Retry and Re-Validation

When lint, test, or integration checks fail, the pipeline triggers the autofixer agent to attempt repairs. The autofix process now includes synchronous waiting and re-validation:

**Flow:**
```
work → lint/test (parallel) → fail → autofixer → re-run checks → pass → review
                                                               → fail → autofixer (retry)
                                                               → fail (max retries) → escalate
```

**Key behaviors:**

1. **Synchronous wait**: The check-fixer job waits for the autofix workflow to complete (30-minute timeout) instead of fire-and-forget
2. **Re-validation**: After autofix completes, the work loop re-dispatches itself in `checks-only` mode to re-run lint/test/integration
3. **Circuit breaker**: Maximum 3 autofix attempts per implement phase cycle (configurable via `max_autofix_attempts` in contract)
4. **Escalation**: When max attempts exceeded, pipeline posts an escalation comment and pauses for human intervention

**Contract fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `autofix_attempts` | 0 | Current autofix attempt count in this phase cycle |
| `max_autofix_attempts` | 3 | Maximum attempts before circuit breaker triggers |

**Work loop modes:**

| Mode | Description |
|------|-------------|
| `full` (default) | Execute work, then run checks and review |
| `checks-only` | Skip work, only run checks and review (used after autofix) |

The `autofix_attempts` counter resets to 0 when:
- A new implement phase starts
- The work loop is manually re-triggered via `workflow_dispatch`

## Implementation Reference

### Key Files

| File | Purpose |
|------|---------|
| `.github/workflows/sdlc-pipeline.yml` | Main pipeline orchestration |
| `.github/workflows/sdlc-multi-agent.yml` | Multi-agent orchestration workflow |
| `.github/workflows/sdlc-work-loop.yml` | Unified work/review/respond cycle for refine, plan, and implement phases |
| `.github/workflows/reusable-review.yml` | PR-based code review workflow |
| `.github/workflows/sdlc-hitl.yml` | HITL checkbox detection |
| `.github/scripts/checks/base.py` | CheckRunner base class for phase checks |
| `.github/scripts/checks/run_check.py` | Check execution entry point |
| `.github/scripts/checks/*.py` | Built-in check implementations |
| `.github/scripts/push-contract-update.sh` | Conflict-resistant contract push utility |
| `action/build-sdlc-prompt.sh` | Phase-specific prompt builder (includes review feedback injection) |
| `action/build-coder-prompt.sh` | Coder agent prompt builder |
| `action/build-tester-prompt.sh` | Tester agent prompt builder |
| `action/build-documenter-prompt.sh` | Documenter agent prompt builder |
| `action/build-integrator-prompt.sh` | Integrator agent prompt builder |
| `action/build-unified-review-prompt.sh` | Unified review prompt builder for all SDLC phases |
| `action/build-agent-mode-design-review-prompt-workloop.sh` | Agent-mode design review for work loop |
| `action/build-contract-verification-prompt-workloop.sh` | Contract verification review for work loop |
| `action/build-code-review-prompt-workloop.sh` | Code review for work loop |
| `action/populate-contract-tasks.py` | Extracts tasks from plan into contract |
| `action/contract-state.sh` | Contract state management utility |
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

## SDLC Labels

The pipeline uses GitHub labels to track issue state and enable filtering:

### Phase Labels

| Label | Description | Applied When |
|-------|-------------|--------------|
| `sdlc:refine` | Issue is in refine phase | Pipeline initialized or workflow triggered |
| `sdlc:plan` | Issue is in plan phase | Refine phase approved |
| `sdlc:implement` | Issue is in implement phase | Plan phase approved |
| `sdlc:pr` | Issue has a PR in review | Draft PR created |

### Status Labels

| Label | Description | Applied When |
|-------|-------------|--------------|
| `sdlc:awaiting-approval` | Human approval required | Phase completion or escalation |

### Label Lifecycle

1. **Pipeline start**: `sdlc:refine` is added (and triggers the pipeline)
2. **Phase transitions**: Old phase label removed, new phase label added
3. **Awaiting approval**: `sdlc:awaiting-approval` added when human review needed
4. **On approval**: `sdlc:awaiting-approval` removed, phase label transitioned
5. **Issue closed**: All SDLC labels automatically removed by cleanup workflow

### Label Setup

To set up SDLC labels in a repository:

```bash
.github/scripts/setup-sdlc-labels.sh --repo owner/repo
```

This script is idempotent and safe to run multiple times.

### Triggering the Pipeline

**Via label** (recommended):
```bash
gh issue edit 123 --add-label "sdlc:refine"
```

**Via workflow dispatch**:
```bash
gh workflow run sdlc-pipeline.yml -f issue_number=123 -f starting_phase=refine
```

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

*See also: [The Agentic Feedback Loop](../agentic-feedback-loop.md), [ADR: SDLC Pipeline](../adr/implemented/ADR-SDLC-Pipeline.md), [Analysis Template](../templates/analysis.md), [Plan Template](../templates/plan.md), [GitHub Automation](github-automation.md)*
