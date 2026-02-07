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
2. **Separate Context Windows**: Each agent invocation (worker, reviewer) runs in a separate GitHub Actions job with fresh context.
3. **Contract-as-Code**: All state is stored in `.egg/contracts/{issue-number}.json` and committed to the branch.
4. **Human-in-the-Loop**: Critical decisions pause the pipeline for human input via GitHub checkboxes.

---

## Part 2: Task Collection and Role-Based Enforcement

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
      "review_cycles": 0,
      "max_cycles": 3,
      "escalated": false,
      "escalation_reason": null,
      "tasks": [
        {
          "id": "task-1",
          "description": "Create contract JSON schema",
          "status": "pending",
          "commit": null,
          "notes": ""
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
- Pre-commit hooks provide defense-in-depth validation

---

## Part 3: Human-in-the-Loop Decision System

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

## Part 4: Circuit Breaker and Escalation

### Thresholds

| Metric | Threshold | Action |
|--------|-----------|--------|
| Per-phase review cycles | 3 | Escalate phase |
| Total review cycles | 10 | Escalate entire pipeline |
| Consecutive failures | 2 | Pause and notify |

### Circuit Breaker State

```json
{
  "phases": [
    {
      "id": "phase-1",
      "review_cycles": 0,
      "max_cycles": 3,
      "escalated": false,
      "escalation_reason": null
    }
  ],
  "circuit_breaker": {
    "total_cycles": 0,
    "max_total_cycles": 10,
    "consecutive_failures": 0,
    "status": "closed"
  }
}
```

### State Transitions

- **CLOSED** → **OPEN**: Threshold exceeded
- **OPEN** → **HALF-OPEN**: Human intervention received
- **HALF-OPEN** → **CLOSED**: Next review passes
- **HALF-OPEN** → **OPEN**: Next review fails

### Escalation Actions

1. Label issue with `needs-human-intervention`
2. Post detailed context comment with:
   - Current phase and task
   - Review feedback history
   - Suggested resolution paths
3. Create HITL decision checkbox for human guidance

---

## Part 5: Resume After HITL

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

## Part 6: Reviewer Workflow Architecture

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
            Evaluate against acceptance criteria.
            Use egg-contract CLI to mark task status.
```

### Context Window Isolation

Each "separate context window" is a **separate GitHub Actions job**:

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
          prompt: "Implement phase-1 tasks..."

  review:
    needs: implement
    runs-on: ubuntu-latest
    env:
      EGG_AGENT_ROLE: reviewer
    steps:
      - uses: ./action
        with:
          role: reviewer
          prompt: "Review phase-1 implementation..."
```

### Reviewer System Prompt

Located at `sandbox/.claude/reviewer-rules.md`:
- Focused on verification, not implementation
- Cannot modify code, only review
- Must produce structured verdict output
- Loaded via `--rules` flag in action

---

## Acceptance Criteria

### Structural Enforcement
- [ ] Contract CLI (`egg-contract`) rejects unauthorized field modifications
- [ ] Gateway validates role before allowing contract mutations
- [ ] Pre-commit hook validates contract changes respect role ownership
- [ ] Implementer role cannot mark tasks as complete (verified by test)
- [ ] Reviewer role cannot modify task commits (verified by test)

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
