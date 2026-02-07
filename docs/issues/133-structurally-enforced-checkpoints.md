# Issue #133: Structurally Enforced Agent Checkpoints and Verification Gates

> **Note**: This document contains the full specification for issue #133. The issue description references this file because GitHub issue body length limits prevented inline content.

---

## Part 1: Multi-Stage Pipeline Architecture

The SDLC pipeline consists of multiple stages, each with a worker → reviewer pattern:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           SDLC PIPELINE                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐ │
│  │   REFINE    │───▶│    PLAN     │───▶│  IMPLEMENT  │───▶│ CREATE   │ │
│  │   ISSUE     │    │             │    │  (per phase)│    │   PR     │ │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘ │
│        │                  │                  │                  │       │
│        ▼                  ▼                  ▼                  ▼       │
│   ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐ │
│   │ REVIEW  │        │ REVIEW  │        │ REVIEW  │        │  HUMAN  │ │
│   │ (auto)  │        │ (HITL?) │        │ (auto)  │        │  MERGE  │ │
│   └─────────┘        └─────────┘        └─────────┘        └─────────┘ │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key Principles

1. **Structural Enforcement**: Agents cannot be trusted to self-police via prompts. Role-based restrictions are enforced at the CLI and gateway level.
2. **Phase-Based Operation Filtering**: Each pipeline phase permits only specific operations. The gateway blocks operations not allowed in the current phase.
3. **Separate Context Windows**: Each agent invocation (worker, reviewer) runs in a separate GitHub Actions job with fresh context.
4. **Contract-as-Code**: All state is stored in `.egg/contracts/{issue-number}.json` and committed to the branch.
5. **Human-in-the-Loop**: Critical decisions pause the pipeline for human input via GitHub checkboxes.

### Phase-Based Operation Restrictions

Each phase has a defined set of permitted operations. The gateway blocks all other operations:

| Phase | Permitted Operations | Blocked Operations | Exit Requires |
|-------|---------------------|-------------------|---------------|
| **Refine** | `gh issue comment/edit` | `git push`, `gh pr create` | Human approval |
| **Plan** | `gh issue comment/edit`, `egg-contract add-decision` | `git push`, `gh pr create` | Human approval |
| **Implement** | `git push`, `egg-contract add-commit/mark-task` | `gh pr create` | Reviewer approval |
| **PR** | `gh pr create/edit`, `git push` | — | Human merge |

