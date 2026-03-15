# Analysis: Comment Hider Improperly Hiding Issue Comments

> Issue: #363 | Phase: refine

## Problem Statement

In issue #359, the SDLC pipeline's review phase improperly hid/minimized comments that should have been preserved. Specifically, the analysis document posted to the issue was minimized (collapsed) when it should have remained visible.

The comment hider logic was designed for PR workflows where status comments should be hidden to reduce clutter, but it's being applied too broadly to issue comments in the SDLC pipeline, resulting in substantive content (like analysis documents) being hidden.

**Current state:** Comment minimization runs on issue comments in the SDLC pipeline using patterns that can match substantive content.

**Desired outcome:** Comment hiding should:
1. Only apply to PR workflows (not issue workflows in the refine/plan phases)
2. Hide only status/notification comments, not substantive analysis or review content
3. Review bots should hide their prior reviews
4. Fixer bots should hide their old status comments

## Current Behavior

The codebase has comment minimization logic in 7 workflow files using GitHub's GraphQL `minimizeComment` mutation with the `OUTDATED` classifier. Here's how it currently works:

### Where Comment Hiding is Implemented

| Workflow | Location | Pattern Matched |
|----------|----------|-----------------|
| `sdlc-pipeline.yml` | Lines 229-243 | `SDLC Pipeline\|phase completed\|phase encountered` |
| `sdlc-pipeline.yml` | Lines 593-607 | `SDLC Pipeline\|phase completed\|phase encountered\|Pull request ready` |
| `sdlc-pipeline.yml` | Lines 983-997 | `SDLC Pipeline\|phase completed\|phase encountered\|Pull request ready\|Checks timed out\|Checks failed` |
| `sdlc-pipeline.yml` | Lines 1193-1207 | `SDLC Pipeline\|phase completed\|phase encountered` |
| `sdlc-pipeline.yml` | Lines 1914-1928 | `SDLC Pipeline\|phase completed\|phase encountered` |
| `reusable-review.yml` | Lines 383-397 | `egg <bot_name> (completed\|failed)` |
| `on-review-feedback.yml` | Lines 234-254 | `egg is addressing\|egg feedback` |
| `on-check-failure.yml` | Lines 84-103 | `egg is investigating\|egg autofix` |
| `on-mention.yml` | Lines 154-171 | `egg run\|egg finished\|Working on it` |
| `on-merge-conflict.yml` | Lines 171-181, 331-341 | `egg is resolving\|egg conflict resolution` |

### The Problem with Issue #359

Looking at issue #359's comments, the comment minimization step ran on **issue comments** (not PR comments) and used a pattern that was too broad. The pattern `SDLC Pipeline|phase completed|phase encountered` matches:

1. **Intended targets**: "SDLC Pipeline initialized for this issue" (status message)
2. **Unintended targets**: Any comment containing "phase" or similar keywords

The analysis document itself starts with `# Analysis: Investigate strongdm/attractor` and contains extensive content. While this specific content shouldn't match the pattern, the underlying issue is that:

1. **Comment hiding runs on issues, not just PRs** - The SDLC pipeline operates on issues during refine/plan phases, but comment hiding was designed for PR clutter reduction
2. **Pattern matching is fragile** - Using regex patterns to identify "status-only" comments can accidentally match substantive content
3. **No distinction between comment types** - There's no semantic marker distinguishing status comments from content comments

## Constraints

### Technical Constraints
- **GitHub API limitations**: `minimizeComment` mutation hides comments but they can be expanded by users
- **Pattern matching risks**: Regex patterns can match unintended content; need precise targeting
- **Workflow structure**: Comment hiding typically runs as a step before posting new status, meaning it runs on every phase transition

### Business Constraints
- **Preserve substantive content**: Analysis documents, review feedback, and human-facing content must never be hidden
- **Reduce clutter**: Status/notification comments should still be minimized to keep discussions focused
- **Backward compatibility**: Existing PR review workflows should continue to work

### Dependencies
- SDLC pipeline phases (refine, plan, implement, pr) each have different comment contexts
- PR-based workflows (`reusable-review.yml`, `on-review-feedback.yml`) operate on PRs, not issues
- Issue-based workflows (early SDLC phases) should preserve more content

## Options Considered

### Option A: Scope Comment Hiding to PR Workflows Only

**Approach**: Remove or disable comment minimization from SDLC pipeline jobs that operate on issues (refine, plan phases). Only apply comment hiding in the PR/implement phase.

