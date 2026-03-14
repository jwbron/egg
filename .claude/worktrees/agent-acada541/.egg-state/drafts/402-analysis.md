# Analysis: Leverage Issue Labels for SDLC Workflow

> Issue: #402 | Phase: refine

## Problem Statement

The SDLC pipeline currently uses the `egg-sdlc` label as the primary trigger for workflow execution, but the label ecosystem doesn't capture the current **state** of an issue as it progresses through the pipeline phases. Users and automation cannot easily determine at a glance whether an issue is in refine, plan, implement, or PR phase—or whether it's awaiting human approval.

**Current state**: Labels are used as triggers (`egg-sdlc`) and markers (`self-improvement`), but not as state indicators.

**Desired outcome**: Labels should reflect the current SDLC phase and approval status, enabling:
- Visual dashboard filtering by phase
- Automation queries for issues awaiting approval
- Clear status communication without reading comments

## Current Behavior

### Existing Labels
The repository currently has these labels (from `gh label list`):

| Label | Purpose | Status |
|-------|---------|--------|
| `egg-sdlc` | Pipeline trigger | Active |
| `self-improvement` | Auto-generated improvement issues | Active |
| `bug`, `enhancement`, `documentation` | Standard GitHub labels | Active |
| `dependencies`, `github_actions`, `python` | Dependabot/type labels | Active |

### Current Pipeline State Tracking
State is tracked via:
1. **Contract JSON**: `.egg-state/contracts/{issue}.json` with `current_phase` field
2. **Workflow job names**: GitHub Actions UI shows which job is running
3. **Issue comments**: Phase completion posts with approval checkboxes

**Problem**: None of these are queryable via GitHub's label-based filtering. Users cannot filter the issue list to see "all issues awaiting approval" or "all issues in implement phase."

### Relevant Code Paths
- Pipeline trigger: `.github/workflows/sdlc-pipeline.yml:41-44` listens for `egg-sdlc` label
- Label addition: `.github/workflows/sdlc-pipeline.yml:579-582` adds `egg-sdlc` to PRs
- Contract phase: `.egg-state/contracts/{issue}.json` contains `current_phase`

## Constraints

- **Label consistency**: Labels must be added/removed atomically with phase transitions to avoid stale states
- **Workflow triggers**: Adding/removing labels can trigger workflows; must avoid infinite loops
- **Concurrency**: Multiple workflow runs may attempt label changes simultaneously
- **GitHub API rate limits**: Frequent label changes contribute to rate limit consumption
- **Backward compatibility**: Existing `egg-sdlc` trigger must continue to work

## Options Considered

### Option A: Phase Labels (Mutually Exclusive)

**Approach**: Add one label per pipeline phase (`sdlc:refine`, `sdlc:plan`, `sdlc:implement`, `sdlc:pr`) plus an approval state label (`sdlc:awaiting-approval`). Only one phase label is active at a time.

**Labels to add**:
- `sdlc:refine` (color: `#c2e0c6`)
- `sdlc:plan` (color: `#bfdadc`)
- `sdlc:implement` (color: `#fef2c0`)
- `sdlc:pr` (color: `#d4c5f9`)
- `sdlc:awaiting-approval` (color: `#fbca04`)

**Implementation**:
1. When phase transitions, remove previous phase label, add new phase label
2. When HITL approval is required, add `sdlc:awaiting-approval`
3. When human approves, remove `sdlc:awaiting-approval`

**Pros**:
- Clear visual indication of current phase
- Queryable via GitHub search (`label:"sdlc:refine"`)
- Namespace prefix (`sdlc:`) groups related labels
- Approval state is independent of phase (can be in "implement" and "awaiting-approval")

**Cons**:
- More label churn (add/remove on each transition)
- Requires workflow updates to manage label lifecycle
- Stale labels if workflow fails mid-transition

### Option B: Compound State Label

**Approach**: Use a single label that encodes both phase and status (e.g., `sdlc:refine-in-progress`, `sdlc:refine-awaiting-approval`, `sdlc:plan-in-progress`).

**Labels to add** (10 total):
- `sdlc:refine-in-progress`
- `sdlc:refine-awaiting-approval`
- `sdlc:plan-in-progress`
- `sdlc:plan-awaiting-approval`
- `sdlc:implement-in-progress`
- `sdlc:implement-review`
- `sdlc:pr-ready`
- `sdlc:pr-changes-requested`

**Pros**:
- Single label captures full state
- No need to track multiple labels

**Cons**:
- Many labels (combinatorial explosion)
- Harder to query "all awaiting approval" across phases
- More labels to maintain

### Option C: Phase Labels + Suffix Modifiers

**Approach**: Use phase labels with optional modifier labels that can apply to any phase.

**Labels**:
- Phase: `sdlc:refine`, `sdlc:plan`, `sdlc:implement`, `sdlc:pr`
- Modifiers: `awaiting-approval`, `blocked`

**Pros**:
- Modifiers are reusable across phases
- Can query "awaiting-approval" independent of phase
- Fewer total labels than Option B

**Cons**:
- Two-label system slightly more complex
- Modifier labels aren't namespaced (could conflict)

## Recommended Approach

**Option A: Phase Labels (Mutually Exclusive)** with the `sdlc:awaiting-approval` modifier.

**Justification**:
1. **Clarity**: Each phase has exactly one label, making state unambiguous
2. **Queryability**: GitHub search supports `label:"sdlc:refine" label:"sdlc:awaiting-approval"`
3. **Namespace**: The `sdlc:` prefix groups labels visually in the label picker
4. **Simplicity**: 5 new labels (vs. 8-10 for Option B)
5. **Alignment with contract**: Labels mirror `current_phase` in the contract JSON

**Proposed Label Set**:

| Label | Color | Description |
|-------|-------|-------------|
| `sdlc:refine` | `#c2e0c6` (green) | Issue is in refine phase |
| `sdlc:plan` | `#bfdadc` (teal) | Issue is in plan phase |
| `sdlc:implement` | `#fef2c0` (yellow) | Issue is in implement phase |
| `sdlc:pr` | `#d4c5f9` (purple) | PR created, awaiting human merge |
| `sdlc:awaiting-approval` | `#fbca04` (orange) | Human approval required to proceed |

**Implementation Steps**:
1. Add labels to repository via `gh label create`
2. Update `sdlc-pipeline.yml` to add/remove phase labels at transitions
3. Update HITL workflows to add `sdlc:awaiting-approval` when pausing for human input
4. Add label removal logic to cleanup workflow (`on-issue-closed.yml`)
5. Document label meanings in `docs/guides/sdlc-pipeline.md`

## Open Questions

**Question 1** (multiple-choice via HITL):

Should the `egg-sdlc` trigger label be retained alongside phase labels, or replaced by the new labels?

- **Retain both**: Keep `egg-sdlc` as the trigger, add phase labels as state indicators
- **Replace**: Remove `egg-sdlc`, use `sdlc:refine` as the initial trigger
- **Other (explain in reply)**

---

**Question 2** (multiple-choice via HITL):

How should the pipeline handle label synchronization if a workflow fails mid-transition?

- **Best effort**: Label may be stale; next workflow run corrects it
- **Reconciliation job**: Periodic job syncs labels with contract state
- **Defensive check**: Each phase reads contract and corrects label before proceeding
- **Other (explain in reply)**

---

*Authored-by: egg*
