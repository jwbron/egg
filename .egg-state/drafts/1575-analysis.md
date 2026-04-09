### Task Analysis

**Problem statement**: The HITL phase gate shows "No draft was found on the work branch" for both analysis and plan phases, even though the agent wrote the drafts to the correct issue-specific paths on the branch.

**System context**: Draft resolution flows through two code paths:

1. **Phase gate creation** (`pipelines.py:7743`): `_read_phase_draft(worktree_repo_path, phase, issue_number, pipeline_id)` → `_get_draft_path(phase, issue_number, pipeline_id)` → constructs `<prefix>-analysis.md` using `_pipeline_identifier(issue_number, pipeline_id)`. If the file doesn't exist on the worktree filesystem, returns None, which triggers the warning message at line 7762.

2. **MCP get_status enrichment** (`mcp_tools.py:847`): Same function, called when the user polls status. Uses `resolve_worktree_path()` to find the worktree, then reads the draft.

The draft path is constructed by `_get_draft_path()` (`pipelines.py:2176`) which calls `_pipeline_identifier()` to derive a prefix (prefers `issue_number`, falls back to `pipeline_id`). The agent prompt (`_build_phase_prompt`, line 3954) tells agents to write to the same path.

**Technical root cause**: `_read_phase_draft` constructs only a single candidate path via `_get_draft_path`. If the file isn't found at that exact path, it returns None with no fallback. The single-path approach is brittle — when the worktree sync doesn't bring the file into the local worktree (e.g., fetch failure, worktree on wrong branch, or worktree already cleaned up and `resolve_worktree_path` falls back to `repo_path` where the draft doesn't exist), the draft is silently missing. There is no fallback to check the generic path (`analysis.md` / `plan.md`).

**Files affected**:
- `orchestrator/routes/pipelines.py` — `_read_phase_draft` needs fallback path resolution
- `orchestrator/tests/test_read_phase_draft.py` — new tests for fallback behavior

**Risks / edge cases**: The `_cleanup_stale_generic_drafts` function (#1559) removes unprefixed `analysis.md`/`plan.md` at pipeline start. So the generic fallback won't help in cases where cleanup already ran. The `_get_draft_path` function is also called from `_build_phase_prompt` (to tell agents where to write) — that caller must NOT be affected by the fallback logic (agents should always write to the canonical path).