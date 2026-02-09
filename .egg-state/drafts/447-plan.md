# Plan: Document SDLC Work Loop Concepts

> Issue: #447 | Phase: plan

## Summary

This plan creates conceptual documentation for the agentic work loop pattern that underlies the egg SDLC pipeline. Following the approved analysis, we will create a new top-level document (`docs/agentic-work-loop.md`) explaining the three-layer feedback loop model, update `docs/index.md` to reference it, and add cross-references from `docs/guides/sdlc-pipeline.md`. The document will serve as the conceptual foundation alongside the technical foundations (gateway, sandbox).

## Implementation Phases

### Phase 1: Create Core Conceptual Document

**Goal**: Create the foundational document explaining the agentic work loop model.

**Tasks**:
- [TASK-1-1] Create `docs/agentic-work-loop.md` with overview and three-layer model introduction — Acceptance: Document exists with clear introduction explaining the conceptual framework
- [TASK-1-2] Write the "Agentic Feedback Loop" section covering work/review/address-feedback cycle — Acceptance: Section explains each phase of the inner loop with concrete examples
- [TASK-1-3] Write the "Human Feedback Loop" section covering review gates and approval — Acceptance: Section explains when and how humans enter the loop
- [TASK-1-4] Write the "Complete Pipeline" section with phase diagram covering problem statement through acceptance — Acceptance: ASCII diagram shows full pipeline with both loop types marked
- [TASK-1-5] Write the "Why This Works" section explaining quality through iteration, alignment through gates, and safety through structure — Acceptance: Section provides clear rationale for the workflow model
- [TASK-1-6] Write the "Scaling Through Delegation" section as a forward-looking placeholder for agent decomposition — Acceptance: Section acknowledges future capability without over-specifying

**Dependencies**: None

**Exit criteria**: Complete conceptual document exists at `docs/agentic-work-loop.md` covering all approved sections.

### Phase 2: Update Documentation Index and Cross-References

**Goal**: Integrate the new document into the documentation structure.

**Tasks**:
- [TASK-2-1] Update `docs/index.md` to add the new document in the Strategy section — Acceptance: New entry appears in the Strategy table with appropriate description
- [TASK-2-2] Update `docs/guides/sdlc-pipeline.md` introduction to reference the conceptual doc — Acceptance: Opening section includes a reference to `agentic-work-loop.md` for conceptual background
- [TASK-2-3] Add cross-reference from `docs/collaboration-effectiveness.md` to the new document — Acceptance: Related documentation section or inline reference connects the two docs

**Dependencies**: Phase 1

**Exit criteria**: All documentation files properly cross-reference each other; `docs/index.md` includes the new document.

### Phase 3: Final Review and Consistency Check

**Goal**: Ensure documentation is consistent and complete.

**Tasks**:
- [TASK-3-1] Review all modified files for consistent terminology and style — Acceptance: Terminology matches existing docs (e.g., "human-in-the-loop", "phase transitions", "approval gates")
- [TASK-3-2] Verify all internal links work correctly — Acceptance: All markdown links resolve to existing files
- [TASK-3-3] Ensure ASCII diagrams render correctly in GitHub markdown — Acceptance: Diagrams display properly when viewed raw on GitHub

**Dependencies**: Phase 2

**Exit criteria**: All documentation passes consistency and link checks.

## Test Strategy

- **Manual testing**: Preview all markdown files in GitHub to verify rendering
- **Link verification**: Check all internal document links resolve correctly
- **Style verification**: Compare terminology and formatting against existing documents like `docs/collaboration-effectiveness.md` and `docs/guides/sdlc-pipeline.md`

## Rollback Plan

Since this is purely documentation with no code changes:

1. If issues are found post-merge, simply revert the PR:
   ```bash
   git revert <merge-commit>
   ```

