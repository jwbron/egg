# SDLC Pipeline Architecture

> Structurally enforced agent checkpoints and verification gates for autonomous software development.

This document describes the SDLC (Software Development Lifecycle) pipeline that enables autonomous agents to work on issues while maintaining quality through structural enforcement—not just prompts.

## Guiding Principles

### 1. Structural Enforcement Over Prompt Compliance

Agents cannot be trusted to self-police via prompts alone. The pipeline enforces constraints at multiple infrastructure layers:

- **Gateway-level operation filtering**: The gateway blocks operations not permitted in the current phase
- **Role-based field ownership**: Contract mutations are validated against caller role
- **Separate context windows**: Each agent invocation runs in a separate GitHub Actions job with fresh context

### 2. Contract-as-Code

All pipeline state is stored in JSON contracts at `.egg-state/contracts/{issue-number}.json` and committed to the feature branch (not main). This provides:

- Auditable history of all state changes
- Recovery from failures without losing progress
- Clear handoff between phases and agents

### 3. Worker-Reviewer Separation

The implementer and reviewer are separate agent invocations with different permissions:

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| **Implementer** | `commit`, `notes`, `files_affected` | `status`, `verified`, `review_feedback` |
| **Reviewer** | `status`, `review_feedback`, `current_phase` | `commit`, task definitions |
| **Human** | All fields | — |

### 4. Human-in-the-Loop at Critical Points

The pipeline pauses for human approval at phase transitions and when circuit breakers trigger. Decisions use checkbox-based UI with 30-second debounce to prevent accidental clicks.

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           SDLC PIPELINE                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐ │
│  │   REFINE    │───▶│    PLAN     │───▶│  IMPLEMENT  │───▶│ CREATE   │ │
│  │   ISSUE     │    │             │    │  (cycles)   │    │   PR     │ │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘ │
│        │                  │                  │                  │       │
│        ▼                  ▼                  ▼                  ▼       │
│   ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐ │
│   │ HITL    │        │ HITL    │        │ REVIEW  │        │  HUMAN  │ │
│   │ Approve │        │ Approve │        │ (auto)  │        │  MERGE  │ │
│   └─────────┘        └─────────┘        └─────────┘        └─────────┘ │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Phases

| Phase | Purpose | Allowed Operations | Exit Requires |
|-------|---------|-------------------|---------------|
| **Refine** | Analyze issue, produce analysis document | `gh issue comment/edit` | Human approval |
| **Plan** | Create implementation plan with tasks | `gh issue comment/edit`, `egg-contract add-decision` | Human approval |
| **Implement** | Execute tasks, implement→review cycles | `git push`, `egg-contract add-commit/update-notes/mark-task` | Reviewer approval |
| **PR** | Create pull request for human review | `gh pr create/edit`, `git push` | Human merge |

### Phase-Based Operation Filtering

Each phase has a defined set of permitted operations. The gateway blocks all other operations:

- **Refine/Plan phases**: Cannot `git push` or `gh pr create`—prevents code changes before plan approval
- **Implement phase**: Cannot `gh pr create` until reviewer marks tasks complete
- **PR phase**: All operations allowed; human must merge

This prevents incidents where agents push code during planning or create PRs before implementation is verified.

## Contract System

### Directory Structure

| Directory | Purpose | Committed To |
|-----------|---------|--------------|
| `.egg/schemas/` | Contract JSON schema definitions | `main` |
| `.egg/phase-permissions.json` | Phase operation restrictions | `main` |
| `.egg-state/contracts/` | Per-issue contract instances | Feature branches only |

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
  "circuit_breaker": {
    "total_cycles": 2,
    "max_total_cycles": 10,
    "status": "closed"
  },
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

### Implement→Review Cycle

The implement phase uses a cyclic pattern:

1. **Implementer** executes all tasks in the current phase
2. **Reviewer** evaluates each task against acceptance criteria
3. **Incomplete tasks** are marked with feedback and returned to implementer
4. **Cycle repeats** until all tasks pass or circuit breaker triggers
5. **Human review** only triggered if circuit breaker opens

```yaml
# From .github/workflows/sdlc-pipeline.yml
jobs:
  implement:
    env:
      EGG_AGENT_ROLE: implementer
    # Runs egg with implementer prompt

  review:
    needs: implement
    env:
      EGG_AGENT_ROLE: reviewer
    # Evaluates tasks, marks status

  loop:
    needs: review
    # Checks if more cycles needed or advance to PR
```

### Context Window Isolation

Each job runs in a fresh container with no memory of previous invocations. All state transfer happens through:

1. The contract JSON in `.egg-state/contracts/`
2. Git commits on the feature branch
3. GitHub issue/PR comments

This prevents context pollution and ensures reproducible behavior.

## Circuit Breaker and Escalation

### Thresholds

| Metric | Threshold | Action |
|--------|-----------|--------|
| Per-task review cycles | 3 | Escalate task to human |
| Total pipeline cycles | 10 | Open circuit breaker, pause pipeline |

### Circuit Breaker States

- **CLOSED**: Normal operation, implement→review cycles continue
- **OPEN**: Human intervention required; pipeline paused

When the circuit breaker opens:

1. Issue is labeled `needs-human-intervention`
2. Context comment posted with task history and review feedback
3. HITL decision checkboxes presented for human guidance

### Escalation Summary

The `circuit_breaker.py` module provides escalation summaries:

```python
def get_escalation_summary(contract):
    return {
        "circuit_breaker_status": "open",
        "total_cycles": 8,
        "escalated_tasks": [
            {"id": "task-1-2", "cycles": 3, "description": "..."}
        ],
        "recommendation": "Review acceptance criteria..."
    }
```

## Human-in-the-Loop Decisions

### Checkbox-Based Interface

HITL decisions render as checkboxes in bot comments:

```markdown
## Human Decision Required

### Option 1: Provide Guidance
<!-- HITL-DECISION: guidance -->
- [ ] I will provide additional context or requirements below
- [ ] The acceptance criteria should be adjusted
- [ ] Break this task into smaller sub-tasks

### Option 2: Override
<!-- HITL-DECISION: override -->
- [ ] Mark current tasks as complete (override review)
- [ ] Skip remaining tasks in this phase
- [ ] Cancel the pipeline for this issue
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

## Implementation Reference

### Key Files

| File | Purpose |
|------|---------|
| `.github/workflows/sdlc-pipeline.yml` | Main pipeline orchestration |
| `.github/workflows/sdlc-hitl.yml` | HITL checkbox detection |
| `shared/egg_contracts/models.py` | Pydantic models for contract |
| `shared/egg_contracts/roles.py` | Role definitions and field ownership |
| `shared/egg_contracts/validator.py` | Mutation validation |
| `shared/egg_contracts/circuit_breaker.py` | Escalation logic |
| `shared/egg_contracts/hitl.py` | Checkbox parsing and debounce |
| `.egg/schemas/contract.schema.json` | JSON schema definition |
| `.egg/phase-permissions.json` | Phase operation restrictions |

### Triggering the Pipeline

**Via label**:
```bash
gh issue edit 123 --add-label "egg-sdlc"
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

# Mark task status (reviewer only)
egg-contract mark-task --task task-1-1 --status complete

# Mark phase status (reviewer only)
egg-contract mark-phase --phase phase-1 --passed true
```

---

*See also: [Analysis Template](../templates/analysis.md), [Plan Template](../templates/plan.md), [GitHub Automation](github-automation.md)*
