# Plan: Fix Duplicate Messaging in SDLC Workflow

> Issue: #403 | Phase: plan

## Summary

The plan approval message appears twice when a plan is posted to a GitHub issue: once from the agent-generated draft (per `docs/templates/plan.md` instructions) and again when the workflow appends its "Plan Phase Complete" footer. Following the analysis recommendation (Option A), this plan removes the approval section from the plan template so the workflow is the sole source of the approval section, matching the pattern already used by the analysis template.

## Implementation Phases

### Phase 1: Remove Approval Section from Plan Template

**Goal**: Eliminate the duplicate approval section by removing the instruction for agents to include it in their draft. The workflow will remain the sole source of the approval section, consistent with how the analysis template works.

**Tasks**:
- [TASK-1-1] Remove "Phase Approval" section from `docs/templates/plan.md` (lines 97-116) — Acceptance: Template ends after the structured task appendix; no approval section instructions remain
- [TASK-1-2] Verify analysis template does not have approval section (for consistency check) — Acceptance: Confirm `docs/templates/analysis.md` has no approval section (already the case)

**Dependencies**: None

**Exit criteria**: The plan template no longer instructs agents to include an approval section. The workflow's "Post final plan to issue" step (sdlc-pipeline.yml:2257-2313) continues to append the sole approval section.

### Phase 2: Clean Up Agent System Prompt (if applicable)

**Goal**: Ensure the agent's system prompt during the plan phase doesn't separately instruct it to add approval sections. The task description given to agents at runtime may include redundant instructions that need alignment.

**Tasks**:
- [TASK-2-1] Review `docs/prompts/plan.md` or similar agent prompts for approval section instructions — Acceptance: If found, remove redundant instructions; if not found, document that no change is needed
- [TASK-2-2] Search codebase for any other references to "Phase Approval" or "egg-phase-approval" in agent instructions — Acceptance: All agent-facing instructions are consistent with the updated template

**Dependencies**: Phase 1

**Exit criteria**: No agent instructions tell the agent to include an approval section in the plan draft. Only the workflow appends the approval section.

## Test Strategy

- **Unit tests**: Not applicable (documentation/template changes only)
- **Integration tests**:
  - Trigger the SDLC pipeline on a test issue and observe the plan phase
  - Verify the posted plan comment contains exactly ONE `<!-- egg-phase-approval -->` marker
  - Verify the approval checkbox is functional (checking it should trigger phase advancement)
- **Manual testing**:
  1. Create a test issue with `egg-sdlc` label
  2. Let pipeline run through refine phase to plan phase
  3. Verify the plan draft in `.egg-state/drafts/` does NOT contain an approval section
  4. Verify the posted issue comment contains exactly one approval section (from the workflow)
  5. Check the approval checkbox and verify the HITL workflow triggers correctly

## Rollback Plan

If issues arise after the template change:

1. **Immediate rollback**: Revert the template change
```bash
git revert <commit-hash> --no-edit
git push origin main
```

2. **If agents still generate approval sections** (cached behavior): This is harmless—the workflow will still append its own, resulting in two approval sections temporarily. Future runs with fresh context will use the updated template.

3. **If approval section is missing entirely**: Check that the workflow's "Post final plan to issue" step is running correctly. The workflow should always append the approval section.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Agents use cached/stale instructions | Medium | Low | Duplicates are cosmetic; new runs will use updated template |
| Breaking change to existing drafts | Low | Low | Existing drafts already committed won't be reprocessed |
| Workflow fails to append approval | Low | High | No changes to workflow; monitor first run after change |

## Migration Notes

- **Breaking changes**: None. This is a cosmetic fix for duplicate content.
- **Database migrations**: None
- **Config changes**: None
- **User impact**: Users will see cleaner plan comments with exactly one approval section instead of two.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Fix duplicate approval section in SDLC plan comments"
  description: |
    Fixes #403. The plan approval message was appearing twice in SDLC workflow
    comments because both the plan template and the workflow instructed adding
    an approval section.

    This PR removes the approval section from the plan template, making the
    workflow the sole source of the approval section. This aligns with the
    pattern already used by the analysis template.
phases:
  - id: 1
    name: Remove Approval Section from Plan Template
    goal: Eliminate duplicate approval section by removing it from the template
    tasks:
      - id: TASK-1-1
        description: Remove "Phase Approval" section from docs/templates/plan.md (lines 97-116)
        acceptance: Template ends after the structured task appendix; no approval section instructions remain
        files:
          - docs/templates/plan.md
      - id: TASK-1-2
        description: Verify analysis template does not have approval section (for consistency check)
        acceptance: Confirm docs/templates/analysis.md has no approval section
        files:
          - docs/templates/analysis.md
  - id: 2
    name: Clean Up Agent System Prompt
    goal: Ensure agent prompts don't separately instruct adding approval sections
    tasks:
      - id: TASK-2-1
        description: Review docs/prompts/plan.md or similar agent prompts for approval section instructions
        acceptance: If found, remove redundant instructions; if not found, document no change needed
        files:
          - docs/prompts/plan.md
      - id: TASK-2-2
        description: Search codebase for references to "Phase Approval" or "egg-phase-approval" in agent instructions
        acceptance: All agent-facing instructions are consistent with the updated template
        files: []
```

---

*Authored-by: egg*
