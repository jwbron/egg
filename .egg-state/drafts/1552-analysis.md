### Task Analysis

**Problem statement**: BRC consensus history (proposals, ACKs, NACKs, reviews) is not deterministically preserved in git history. Some PRs include agent-written review files in `.egg-state/agent-outputs/` (e.g., PR #1563), while others don't — the inclusion depends on whether agents happen to commit these files from their per-agent worktrees.

**System context**: BRC messages live in the in-memory/Redis `MessageStore` (`orchestrator/message_store.py`) and are never written to files. Separately, reviewer agents are instructed to write verdict files to `.egg-state/reviews/`, and agent outputs go to `.egg-state/agent-outputs/` via the signals route. These files live in per-agent worktrees (`/home/egg/.egg-worktrees/<pipeline_id>-<role>/<repo>/`) and only reach the shared branch if the agent commits and pushes them before exiting.

**Why review context is non-deterministic**: The orchestrator's `_commit_statefiles_to_worktree()` (`orchestrator/routes/pipelines.py:2501`) operates on the **pipeline-level worktree**, not per-agent worktrees. Files agents write in their per-agent worktrees only reach the branch if the agent explicitly commits them. Since agents are LLMs, this is non-deterministic — sometimes they `git add .egg-state/` and sometimes they don't.

**Key code paths**:
- `MessageStore` (`orchestrator/message_store.py`) — in-memory/Redis message storage with BRC message types (CONSENSUS_PROPOSE, CONSENSUS_ACK, CONSENSUS_NACK, CONSENSUS_WITHDRAW, CONSENSUS_CONFIRMED, CONSENSUS_RE_REVIEW)
- `get_message_store().get_messages(pipeline_id, limit=10000)` — retrieves all messages for a pipeline
- `_build_pr_body()` (`orchestrator/routes/pipelines.py:2834`) — builds PR title/body from contract metadata only, no BRC context
- `_commit_statefiles_to_worktree()` (`orchestrator/routes/pipelines.py:2501`) — stages and commits `.egg-state/` files matching pipeline identifier prefix
- Phase completion statefiles commit at line ~6738: `_commit_statefiles_to_worktree(worktree_repo_path, f"Persist statefiles after {current_phase.value} phase", pipeline_identifier=...)`
- PR phase block at line ~6420: auto-creates PR after pushing latest commits
- `_pipeline_identifier()` — returns `issue_number` if available, otherwise `pipeline_id`

**Proposed approach**: Have the orchestrator write BRC history deterministically at phase completion. The orchestrator has direct access to the message store and operates on the pipeline-level worktree where `_commit_statefiles_to_worktree()` runs. Write per-phase BRC history files to `.egg-state/brc-history/{identifier}-{phase}.md`. Include a BRC summary section in the PR body.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Add `_write_brc_history()` helper, call at phase boundaries and before PR creation. Add BRC summary to `_build_pr_body()`
- `orchestrator/tests/test_auto_pr.py` — Tests for new functionality