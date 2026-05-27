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

Register questions about *what the problem is* and *what's in or out of scope* —
facts only the operator can answer (product intent, scope boundaries, external
commitments, user-visible behavior). Do **not** register decisions about:

- **Work decomposition / slice-DAG shape / PR packaging** — these belong to the
  plan phase. The **architect** owns the slice-DAG shape (#2809) (see
  [Slice-DAG Implement Phase](../architecture/slice-dag.md)) and the operator
  approves it at the plan HITL gate. If the task spans multiple independently-
  implementable parts, name them in `## Problem Statement` or `## Constraints`
  as advisory context — the architect will propose a slice shape from that.
- **Implementation strategy** the planner can derive from the analysis (migration
  approach, fallback design, detector shape). Surface these as Options Considered
  / Recommended Approach, not as `add-decision` items.
- **API / schema details** the planner will work out during design. If the operator
  must constrain the API shape, state it as a `## Constraints` entry.]

---

*Authored-by: egg*
