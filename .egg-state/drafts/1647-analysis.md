### Task Analysis

**Problem statement**: When a pipeline run completes refine+plan but fails during implement, there's no clean way to resubmit using the prior run's plan and analysis artifacts. These documents can be 50-80KB — too large to pass through the MCP tool interface practically (consumes context window tokens and may hit transport limits). Users are forced to fall back to Python scripts calling the REST API directly.

**Source context**: Issue #1647, referencing #1570 where runs v3 produced high-quality artifacts on branch `egg/issue-1570-v3` that couldn't be reused for v5 without manual workarounds. The 55KB plan document was the main blocker.

**System context**: The `submit_task` MCP tool (`orchestrator/mcp_tools.py:612`) accepts inline `plan` and `analysis` string parameters. These get passed through `POST /api/v1/pipelines` (`pipelines.py:620`) → `StateStore.create_pipeline()` (`state_store.py:720`) → stored on the `Pipeline` model (`models.py:468-478`). During `_run_pipeline()` (`pipelines.py:7095`), if `pipeline.plan`/`pipeline.analysis` are set, they're written to `.egg-state/drafts/{prefix}-plan.md` and `{prefix}-analysis.md` in the per-pipeline worktree (line 7370-7406), then `_populate_contract_from_plan()` (line 6758) parses the plan's yaml-tasks appendix to populate the contract. The prefix is derived from `_pipeline_identifier()` — `issue_number` when present, else `pipeline_id`.

**Technical root cause**: The MCP tool interface serializes all parameters as inline strings. The `plan` field on Pipeline accepts up to 200KB, but Claude Code's MCP transport makes passing 50-80KB inline impractical. The artifacts already exist on a prior run's branch (`.egg-state/drafts/` directory), but there's no mechanism to reference them by branch.

**Additional requirement**: `create_pipeline` currently returns 409 if the target branch exists on remote, even after the prior pipeline was cancelled. The check should be relaxed: only 409 when both the branch exists AND an active pipeline with that ID exists. If the branch exists but the pipeline is terminal/missing, allow branch reuse.

**Files affected**:
- `orchestrator/models.py:468` — add `source_branch` field to Pipeline model
- `orchestrator/mcp_tools.py:63-113` — add `source_branch` to tool schema
- `orchestrator/mcp_tools.py:612-717` — pass `source_branch` through handler
- `orchestrator/routes/pipelines.py:620-816` — accept and forward `source_branch` in REST handler; relax branch-exists check (lines 687-715)
- `orchestrator/state_store.py:720-818` — accept `source_branch` in `create_pipeline()`
- `orchestrator/routes/pipelines.py:~7348` — add artifact reading from source branch before contract creation block
- Tests for the new functionality

**Key functions in the flow**:
- `_handle_submit_task()` at `mcp_tools.py:612` — MCP handler, builds REST payload
- `create_pipeline()` at `pipelines.py:620` — REST handler, validates + creates pipeline
- `StateStore.create_pipeline()` at `state_store.py:720` — persists Pipeline model
- `_run_pipeline()` at `pipelines.py:7095` — worktree creation, draft writing, contract population
- `_populate_contract_from_plan()` at `pipelines.py:6758` — parses yaml-tasks from plan
- `_pipeline_identifier()` at `pipelines.py:296` — derives prefix for .egg-state filenames
- `_get_draft_path()` at `pipelines.py:2254` — returns relative path to draft file
- `_draft_filename()` at `pipelines.py:2240` — maps phase name to filename (refine→analysis.md, plan→plan.md)
- `_sync_worktree_with_remote()` at `pipelines.py:3245` — fetches remote state into worktree (runs before contract creation, makes origin refs available)