# Analysis: [Issue Title]

> Issue: #[number] | Phase: refine

## Problem Statement

[Describe the problem or feature request. What is the current state? What is the desired outcome?]

## Current Behavior

[Describe how the system currently works in the relevant area. Include code references where helpful.]

## Constraints

- [Technical constraints (compatibility, performance, security)]
- [Business constraints (timeline, scope)]
- [Dependencies on other systems or features]

## Options Considered

### Option A: [Name]

**Approach**: [Brief description]

**Pros**:
- [Advantage 1]
- [Advantage 2]

**Cons**:
- [Disadvantage 1]
- [Disadvantage 2]

### Option B: [Name]

**Approach**: [Brief description]

**Pros**:
- [Advantage 1]
- [Advantage 2]

**Cons**:
- [Disadvantage 1]
- [Disadvantage 2]

## Recommended Approach

[Which option is recommended and why. Reference the option above.]

## Open Questions

For questions that require human input before proceeding, use formal HITL decisions.
The agent will generate these using `egg-contract add-decision --format markdown`.

**Multiple-choice questions** (when you need the human to pick from options):
```
egg-contract add-decision --question "Which caching strategy should we use?" \
  --options "Redis" "In-memory LRU" "File-based" --format markdown
```
This outputs markdown with checkboxes that the human can interact with directly.

**Open-ended questions** (when you need free-form input):
Include these as plain text in your analysis. The human will respond via comment.

Example open-ended questions:
- What is the expected request volume for this feature?
- Are there any constraints on third-party dependencies?

---

*Authored-by: egg*