2. If partial rollback is needed, the changes are isolated to:
   - `docs/agentic-work-loop.md` (new file — can be deleted)
   - `docs/index.md` (single line addition — easy to revert)
   - `docs/guides/sdlc-pipeline.md` (single paragraph addition — easy to revert)
   - `docs/collaboration-effectiveness.md` (single line addition — easy to revert)

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Terminology inconsistency with existing docs | Medium | Low | Explicitly reference terminology from existing docs during writing |
| Document becomes outdated as pipeline evolves | Low | Medium | Focus on conceptual patterns rather than implementation details |
| Duplication with existing documentation | Low | Low | Reference existing docs for details rather than repeating content |
| Diagram doesn't render correctly on GitHub | Low | Low | Use existing diagram patterns from sdlc-pipeline.md |

## Migration Notes

No migrations required. This is purely additive documentation with no breaking changes. No configuration changes, database migrations, or API changes are involved.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add agentic work loop conceptual documentation"
  description: |
    Creates foundational documentation explaining the agentic work loop pattern
    that underlies the egg SDLC pipeline. Introduces the three-layer feedback
    loop model (agentic loop, human loop, complete pipeline) and explains why
    this structured iteration approach produces high-quality results.

    Closes #447
phases:
  - id: 1
    name: Create Core Conceptual Document
    goal: Create the foundational document explaining the agentic work loop model
    tasks:
      - id: TASK-1-1
        description: Create docs/agentic-work-loop.md with overview and three-layer model introduction
        acceptance: Document exists with clear introduction explaining the conceptual framework
        files:
          - docs/agentic-work-loop.md
      - id: TASK-1-2
        description: Write the Agentic Feedback Loop section covering work/review/address-feedback cycle
        acceptance: Section explains each phase of the inner loop with concrete examples
        files:
          - docs/agentic-work-loop.md
      - id: TASK-1-3
        description: Write the Human Feedback Loop section covering review gates and approval
        acceptance: Section explains when and how humans enter the loop
        files:
          - docs/agentic-work-loop.md
      - id: TASK-1-4
        description: Write the Complete Pipeline section with phase diagram
        acceptance: ASCII diagram shows full pipeline with both loop types marked
        files:
          - docs/agentic-work-loop.md
      - id: TASK-1-5
        description: Write the Why This Works section explaining quality rationale
        acceptance: Section provides clear rationale for the workflow model
        files:
          - docs/agentic-work-loop.md
      - id: TASK-1-6
        description: Write the Scaling Through Delegation section as forward-looking placeholder
        acceptance: Section acknowledges future capability without over-specifying
        files:
          - docs/agentic-work-loop.md
  - id: 2
    name: Update Documentation Index and Cross-References
    goal: Integrate the new document into the documentation structure
    tasks:
      - id: TASK-2-1
        description: Update docs/index.md to add the new document in the Strategy section
        acceptance: New entry appears in Strategy table with appropriate description
        files:
          - docs/index.md
      - id: TASK-2-2
        description: Update docs/guides/sdlc-pipeline.md introduction to reference the conceptual doc
        acceptance: Opening section includes reference to agentic-work-loop.md
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-2-3
        description: Add cross-reference from docs/collaboration-effectiveness.md
        acceptance: Related documentation section connects the two docs
        files:
          - docs/collaboration-effectiveness.md
  - id: 3
    name: Final Review and Consistency Check
    goal: Ensure documentation is consistent and complete
    tasks:
      - id: TASK-3-1
        description: Review all modified files for consistent terminology and style
        acceptance: Terminology matches existing docs
        files:
          - docs/agentic-work-loop.md
          - docs/index.md
          - docs/guides/sdlc-pipeline.md
          - docs/collaboration-effectiveness.md
      - id: TASK-3-2
        description: Verify all internal links work correctly
        acceptance: All markdown links resolve to existing files
        files:
          - docs/agentic-work-loop.md
      - id: TASK-3-3
        description: Ensure ASCII diagrams render correctly in GitHub markdown
        acceptance: Diagrams display properly when viewed raw on GitHub
        files:
          - docs/agentic-work-loop.md
```

---

*Authored-by: egg*
