### Task Analysis

**Problem statement**: Agent pipeline PRs are missing `.egg-state/brc-history/` files and still showing `.egg-state/drafts/{id}-*.md` files in their diffs, despite the fixes in #1585 and #1602. Additionally, PR #1650 shows incorrect "Consensus not reached" in the BRC summary even though consensus was reached.

**Source context**: Issue #1633 provides strong evidence: two PRs (#1624, #1626) merged after #1602 still exhibit the bug. Zero commits with messages "Persist BRC history files for PR" or "Remove pipeline draft files for ..." exist on any branch. However, `_build_brc_consensus_summary` IS producing correct output for #1624 (messages exist in store, phase is correct). The comment on #1650 adds a third symptom: incorrect consensus state in the PR body.

**System context**: The PR-phase handler in `_run_pipeline` (`orchestrator/routes/pipelines.py:8201`) calls three functions in sequence before creating the PR:
1. `_rewrite_brc_history_for_pr` (line 8228) — writes BRC history files, commits via `_commit_statefiles_to_worktree`
2. `_cleanup_drafts_for_pr` (line 8236) — removes draft files via `git rm`, commits
3. Push via `spawner.gateway.push_worktree_branch` (line 8241)
4. `_auto_create_pr` (line 8254) — creates PR from the **remote branch** via GitHub API

There's also a pre-existing per-phase write: after implement completes, `_write_brc_history` (line 8546) and `_commit_statefiles_to_worktree` (line 8564) run with a push at line 8579. This is the "primary" write; the PR-phase call at line 8228 is a "safety net."

**Technical root cause**: Two suspects, likely both contributing:

**Suspect A — Silent function no-ops**: Both functions have multiple early-return paths with no INFO/WARNING logging. The entire diagnostic gap is that we cannot distinguish "function ran and committed" from "function returned early" in production logs. Key silent paths:
- `_write_brc_history` returns with only a `debug` log if message store is unavailable or messages are empty for the phase
- `_commit_statefiles_to_worktree` returns silently if glob matches are empty (line 3802) or if `git diff --cached --quiet` returns 0 (line 3830-3831)
- `_cleanup_drafts_for_pr` returns False without warning if `git rm` succeeds but nothing is staged (untracked files), then commit fails at DEBUG level (line 2478)

**Suspect B — Push failure silently swallowed**: The push at line 8241 catches all exceptions and logs an error but proceeds to create the PR anyway. Since `_auto_create_pr` creates the PR from the **remote** branch (via GitHub API, using `pipeline.branch` as the head), any unpushed local commits (BRC history, draft cleanup) won't appear in the PR diff. The `_build_brc_consensus_summary` works because it reads from the in-memory message store, not from git — explaining why the PR body has correct consensus data even when the files are missing from the branch.

**Files affected**:
- `orchestrator/routes/pipelines.py:_write_brc_history` (line 4122) — add entry/exit/early-return INFO logging
- `orchestrator/routes/pipelines.py:_commit_statefiles_to_worktree` (line 3755) — add logging for glob matches, staged status, commit result
- `orchestrator/routes/pipelines.py:_rewrite_brc_history_for_pr` (line 4193) — add entry/exit INFO logging
- `orchestrator/routes/pipelines.py:_cleanup_drafts_for_pr` (line 2401) — add entry/exit INFO logging, promote commit failure from DEBUG to WARNING
- `orchestrator/routes/pipelines.py` PR-phase handler (line 8228-8252) — add push outcome logging with commit count
- `orchestrator/tests/test_pr_phase_brc_rewrite.py` — add integration test for call chain

**Risks / edge cases**: Logging additions are low-risk (executed once per pipeline at PR time, not in hot loops). No behavioral changes to production code — only log level promotions and new log statements.