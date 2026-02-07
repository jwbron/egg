# Issue #{number}: Plan

> Implementation plan for the plan phase. This document is decomposed into contract tasks on approval.

## Summary

_One paragraph: what will be built and why (references the analysis document)._

## Implementation Phases

### Phase 1: [Name]

**Goal**: _What this phase achieves_

**Tasks**:

| ID | Description | Acceptance Criteria | Files |
|----|-------------|---------------------|-------|
| task-1 | _Task description_ | _How reviewer knows it's complete_ | `path/to/file.py` |
| task-2 | _Task description_ | _How reviewer knows it's complete_ | `path/to/file.py` |

**Dependencies**: _What must be true before this phase starts_

**Exit criteria**: _How the reviewer knows this phase is complete_

### Phase 2: [Name]

**Goal**: _What this phase achieves_

**Tasks**:

| ID | Description | Acceptance Criteria | Files |
|----|-------------|---------------------|-------|
| task-3 | _Task description_ | _How reviewer knows it's complete_ | `path/to/file.py` |
| task-4 | _Task description_ | _How reviewer knows it's complete_ | `path/to/file.py` |

**Dependencies**: _What must be true before this phase starts_

**Exit criteria**: _How the reviewer knows this phase is complete_

## Test Strategy

- _What tests will be added/modified_
- _How to verify the change end-to-end_
- _Edge cases to cover_

## Rollback / Risk

### What Could Go Wrong
- _Risk 1 and mitigation_
- _Risk 2 and mitigation_

### How to Revert
_Steps to revert if the change causes problems_

## Migration (if applicable)

### Breaking Changes
_List any breaking changes and who is affected_

### Migration Path
_Steps for consumers to migrate_

---

**Exit criteria for plan phase:**
- [ ] Plan document committed to `docs/issues/{number}-plan.md`
- [ ] All phases have tasks with acceptance criteria
- [ ] Test strategy defined
- [ ] Human approval via HITL checkpoint
- [ ] Tasks extracted to contract JSON
