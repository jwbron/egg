# Analysis: Merge fixer bot is causing issues with PR history and introducing incorrect changes

> Issue: #427 | Phase: refine

## Problem Statement

The merge fixer bot (conflict resolver) is causing issues with PR history and introducing incorrect changes. The issue references [PR #405 review](https://github.com/jwbron/egg/pull/405#pullrequestreview-3771357980), which identified that the conflict resolver may not be correctly or intelligently solving merge conflicts.

**Current state**: The conflict resolver triggers when PRs develop merge conflicts with `main`, runs an LLM agent to resolve the conflicts via `git rebase`, and force-pushes the result.

**Desired outcome**: The conflict resolver should correctly identify and resolve auto-resolvable conflicts while escalating truly ambiguous or risky conflicts to human review, without corrupting branch state or introducing incorrect changes.

## Current Behavior

The conflict resolver workflow (`on-merge-conflict.yml` → `reusable-conflict-resolve.yml`) operates as follows:

1. **Detection**: Triggers on push to main, every 2 hours, or manual dispatch
2. **Conflict scan**: Queries all open PRs for `mergeable_state == "dirty"`
3. **Prompt building**: Runs `action/build-conflict-prompt.sh` from `main` (trusted)
4. **Resolution**: Agent executes rebase workflow:
   - `git fetch origin main`
   - `git rebase origin/main`
   - Resolve conflicts based on rules
   - `git push --force-with-lease`
5. **Escalation**: If conflicts require human judgment, agent aborts and posts comment

### Key Code References

- **Workflow**: `.github/workflows/reusable-conflict-resolve.yml:179-191`
- **Prompt builder**: `action/build-conflict-prompt.sh:69-107`
- **Conventions**: `action/conflict-conventions.md`

### Current Resolution Rules (from `action/build-conflict-prompt.sh`)

**Auto-resolvable**:
- Lock files (regenerate)
- Additive changes (both sides add different content)
- Formatting conflicts (whitespace, import order)
- Version bumps

**Escalate to human**:
- Semantic conflicts (both sides modify the same logic differently)
- Breaking API changes
- Security-sensitive code
- Database migrations
- Configuration conflicts affecting production

## Constraints

### Technical Constraints
- **Rebase rewrites history**: Force-push is required, which can confuse reviewers tracking PR progress
- **Race conditions**: Between conflict detection and resolution, the branch may change
- **Limited context**: The agent has access to the files but may lack understanding of intent behind changes
- **Test coverage**: Not all incorrect merges will fail tests (semantic bugs may pass CI)

### Operational Constraints
- **Fork PRs cannot be resolved**: Bot cannot push to fork branches
- **Gateway blocks merges**: Only force-push to egg-owned branches is allowed
- **No human in the loop**: Once triggered, resolution happens autonomously

### Dependencies
- Anthropic API (Claude)
- GitHub API (PR metadata, check status, comments)
- Gateway sidecar (git operations)
- CI checks (lint, test, build) for verification

## Options Considered

### Option A: Conservative Escalation with Semantic Analysis

**Approach**: Enhance the agent's decision-making to be more conservative, escalating more conflicts to human review. Add pre-resolution analysis that examines the semantic nature of changes before attempting resolution.

Implementation:
1. Before attempting resolution, have the agent analyze both sides of each conflict
2. Classify conflicts into categories: trivial (whitespace, imports), additive (new code), semantic (logic changes)
3. Only auto-resolve trivial and clearly additive conflicts
4. Escalate anything with logic overlap to human review
5. Add a dry-run mode that shows what resolution would look like without pushing

**Pros**:
- Reduces risk of incorrect resolutions significantly
- Maintains automation for low-risk conflicts
- Minimal workflow changes required
- Human stays in control for ambiguous cases

**Cons**:
- May escalate too many conflicts, increasing human workload
- Classification heuristics may be imperfect
- Doesn't address history rewrite concerns

### Option B: Merge-based Resolution (No Rebase)

**Approach**: Switch from rebase to merge-based conflict resolution. Create a merge commit instead of rewriting history.

Implementation:
1. Replace `git rebase` with `git merge origin/main`
2. Resolve conflicts during merge
3. Regular push (no force) with the merge commit
4. History preserved, reviewers see clear conflict resolution commit

**Pros**:
- Preserves commit history (no force-push)
- Clear audit trail of conflict resolution
- Less risk of losing commits
- Simpler mental model for reviewers

**Cons**:
- Creates merge commits that some teams dislike
- May create messy history with many merge commits
- Doesn't fully address incorrect resolution problem
- GitHub's "squash and merge" negates merge history anyway

### Option C: Human-Gated Resolution with Preview

**Approach**: Never auto-push resolutions. Instead, create a preview branch and request human approval before force-pushing.

Implementation:
1. Resolve conflicts on a temporary branch (e.g., `egg/conflict-preview-<pr>`)
2. Create a draft PR or comment showing the diff between original branch and resolved branch
3. Wait for human approval (checkbox, comment, or review)
4. Only after approval, force-push to the original branch
5. Clean up preview branch

**Pros**:
- Human always reviews before destructive action
- Full visibility into what changes the bot made
- Catches incorrect resolutions before they land
- Maintains trust in the automation

**Cons**:
- Adds latency (human must approve)
- Creates extra branches/comments to manage
- May not scale if many PRs have conflicts
- Complexity in tracking approval state

### Option D: Improved Verification with Rollback Capability

**Approach**: Keep the current flow but add stronger verification and rollback mechanisms.

Implementation:
1. Before resolution, save current branch state as a backup tag
2. After resolution, run expanded verification (beyond just tests)
3. If verification fails or specific patterns detected, auto-rollback
4. Add comment explaining what happened and why rollback occurred
5. Provide one-click mechanism to restore from backup

**Pros**:
- Safety net for failed resolutions
- Current workflow mostly unchanged
- Easy recovery from mistakes
- Can learn from rollbacks to improve rules

**Cons**:
- Reactive rather than proactive
- Bad resolutions may still temporarily break things
- Backup tags can accumulate
- Doesn't prevent incorrect resolutions, just recovers from them

## Recommended Approach

**Option A: Conservative Escalation with Semantic Analysis** is recommended as the primary change, combined with elements from **Option D** (backup/rollback capability).

### Justification

1. **Addresses the core problem**: The issue is incorrect resolutions. Being more conservative about what gets auto-resolved directly tackles this.

2. **Proportional response**: Rather than fundamentally changing the workflow (rebase → merge) or adding blocking human gates for all conflicts, this adds intelligence to the existing flow.

3. **Defense in depth**: Combining conservative escalation with backup/rollback provides two layers of protection.

4. **Reversible**: If the new heuristics are too conservative, the thresholds can be adjusted. No architectural change required.

### Recommended Implementation

1. **Phase 1: Conservative Classification**
   - Modify `action/build-conflict-prompt.sh` to emphasize conservative resolution
   - Add explicit instruction to escalate when in doubt
   - Expand the "escalate" category to include any file with more than trivial changes

2. **Phase 2: Pre-Resolution Analysis**
   - Before attempting rebase, agent should read and summarize what each side of the conflict does
   - If both sides touch the same functions/classes, escalate
   - Only proceed with auto-resolution if the conflict is clearly mechanical

3. **Phase 3: Backup and Verification**
   - Before resolution, create a backup tag: `git tag backup/conflict-<pr>-<sha> HEAD`
   - After resolution, verify that:
     - No source files were deleted that shouldn't be
     - No unexpected files were modified
     - The diff is "reasonable" in size
   - If verification fails, rollback and escalate

4. **Phase 4: Improved Escalation Comments**
   - When escalating, provide actionable information:
     - Show the conflicting hunks
     - Explain what each side is trying to do
     - Suggest resolution approach for human

## Open Questions

### Questions for Human Input

1. **Preferred merge strategy**: Should we switch from rebase to merge commits for conflict resolution, or keep the rebase approach?

2. **Escalation threshold**: When conflicts are escalated, should the PR be:
   - Left as-is (human resolves manually)?
   - Assigned a label for tracking?
   - Mentioned to specific reviewers?

3. **Backup retention**: How long should backup tags be retained before cleanup?

### Technical Questions (Can Research Further)

- Are there specific examples of incorrect resolutions we can analyze to improve rules?
- What percentage of conflicts currently get auto-resolved vs escalated?
- How often do escalated conflicts get resolved correctly by humans?

---

*Authored-by: egg*
