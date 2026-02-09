# Plan: [Issue Title]

> Issue: #[number] | Phase: plan

## Summary

[2-3 sentence overview of the implementation approach. Reference the analysis document if applicable.]

## Implementation Phases

### Phase 1: [Phase Name]

**Goal**: [What this phase achieves]

**Tasks**:
- [TASK-1-1] [Task description] — Acceptance: [Criteria for completion]
- [TASK-1-2] [Task description] — Acceptance: [Criteria for completion]

**Dependencies**: [What must be completed before this phase]

**Exit criteria**: [How we know this phase is complete]

### Phase 2: [Phase Name]

**Goal**: [What this phase achieves]

**Tasks**:
- [TASK-2-1] [Task description] — Acceptance: [Criteria for completion]
- [TASK-2-2] [Task description] — Acceptance: [Criteria for completion]

**Dependencies**: Phase 1

**Exit criteria**: [How we know this phase is complete]

## Test Strategy

- **Unit tests**: [What unit tests will be added]
- **Integration tests**: [What integration tests will be added]
- **Manual testing**: [Steps for manual verification]

## Rollback Plan

[How to revert if something goes wrong. Include specific commands or steps.]

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| [Risk 1] | Low/Med/High | Low/Med/High | [How to mitigate] |
| [Risk 2] | Low/Med/High | Low/Med/High | [How to mitigate] |

## Migration Notes

[If applicable: database migrations, config changes, breaking changes for users]

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "[Concise PR title, max 70 chars]"
  description: |
    [2-3 sentence description of the PR. Explain the problem being solved
    and the approach taken. Link to the issue for additional context.]
phases:
  - id: 1
    name: [Phase Name]
    goal: [What this phase achieves]
    tasks:
      - id: TASK-1-1
        description: [Task description]
        acceptance: [Criteria for completion]
        files:
          - [path/to/file]
      - id: TASK-1-2
        description: [Task description]
        acceptance: [Criteria for completion]
        files:
          - [path/to/file]
  - id: 2
    name: [Phase Name]
    goal: [What this phase achieves]
    tasks:
      - id: TASK-2-1
        description: [Task description]
        acceptance: [Criteria for completion]
        files:
          - [path/to/file]
```

---

*Authored-by: egg*
