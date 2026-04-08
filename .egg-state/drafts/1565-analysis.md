### Task Analysis

**Problem statement**: SDLC pipeline reviewers miss issues that PR review bots catch because they have (1) a truncated diff view (`HEAD~10` instead of full changeset), (2) no explicit instruction to fetch producer commits into their worktree, and (3) weaker "find all issues" emphasis compared to PR bots.

**Source context**: Observed in #1563 (assertion logic bug missed) and #1561 (threading data race missed). Both caught by PR bots but not SDLC reviewers despite same model (Opus) and same review criteria.

**System context**: `_build_review_prompt()` (pipelines.py:1925) constructs reviewer instructions. For first reviews, it hardcodes `git diff HEAD~10..HEAD` (line 1969). The orchestrator knows the base branch (`pipeline.base_branch` or `get_default_branch()`). Per-agent worktrees (#1481) are created at phase start from the pipeline branch. When a producer pushes and proposes, other agents' worktrees don't have those commits — agents must fetch/merge themselves (implicitly). The BRC preamble (`_build_brc_preamble()`, line 3515) tells reviewers to "Read the actual files" but never to fetch first. The PR review bot (action/build-review-prompt.sh) gets `gh pr diff` (complete changeset) and "Find ALL issues on the first pass" emphasis.

**Technical root cause**:
1. Line 1969: `"git diff HEAD~10..HEAD"` — arbitrary truncated window instead of `origin/{base_branch}...HEAD`
2. BRC reviewer lifecycle (lines 3609-3631): no fetch+merge step before review
3. Missing "Find ALL issues" emphasis that PR bot gets at line 165 of build-review-prompt.sh

**Files affected**:
- `orchestrator/routes/pipelines.py` — thread `base_branch` and `branch` through call chain; fix diff command; add fetch+merge to BRC preamble; add "Find ALL issues" emphasis
- `orchestrator/tests/test_pipeline_prompts.py` — tests for new behavior

**Risks / edge cases**: None significant. All changes are prompt-level. `base_branch` is always available. Agents already fetch implicitly — we're making it explicit.