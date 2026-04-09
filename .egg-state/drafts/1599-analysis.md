### Task Analysis

**Problem statement**: BRC history files (`.egg-state/brc-history/`) are missing from PRs, and draft files (`.egg-state/drafts/`) pollute PR diffs with non-code content.

**Source context**: Issue #1599 references #1572 (original BRC history feature) and #1593 (example PR showing both problems). The BRC consensus summary already appears in PR bodies from the message store, so the data exists — it's just not making it into the file tree. Draft files are no longer needed by PR time since BRC messages now capture that context.

**System context**: The pipeline runs phases sequentially (refine → plan → implement → PR). At each phase completion (`pipelines.py:7721-7770`), `_write_brc_history()` writes a markdown log to `.egg-state/brc-history/{id}-{phase}.md`, then `_commit_statefiles_to_worktree()` commits it, and `push_worktree_branch()` pushes. The PR phase (`pipelines.py:7404`) skips agent spawn and calls `_auto_create_pr()` directly. It assumes BRC history is already on the branch (comment at line 7426), but in practice the per-phase push can fail silently or worktree sync can lose the files.

**Technical root cause**:
1. **BRC history**: The per-phase commit+push at lines 7742-7770 is best-effort — both the commit and push have exception handlers that log warnings and continue. If either fails, the BRC history file is lost. The PR phase has no safety net to re-write them.
2. **Draft files**: There's no cleanup step for pipeline-specific drafts (`.egg-state/drafts/{id}-analysis.md`, `.egg-state/drafts/{id}-plan.md`). Only stale *generic* drafts (`analysis.md`, `plan.md`) are cleaned up by `_cleanup_stale_generic_drafts()` at worktree init.

**Files affected**:
- `orchestrator/routes/pipelines.py` — PR phase path (~line 7404): add BRC history re-write for all completed phases + draft cleanup before PR push
- `orchestrator/tests/test_brc_history.py` — tests for new PR-phase BRC history write
- New test or additions for draft cleanup

**Risks / edge cases**:
- `_write_brc_history` is idempotent (overwrites existing file), so re-writing in the PR phase is safe
- Draft cleanup must only remove files for *this* pipeline (scoped by identifier) to avoid affecting concurrent pipelines
- The `git rm` for drafts must handle files that are untracked or already removed (use `-f --ignore-unmatch`)