**Pros**:
- Simple to implement - remove/disable a few workflow steps
- Eliminates risk of hiding issue content entirely
- Clear separation: issues keep all comments, PRs get cleaned up

**Cons**:
- Issue threads may get cluttered with status messages over multiple cycles
- Inconsistent behavior between phases

### Option B: Use Semantic Markers to Identify Hideable Comments

**Approach**: Add HTML comment markers (e.g., `<!-- egg-status-comment -->`) to status-only comments, then only minimize comments containing this marker.

**Pros**:
- Precise targeting - only comments explicitly marked get hidden
- Self-documenting - marker indicates intent
- Future-proof - new comment types can opt-in or out of hiding

**Cons**:
- Requires updating all status comment posting steps to include the marker
- Existing unmarked comments won't be hidden (may need migration)
- More changes across multiple workflows

### Option C: Restrict Patterns and Add Negative Matches

**Approach**: Tighten regex patterns and add negative lookahead to exclude content-rich comments. For example:
```bash
select(.body | test("SDLC Pipeline initialized|phase completed")) |
select(.body | test("# Analysis|## Problem Statement|## Recommended") | not)
```

**Pros**:
- Can be done incrementally without marker changes
- Preserves existing behavior for true status comments

**Cons**:
- Negative patterns are fragile and grow over time
- Doesn't address the root cause (hiding on issues vs PRs)
- Hard to maintain as content formats evolve

### Option D: Role-Based Hiding (Review Bots Hide Reviews, Fixers Hide Status)

**Approach**: Each bot type only hides its own prior output of the same type:
- Review bots (refine reviewer, code reviewer) hide their previous reviews before posting new ones
- Fixer bots (autofixer, conflict resolver) hide their previous status comments
- Implementation agents don't hide anything on issues

Combined with semantic markers for precise targeting.

**Pros**:
- Aligns with the stated goal: "review bots hide prior reviews, fixer bots hide old comments"
- Each workflow owns its hiding logic
- Clear responsibility boundaries

**Cons**:
- Requires auditing each workflow to ensure correct behavior
- May need coordination when multiple bot types operate on the same thread

## Recommended Approach

**Option D (Role-Based Hiding with Semantic Markers)** combined with **Option A (Scope to PR Workflows)** for the SDLC pipeline.

### Rationale

1. **Addresses the root cause**: The issue is that comment hiding was applied too broadly to issue comments. By restricting SDLC pipeline comment hiding to PR-phase operations and using semantic markers, we eliminate the risk of hiding substantive content.

2. **Matches stated requirements**: The issue explicitly states "review bots should hide prior reviews and fixer bots should hide their old comments" - this is role-based hiding.

3. **Precise targeting via markers**: Using `<!-- egg-status-comment -->` or similar markers ensures only intended comments are hidden, regardless of content patterns.

4. **Incremental implementation**: Can be done in phases:
   - Phase 1: Remove comment hiding from SDLC issue-phase jobs (quick fix)
   - Phase 2: Add semantic markers to status comments across workflows
   - Phase 3: Update hiding logic to use markers instead of content patterns

### Suggested Implementation

1. **SDLC Pipeline Changes** (`.github/workflows/sdlc-pipeline.yml`):
   - Remove the "Minimize previous status comments" steps from jobs that operate on issues (init, refine, refine-review, plan phases)
   - Keep comment minimization only in PR-phase jobs (implement-pr-ready, check-impl-pr, etc.)

2. **Add Semantic Markers**:
   - Status comments (e.g., "SDLC Pipeline initialized", "Refine phase completed") should include `<!-- egg-status-comment -->`
   - Content comments (analysis, reviews, feedback) should NOT include this marker

3. **Update Hiding Logic**:
   ```bash
   # Instead of pattern matching on content:
   .[] | select(.body | contains("<!-- egg-status-comment -->")) | .node_id
   ```

4. **PR Review Workflows** (`reusable-review.yml`):
   - Already correctly scoped to PRs
   - Consider adding markers for future-proofing

## Open Questions

1. Should the refine phase status messages (e.g., "Refine phase completed. [View run logs]...") also be hidden on issues, or should they remain visible as a timeline of what happened?

2. For the review bot's reviews that get hidden, should we keep a count or summary visible (e.g., "2 previous reviews hidden") so users know prior review cycles occurred?

---

*Authored-by: egg*
