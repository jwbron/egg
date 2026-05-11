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

[Each open question is registered using `egg-contract add-decision` (multiple-choice)
or `egg-contract add-feedback` (open-ended). Paste the markdown output of each
registration command here. Questions written as plain text will not be seen by the
human.

For **work-decomposition decisions** on multi-part tasks, frame the question on the
slice-DAG shape, not on PR count. In egg, each slice has its own integration branch,
agent team, BRC consensus, and PR, and sibling slices in the same wave run in parallel
(see [Slice-DAG Implement Phase](../architecture/slice-dag.md)). Slice count = PR count
by construction, so annotate the PR consequence in parentheses — e.g.
`Two slices in parallel: [A] || [B+C] (2 PRs)`,
`Two slices with dependency: [A] -> [B] (2 PRs)` — rather than registering an option
list framed as "N PRs" or "N sequential PRs". The "sequential" wording is especially
wrong because the slice scheduler does not require sibling slices to serialize.]

---

*Authored-by: egg*
