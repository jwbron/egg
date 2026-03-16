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

**IMPORTANT: Every open question MUST be registered as a contract decision or feedback
item using `egg-contract`.** Do not just write questions as prose — they will not be
seen by the human unless registered.

Surface **all** uncertainties, ambiguities, and assumptions that need human input.
Do not limit yourself to a small number — every genuine ambiguity, missing requirement,
unstated assumption, or design choice that could go multiple ways should be raised here.
It is far better to ask too many questions than to proceed with incorrect assumptions.

**Multiple-choice questions** — RUN this command for each question where the human
must pick from discrete options:
```bash
egg-contract add-decision --question "Which caching strategy should we use?" \
  --options "Redis" "In-memory LRU" "File-based" --format markdown
```
Copy the markdown output into your analysis. The human can check a checkbox to select
an option. An "Other (explain in reply)" option is auto-appended.

**Open-ended questions** — EXECUTE this command for free-form questions where you need
the human to provide text answers:
```bash
egg-contract add-feedback \
  --question "What is the expected request volume?" \
  --question "Are there any constraints on third-party dependencies?" \
  --format markdown
```
This creates a dedicated comment for the human to fill in answers. They edit the
comment to add their responses and check "Submit feedback" when done. The pipeline
will resume with the feedback available in the contract.

**DO NOT:**
- Write questions as plain markdown text without running `egg-contract add-decision`
  or `egg-contract add-feedback`
- Use custom HTML comment markers like `<!-- DECISION: ... -->` instead of the
  contract CLI
- Skip registration because you think the questions are minor — register every question

---

*Authored-by: egg*
