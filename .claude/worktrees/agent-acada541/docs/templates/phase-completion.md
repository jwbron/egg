# Phase Completion Comment Template

Use this format when posting a phase completion comment to GitHub.
The approval checkbox uses the `<!-- egg-phase-approval -->` marker
which the orchestrator's decision queue detects when edited.

## Template

```markdown
## [Phase Name] Complete

[Brief summary of what was accomplished in this phase]

### Deliverables

- [Deliverable 1]
- [Deliverable 2]

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to next phase

---

*Authored-by: egg*
```

## Notes

- The `<!-- egg-phase-approval -->` marker must appear on the line immediately before the approval checkbox
- When the human checks the `[x] Approve` checkbox, the workflow detects the edit and advances to the next phase
- Keep the approval checkbox as a single option to avoid confusion
- The phase name in the heading should match the current SDLC phase (refine, plan, implement)

## Example: Refine Phase Complete

```markdown
## Refine Phase Complete

Analysis of issue #123 is complete. The recommended approach is to implement
Option B (Redis caching) based on the constraints identified.

### Deliverables

- Problem statement and constraints documented
- Three options analyzed with pros/cons
- Recommendation provided with justification

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to plan phase

---

*Authored-by: egg*
```
