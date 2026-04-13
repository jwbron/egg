### Task Analysis

**Problem statement**: The `/sdlc` monitor estimates elapsed time client-side using wall-clock tracking, which drifts when `AskUserQuestion` dialogs block the poll loop. The monitor flagged a stall at "~10 minutes" when only ~5 minutes had actually elapsed server-side — the dialog blocking time was counted as pipeline elapsed time.

**Source context**: Issue #1702 describes the problem observed during `/sdlc --short 1695`. The client-side wall-clock tracker overestimated elapsed time because `AskUserQuestion` dialogs block the poll loop but wall-clock time keeps advancing, inflating the apparent duration.

**System context**: `_handle_get_status` in `orchestrator/mcp_tools.py:849` fetches pipeline state from the internal Flask API (`/api/v1/pipelines/{task_id}`), then assembles a response dict with `current_phase`, `status`, `running_agents`, `completed_agents`, etc. The pipeline data is serialized via `Pipeline.model_dump(mode="json")`, which means `PhaseExecution.started_at` (models.py:236) and `AgentExecution.started_at` (models.py:164) are already present as ISO 8601 strings in the pipeline data — they're just not surfaced in the `get_status` response or used to compute elapsed times.

**Technical root cause**: The monitor has no server-computed timing. It tracks `phase_entered_at` locally using `Date.now()`, which includes time spent in blocking dialogs. Server-computed `elapsed_seconds` from `started_at` would give accurate timing unaffected by client-side blocking.

**Files affected**:
- `orchestrator/mcp_tools.py` — Add `phase_started_at` + `phase_elapsed_seconds` to the status response; add `elapsed_seconds` to each running agent entry
- `orchestrator/tests/test_mcp_tools.py` — Update test fixtures; add tests for the new fields

**Risks / edge cases**: `started_at` may be `None` for pending phases/agents — must handle gracefully by omitting the fields rather than returning nulls.