This prevents the [#202 incident](https://github.com/jwbron/egg/issues/202) by making it technically impossible to push code during the planning phase.

---

## Part 2: Document Standards for Pipeline Phases

The pipeline produces structured documents at each phase gate. These documents are what humans review, and the contract JSON tracks their status.

### Analysis Document (Refine Phase Output)

The refine phase produces an analysis committed to `docs/issues/{number}-analysis.md`.

```markdown
# Issue #{number}: Analysis

## Problem Statement
What is broken or missing, stated from the user's perspective.

## Current Behavior
How the system works today (or doesn't). Include relevant code paths.

## Constraints
- Technical constraints (compatibility, performance, existing patterns)
- Policy constraints (security model, gateway enforcement, etc.)
- Scope constraints (what's explicitly out of scope)

## Options Considered
For each option:
- Description
- Pros/cons
- Risk assessment

## Recommended Approach
Which option and why. This becomes the input to the plan phase.

## Open Questions
Anything that needs human input before planning can begin.
```

**Exit criteria for refine phase:** Analysis document exists, recommended approach is selected, no unresolved open questions (or they've been converted to HITL decisions).

### Plan Document (Plan Phase Output)

The plan phase produces a plan committed to `docs/issues/{number}-plan.md`. This plan is what gets decomposed into the contract's `phases[].tasks[]`.

```markdown
# Issue #{number}: Plan

## Summary
One paragraph: what will be built and why (references the analysis).

## Implementation Phases
For each phase:

### Phase N: {Name}
- **Goal**: What this phase achieves
- **Tasks**:
  - Task ID, description, acceptance criteria, files affected
- **Dependencies**: What must be true before this phase starts
- **Exit criteria**: How the reviewer knows this phase is complete

## Test Strategy
- What tests will be added/modified
- How to verify the change end-to-end

## Rollback / Risk
- What could go wrong
- How to revert if needed

## Migration (if applicable)
- Breaking changes and migration path
```

**Exit criteria for plan phase:** Plan document exists, all phases have tasks with acceptance criteria, human has approved via HITL checkpoint.

### How Documents Connect to the Contract JSON

When the plan is approved and the pipeline transitions to the implement phase:
1. The plan's phases and tasks get written into the contract JSON as `phases[].tasks[]`
2. Each task's acceptance criteria from the plan become the basis for reviewer evaluation
3. The analysis document's recommended approach provides context for both implementer and reviewer

The contract JSON tracks *status*, while the documents provide *content and context*.

---

## Part 3: Task Collection and Role-Based Enforcement

### Contract Structure

```json
{
  "schemaVersion": "1.0",
  "issue": {
    "number": 133,
    "title": "Implement structurally enforced agent checkpoints",
    "url": "https://github.com/jwbron/egg/issues/133"
  },
  "acceptance_criteria": [
    {
      "id": "ac-1",
      "description": "Implementer cannot mark tasks complete",
      "verified": false
    }
  ],
  "phases": [
    {
      "id": "phase-1",
      "name": "Contract Schema and CLI",
      "status": "pending",
      "review_feedback": [],
      "tasks": [
        {
          "id": "task-1",
          "description": "Create contract JSON schema",
          "status": "pending",
          "commit": null,
          "notes": "",
          "review_cycles": 0,
          "max_cycles": 3,
          "escalated": false,
          "escalation_reason": null
        }
      ]
    }
  ],
  "decisions": [
    {
      "id": "decision-1",
      "question": "Approve implementation plan?",
      "type": "hitl",
      "resolved": false,
      "resolution": null,
      "resolved_by": null,
      "debounce_until": null
    }
  ],
  "audit_log": []
}
```

### Role-Based Field Access

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| **Implementer** | `tasks[].commit`, `tasks[].notes`, `phases[].commits` | `tasks[].status`, `phases[].passes`, `acceptance_criteria[].verified` |
| **Reviewer** | `tasks[].status`, `phases[].passes`, `phases[].review_feedback[]` | `tasks[].commit`, task definitions |
| **Human** | All fields including `decisions[].resolved` | (unrestricted) |

### CLI Commands

```bash
# Implementer commands
egg-contract add-commit --task task-1 --commit abc123
egg-contract update-notes --task task-1 --notes "Implementation complete"

# Reviewer commands (fails if role != reviewer)
egg-contract mark-task --task task-1 --status complete
egg-contract mark-phase --phase phase-1 --passed true

# Human-only (fails for all agent roles)
egg-contract resolve-decision --decision decision-1 --resolution approved
```

### Gateway-Routed Contract Mutations

**Critical**: All contract mutations route through the gateway sidecar. This prevents privilege escalation where an agent could set `EGG_AGENT_ROLE=human`.

- The gateway reads role from **workflow context** (GitHub Actions job metadata), not environment variables set by the agent
- The `egg-contract` CLI communicates with the gateway, which validates the mutation against the caller's role

> **Note**: Rather than using pre-commit hooks for validation, the reviewer automatically kicks incomplete tasks back to the implementer. This ensures implementation must be complete before the workflow moves to the next task.

---

## Part 4: Human-in-the-Loop Decision System

### Checkbox-Based HITL Decisions

HITL decisions are rendered as checkboxes in bot comments:

```markdown
### Decision Required: Approve Plan?

Please select one option:

- [ ] **Approve plan** - Proceed to implementation
- [ ] **Request changes** - I'll provide feedback below
- [ ] **Reject plan** - This approach won't work
```

### Decision Detection Workflow

1. Bot posts comment with checkboxes
2. Human checks a checkbox
3. GitHub triggers `issue_comment.edited` event
4. Workflow detects checkbox state change
5. **30-second debounce**: Wait for additional edits
6. Update contract with resolution
7. Resume pipeline

### Debounce Handling

- 30-second debounce period after checkbox change detection
- Timer resets if comment is edited again within the window
- `debounce_until` field in contract tracks when debounce expires
- Pipeline only resumes after debounce period with stable checkbox state

### Edge Cases

| Scenario | Handling |
|----------|----------|
| Multiple boxes checked | Accept first checked box, warn in comment |
| Box unchecked after resume | Decision is immutable once resolved |
| Comment edited (not checkbox) | Ignore if checkbox state unchanged |

---

## Part 5: Circuit Breaker and Escalation

### Thresholds

| Metric | Threshold | Action |
|--------|-----------|--------|
| Per-task review cycles | 3 | Request human review for stuck task |
| Total pipeline cycles | 10 | Escalate entire pipeline |

### Circuit Breaker State

```json
{
  "phases": [
    {
      "id": "phase-1",
      "tasks": [
        {
          "id": "task-1",
          "review_cycles": 0,
          "max_cycles": 3,
          "escalated": false
        }
      ]
    }
  ],
  "circuit_breaker": {
    "total_cycles": 0,
    "max_total_cycles": 10,
    "status": "closed"
  }
}
```

### State Transitions

- **CLOSED**: Normal operation, implement→review cycles continue
- **CLOSED** → **OPEN**: Per-task threshold exceeded, human review needed
- **OPEN** → **CLOSED**: Human provides guidance, cycle resumes

### Escalation Actions

1. Label issue with `needs-human-intervention`
2. Post detailed context comment with:
   - Current phase and task
   - Review feedback history
   - Suggested resolution paths
3. Create HITL decision checkbox for human guidance

---

## Part 6: Resume After HITL

### Resume Triggers

| Trigger | Source | Action |
|---------|--------|--------|
| Checkbox checked | `issue_comment.edited` | Resume from paused stage |
| Label removed | `issues.unlabeled` | Resume with fresh retry |
| Comment keyword | `issue_comment.created` | Parse and resume |

### State Preservation

The contract JSON preserves:
- Current phase and task
- All prior review feedback
- Decision history
- Audit log of all modifications

When resuming:
1. Load contract from branch
2. Validate resume trigger matches expected decision
3. Update decision as resolved
4. Continue from next pending task/phase

---

## Part 7: Reviewer Workflow Architecture

### Reviewer Kick-Back Pattern

The pipeline executes all tasks in a plan automatically. The reviewer evaluates each task and kicks incomplete tasks back to the implementer:

1. **Implementer** executes all tasks in the current phase
2. **Reviewer** evaluates each task against acceptance criteria
3. **Incomplete tasks** are marked with feedback and returned to implementer
4. **Cycle repeats** until all tasks pass or per-task threshold exceeded
5. **Human review** only triggered if a single task spins too long

This ensures implementation must be complete before the workflow moves to the next task, without requiring pre-commit hooks.

### Separate Reusable Workflow

The Reviewer runs as a **separate GitHub Actions workflow** using `workflow_call`:

```yaml
# .github/workflows/sdlc-review.yml
name: SDLC Review
on:
  workflow_call:
    inputs:
      issue_number:
        required: true
        type: number
      phase_id:
        required: true
        type: string
      branch:
        required: true
        type: string

jobs:
  review:
    runs-on: ubuntu-latest
    env:
      EGG_AGENT_ROLE: reviewer
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.branch }}
      - uses: ./action
        with:
          role: reviewer
          prompt: |
            Review phase ${{ inputs.phase_id }} for issue #${{ inputs.issue_number }}.
            Evaluate each task against acceptance criteria.
            Mark complete tasks as passed.
            For incomplete tasks: mark as incomplete with specific feedback for implementer.
```

### Context Window Isolation

Each "separate context window" is a **separate GitHub Actions job**. The implement→review cycle loops until all tasks pass:

```yaml
jobs:
  implement:
    runs-on: ubuntu-latest
    env:
      EGG_AGENT_ROLE: implementer
    steps:
      - uses: ./action
        with:
          role: implementer
          prompt: "Implement all tasks in phase-1. Address reviewer feedback if present."

  review:
    needs: implement
    runs-on: ubuntu-latest
    env:
      EGG_AGENT_ROLE: reviewer
    steps:
      - uses: ./action
        with:
          role: reviewer
          prompt: "Review phase-1. Mark complete tasks. Kick back incomplete tasks with feedback."

  loop:
    needs: review
    if: needs.review.outputs.has_incomplete_tasks == 'true'
    uses: ./.github/workflows/sdlc-pipeline.yml
    with:
      issue_number: ${{ inputs.issue_number }}
      phase_id: ${{ inputs.phase_id }}
```

### Reviewer System Prompt

Located at `sandbox/.claude/reviewer-rules.md`:
- Focused on verification, not implementation
- Cannot modify code, only review
- Must produce structured verdict with pass/fail per task
- For failed tasks: provide specific, actionable feedback for implementer
- Loaded via `--rules` flag in action

---

## Acceptance Criteria

### Structural Enforcement
- [ ] Contract CLI (`egg-contract`) rejects unauthorized field modifications
- [ ] Gateway validates role before allowing contract mutations
- [ ] Implementer role cannot mark tasks as complete (verified by test)
- [ ] Reviewer role cannot modify task commits (verified by test)
- [ ] Reviewer kicks incomplete tasks back to implementer with feedback
- [ ] Pipeline loops implement→review until all tasks pass or threshold exceeded

### Phase-Based Operation Filtering (Addresses #202)
- [ ] Gateway blocks `git push` during refine and plan phases
- [ ] Gateway blocks `gh pr create` until implementation phase is complete
- [ ] Agent receives clear error message when operation is blocked
- [ ] Phase transitions require appropriate approval (human or reviewer)
- [ ] Audit log records blocked operation attempts

### Task Collection
- [ ] Contract schema supports phases, tasks, and acceptance criteria
- [ ] Tasks link to commits (1:1 relationship preferred, enforced by CLI)
- [ ] Schema versioning field for future migrations
- [ ] Audit log tracks all modifications with timestamp, actor, action, field path

### HITL Decisions
- [ ] Checkbox-based decisions rendered in bot comments
- [ ] `issue_comment.edited` trigger detects checkbox changes
- [ ] 30-second debounce prevents premature resume
- [ ] Decisions are immutable once resolved
- [ ] Only humans can resolve HITL-type decisions

### Pipeline Flow
- [ ] Worker and Reviewer run in separate GitHub Actions jobs
- [ ] Circuit breaker tracks per-phase and total review cycles
- [ ] Escalation labels issue and posts context comment
- [ ] Pipeline resumes correctly after HITL resolution

### Document Standards
- [ ] Refine phase produces analysis document at `docs/issues/{number}-analysis.md`
- [ ] Analysis document follows standard template (Problem Statement, Current Behavior, Constraints, Options, Recommended Approach, Open Questions)
- [ ] Plan phase produces plan document at `docs/issues/{number}-plan.md`
- [ ] Plan document follows standard template (Summary, Implementation Phases with tasks and acceptance criteria, Test Strategy, Rollback/Risk)
- [ ] Prompt builder (`action/build-sdlc-prompt.sh`) instructs agent to follow document templates
- [ ] Plan tasks are extracted into contract JSON `phases[].tasks[]` on plan approval

---

## Resolved Open Questions

| Question | Decision | Rationale |
|----------|----------|-----------|
| HITL at planning? | Configurable, default required | High-risk default, opt-out for routine |
| Branch naming? | Single branch (`egg-{issue}`) | Simpler PR management |
| Parallel phases? | Sequential only (MVP) | Avoid merge conflicts |
| Reviewer model? | Same model as worker | Consistent quality |
| Context isolation? | Separate GitHub Actions jobs | Clean isolation, fresh context |
| Contract enforcement? | Gateway-routed mutations | Prevents privilege escalation |
| Task-to-commit? | 1:1 preferred, enforced by CLI | Clean traceability |
| HITL debounce? | 30-second period | Allows human to finish edits |
| Reviewer workflow? | Separate reusable workflow | Clean separation, `workflow_call` |
| Phase outputs? | Structured documents (analysis, plan) | Consistent reviewer experience, clear exit criteria |

---

## Implementation Phases

### Phase 1: Contract Schema and Validation (Foundation)
- Contract JSON schema at `.egg/schemas/contract.schema.json`
- Contract library at `shared/egg_contracts/`
- Contract CLI at `sandbox/egg_lib/contract_cli.py`
- Unit tests for schema validation and role enforcement

### Phase 2: Review Agent Infrastructure
- Reviewer system prompt at `sandbox/.claude/reviewer-rules.md`
- Action role support (`role` input, `EGG_AGENT_ROLE` env var)
- Reviewer output parsing and contract update logic

### Phase 3: Pipeline Workflow
- Main workflow at `.github/workflows/sdlc-pipeline.yml`
- Reviewer workflow at `.github/workflows/sdlc-review.yml`
- Stage-specific prompt builder scripts
- Contract state management between jobs

### Phase 4: Circuit Breaker and Escalation
- Circuit breaker logic at `shared/egg_contracts/circuit_breaker.py`
- Escalation script at `action/escalate.sh`
- HITL checkbox detection workflow
- 30-second debounce implementation

### Phase 5: Integration and Testing
- Integration test suite
- Pipeline documentation and ADR
- Enhanced observability and logging

---

## Error Message Examples

When CLI rejects an operation:

```
Error: Cannot mark task as complete.
Role 'implementer' is not authorized to modify 'tasks[].status'.
This field can only be modified by role 'reviewer'.
```

When circuit breaker triggers:

```
Error: Circuit breaker OPEN for phase 'phase-1'.
Review cycles (3) exceeded threshold (3).
Human intervention required. See issue comment for context.
```

---

## Testing Strategy

| Test Type | Scope | Example |
|-----------|-------|---------|
| Unit | CLI role enforcement | Mock `EGG_AGENT_ROLE`, verify rejection |
| Integration | Worker → Reviewer handoff | Run both agents, verify contract state |
| E2E | Full pipeline | Trigger workflow, verify issue updates |

---

*This specification is maintained alongside the codebase. For the latest version, see the file in the repository.*
