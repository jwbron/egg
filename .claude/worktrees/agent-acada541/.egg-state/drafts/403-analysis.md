# Analysis: Duplicate Messaging in SDLC Workflow

> Issue: #403 | Phase: refine

## Problem Statement

The plan approval message is appearing twice in SDLC workflow comments. When a plan is posted to a GitHub issue, the comment contains two separate `<!-- egg-phase-approval -->` markers with approval checkboxes, confusing users and creating potential workflow issues.

**Current state**: Plan phase comments contain duplicate approval sections.
**Desired outcome**: Plan phase comments should contain exactly one approval section.

## Current Behavior

Looking at the example from [issue #363 comment](https://github.com/jwbron/egg/issues/363#issuecomment-3869009749), the posted plan comment contains:

1. **First approval section** (from agent-generated draft at the end of the plan content):
   ```markdown
   ## Phase Approval

   <!-- egg-phase-approval -->
   - [ ] Approve and advance to implement phase
   ```

2. **Second approval section** (appended by the workflow):
   ```markdown
   ## Plan Phase Complete

   The implementation plan above has passed internal review and is ready for human approval.

   [View run logs](...)

   ### Ready for Review

   <!-- egg-phase-approval -->
   - [x] Approve and advance to implement phase
   ```

### Root Cause

The duplication occurs because of conflicting instructions between:

1. **Agent instructions** (in `docs/templates/plan.md`, lines 99-112): Tell the agent to include the approval section at the end of the plan draft.

2. **Workflow logic** (in `.github/workflows/sdlc-pipeline.yml`, lines 2269-2289): The "Post final plan to issue" step reads the draft content and unconditionally appends its own "Plan Phase Complete" footer with a second approval section.

The agent draft files (e.g., `.egg-state/drafts/385-plan.md`) contain the approval section per the template instructions. Then the workflow appends another one.

**Code reference**: The workflow's "Post final plan to issue" step at `sdlc-pipeline.yml:2257-2313` concatenates the draft content with a hardcoded footer containing `<!-- egg-phase-approval -->`.

## Constraints

- **Technical**: The `<!-- egg-phase-approval -->` marker is used by `sdlc-hitl.yml` to detect approval checkbox changes. The workflow checks for `contains(github.event.comment.body, '<!-- egg-phase-approval')` - having two markers doesn't break this, but it's confusing.
- **Consistency**: The refine phase has the same pattern but doesn't suffer the same issue because the analysis template (`docs/templates/analysis.md`) does NOT include an approval section - only the workflow appends one.
- **Backwards compatibility**: Existing draft files in `.egg-state/drafts/` already contain approval sections and would need updating if we change the template.

## Options Considered

### Option A: Remove Approval Section from Plan Template

**Approach**: Update `docs/templates/plan.md` to remove the "Phase Approval" section (lines 97-116). The workflow would be the sole source of the approval section.

**Pros**:
- Consistent with analysis template (which doesn't include approval section)
- Single source of truth for approval section formatting
- Workflow has control over the approval message and run logs link

**Cons**:
- Requires updating template documentation
- Agents may still include approval sections based on cached/outdated instructions
- Minor: existing draft files would have stale approval sections (low impact - only affects re-review scenarios)

### Option B: Remove Workflow Footer, Keep Agent-Generated Approval

**Approach**: Modify the workflow to NOT append the "Plan Phase Complete" footer. The agent-generated approval section would be the only one.

**Pros**:
- Keeps approval section close to the plan content where it's written
- Agent has full control over the comment format

**Cons**:
- Loses the "Plan Phase Complete" status message and run logs link
- Inconsistent with refine phase (which relies on workflow to add footer)
- Would need to update agent instructions to include the run logs link

### Option C: Workflow Strips Existing Approval Before Appending

**Approach**: Modify the workflow to detect and remove any existing `<!-- egg-phase-approval -->` section from the draft content before appending its own footer.

**Pros**:
- Handles both properly-formatted drafts and legacy drafts
- Workflow retains control over final approval section format
- No template changes needed

**Cons**:
- More complex logic in workflow (regex/sed to strip the section)
- Fragile - depends on matching specific markdown patterns
- Harder to maintain

### Option D: Conditional Footer Based on Marker Detection

**Approach**: Modify the workflow to check if the draft already contains `<!-- egg-phase-approval -->`. If present, only append a brief status line. If absent, append the full footer.

**Pros**:
- Handles both cases gracefully
- Simpler than stripping (just a conditional check)
- Backwards compatible with existing drafts

**Cons**:
- Still leaves two slightly different footer formats in the codebase
- Plan drafts with approval sections would lack the "Plan Phase Complete" heading

## Recommended Approach

**Option A: Remove Approval Section from Plan Template** is recommended.

Rationale:
1. **Consistency**: This aligns the plan phase with the refine phase, where the template does not include approval sections and the workflow adds them.
2. **Single source of truth**: The workflow controls the approval section format, ensuring consistency across all phases.
3. **Simplicity**: No complex detection or stripping logic required in the workflow.
4. **Maintainability**: Changes to approval section format only need to happen in one place (the workflow).

Implementation is straightforward:
1. Update `docs/templates/plan.md` to remove lines 97-116 (the "Phase Approval" section)
2. Optionally update agent prompts if they explicitly reference adding approval sections

The refine template already follows this pattern and doesn't have the duplication issue.

## Open Questions

1. **Should existing draft files be cleaned up?** The drafts in `.egg-state/drafts/` that contain approval sections are already committed. They're unlikely to be reprocessed, but for completeness, should they be updated to remove the approval sections?

2. **Are there other templates or prompts that instruct agents to add approval sections?** The system prompt given to agents during the plan phase may need review to ensure it doesn't contradict the updated template.

---

*Authored-by: egg*
