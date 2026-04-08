### Task Analysis

**Problem statement**: When a pipeline starts on a branch that has prior `.egg-state/drafts/analysis.md` and `plan.md` from earlier pipeline runs, those stale generic files persist and cause confusion. In the `issue-1553` pipeline, the phase gate reported "no analysis draft found" despite `1553-analysis.md` existing on the branch — the stale generic `analysis.md` contained unrelated content from a markdown documentation audit.

**Source context**: Issue #1559. The bug was observed in pipeline `issue-1553` (migrate to Kubernetes). The branch state confirms the problem: both `analysis.md` (stale, from a doc audit task) and `1553-analysis.md` (correct) coexist on `origin/egg/issue-1553`.

**System context**: Draft resolution flows through two code paths:
1. **Phase gate** (`pipelines.py:6737`): calls `_read_phase_draft(worktree_repo_path, phase, issue_number, pipeline_id)` → `_get_draft_path()` → returns `.egg-state/drafts/{prefix}-analysis.md`
2. **MCP enrichment** (`mcp_tools.py:793`): same `_read_phase_draft` call for `draft_content` on pending decisions

Both correctly use `_pipeline_identifier()` (`pipelines.py:216`) which prefers `issue_number` over `pipeline_id`, producing paths like `1553-analysis.md`. The agent prompt also correctly instructs writing to the prefixed path. No code reads the generic `analysis.md`.

**Technical root cause**: Two issues compound:
1. **Stale generic drafts**: Legacy `analysis.md` and `plan.md` (no issue prefix) persist on work branches from prior merges/pipelines. These were created before the prefix convention was adopted or by pipelines without issue numbers. Nothing cleans them up.
2. **Transient draft read failure**: `_read_phase_draft` returned `None` despite `1553-analysis.md` existing on the branch. Most likely cause: the `_sync_worktree_with_remote` call at line 6646 failed silently (fetch or reset didn't complete), so the worktree didn't have the agent's latest commit when the phase gate read the draft. The function returns `None` on missing file with no diagnostic logging of which path was tried.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Add stale draft cleanup at pipeline start; improve diagnostic logging in `_read_phase_draft`
- `orchestrator/tests/test_read_phase_draft.py` — Add tests for cleanup and logging

**Risks / edge cases**: Cleanup must only remove unprefixed generic files (`analysis.md`, `plan.md`), not prefixed files from prior issues on the same branch — those are harmless historical artifacts.