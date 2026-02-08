# SDLC Pipeline Architecture

> Structurally enforced agent checkpoints and verification gates for autonomous software development.

This document describes the SDLC (Software Development Lifecycle) pipeline that enables autonomous agents to work on issues while maintaining quality through structural enforcement—not just prompts.

For the architectural decision record with threat model and security properties, see [ADR: SDLC Pipeline](../adr/implemented/ADR-SDLC-Pipeline.md).

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

### 3. Role-Based Access Control

The pipeline enforces role-based field ownership in contracts:

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| **Implementer** | `commit`, `notes`, `files_affected` | `status`, `verified`, `review_feedback` |
| **Reviewer** | `status`, `review_feedback`, `current_phase` | `commit`, task definitions |
| **Human** | All fields | — |

Code reviews are performed by the existing PR review workflow (`reusable-review.yml`), which provides line-level feedback on draft PRs created during the implement phase.

### 4. Human-in-the-Loop at Critical Points

The pipeline pauses for human approval at phase transitions and when circuit breakers trigger. Decisions use checkbox-based UI with 30-second debounce to prevent accidental clicks.

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           SDLC PIPELINE                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐  │
│  │   REFINE    │───▶│    PLAN     │───▶│  IMPLEMENT  │───▶│ CREATE   │  │
│  │   ISSUE     │    │             │    │  (cycles)   │    │   PR     │  │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘  │
│        │                  │                  │                  │       │
│        ▼                  ▼                  ▼                  ▼       │
│   ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐  │
│   │ HITL    │        │ HITL    │        │ REVIEW  │        │  HUMAN  │  │
│   │ Approve │        │ Approve │        │ (auto)  │        │  MERGE  │  │
│   └─────────┘        └─────────┘        └─────────┘        └─────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Phases

| Phase | Purpose | Allowed Operations | Exit Requires |
|-------|---------|-------------------|---------------|
| **Refine** | Analyze issue, produce analysis document | `gh issue comment/edit` | Human approval |
| **Plan** | Create implementation plan with tasks | `gh issue comment/edit`, `egg-contract add-decision` | Human approval |
| **Implement** | Execute tasks on draft PR with CI and review feedback | `git push`, `egg-contract add-commit/update-notes` | All checks pass (CI + PR review) |
| **PR** | Finalize PR for human review and merge | `gh pr edit`, `git push` | Human merge |

### Phase-Based Operation Filtering

Each phase has a defined set of permitted operations. The gateway blocks all other operations:

- **Refine/Plan phases**: Cannot `git push` or `gh pr create`—prevents code changes before plan approval
- **Implement phase**: Can `git push` to the branch; draft PR is created automatically by the pipeline
- **PR phase**: Can update the PR; human must merge

This prevents incidents where agents push code during planning or manually create PRs before implementation is complete.

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

### Implement and PR-Based Review

The implement phase uses PR-based automated code review:

1. **Implementer executes tasks** — The implementer agent runs, commits changes, and pushes to the branch
2. **Draft PR created** — After implementation succeeds, a draft PR is created automatically with commit messages in the description
3. **CI and review checks** — The pipeline waits for all GitHub check runs (linting, tests, and PR review) to complete
4. **Review feedback** — The `reusable-review.yml` workflow provides line-level code review comments on the draft PR
5. **Re-implementation cycles** — If checks fail or review requests changes, the implementer is re-invoked with feedback
6. **PR finalization** — Once all checks pass and review approves, the draft PR is marked ready for human merge

This approach provides:
- Line-level code review comments visible to humans
- Integration with existing PR review workflows
- Human visibility into every implementation cycle
- CI/test validation before review

### Context Window Isolation

Each agent invocation runs in a fresh container with no memory of previous runs. All state transfer happens through:

1. The contract JSON in `.egg-state/contracts/`
2. Git commits on the feature branch
3. GitHub issue/PR comments and reviews

This prevents context pollution and ensures reproducible behavior. When the implementer is re-invoked after review feedback, it receives the PR review comments as part of its prompt context.

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

### Task Population

Tasks are automatically extracted from the plan document and populated into the contract during the plan phase, after the plan document is validated.

The `action/populate-contract-tasks.py` script:
1. Fetches the plan comment from the GitHub issue
2. Parses task markers using `shared/egg_contracts/plan_parser.py`
3. Writes phases and tasks into `.egg-state/contracts/{issue-number}.json`
4. Validates the contract against the JSON schema
5. Commits the updated contract to the feature branch

This happens in the plan phase itself (before human approval) to provide early validation of the plan format. The implement phase also runs task population as a fallback in case the plan phase step failed or was skipped.

## Implementation Reference

### Key Files

| File | Purpose |
|------|---------|
| `.github/workflows/sdlc-pipeline.yml` | Main pipeline orchestration |
| `.github/workflows/reusable-review.yml` | PR-based code review workflow |
| `.github/workflows/sdlc-hitl.yml` | HITL checkbox detection |
| `action/build-sdlc-prompt.sh` | Phase-specific prompt builder |
| `action/populate-contract-tasks.py` | Extracts tasks from plan into contract |
| `action/contract-state.sh` | Contract state management utility |
| `sandbox/scripts/gh` | gh wrapper with self-review fallback |
| `shared/egg_contracts/models.py` | Pydantic models for contract |
| `shared/egg_contracts/plan_parser.py` | Parses plan documents for task extraction |
| `shared/egg_contracts/roles.py` | Role definitions and field ownership |
| `shared/egg_contracts/validator.py` | Mutation validation |
| `shared/egg_contracts/circuit_breaker.py` | Escalation logic (deprecated) |
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

# Create HITL decision point
egg-contract add-decision --question "Should we proceed with approach X?"
```

**Note:** `mark-task` and `mark-phase` commands (previously used by the dedicated reviewer agent) are deprecated as of PR #285. Task validation now happens via PR-based code review.

---

*See also: [ADR: SDLC Pipeline](../adr/implemented/ADR-SDLC-Pipeline.md), [Analysis Template](../templates/analysis.md), [Plan Template](../templates/plan.md), [GitHub Automation](github-automation.md)*
