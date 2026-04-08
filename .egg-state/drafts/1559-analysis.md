### Task Analysis

**Problem statement**: When a pipeline starts on a branch that has prior `.egg-state/drafts/analysis.md` and `plan.md` from earlier pipeline runs, those stale generic files persist and cause confusion. In the `issue-1553` pipeline, the phase gate reported "no analysis draft found" despite `1553-analysis.md` existing on the branch — the stale generic `analysis.md` contained unrelated content from a markdown documentation audit.

**Technical root cause**: Two issues compound:
1. **Stale generic drafts**: Legacy `analysis.md` and `plan.md` (no issue prefix) persist on work branches from prior merges/pipelines. Nothing cleans them up.
2. **Transient draft read failure**: `_read_phase_draft` returned `None` with no diagnostic logging of which path was tried.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Add stale draft cleanup at pipeline start; improve diagnostic logging in `_read_phase_draft`
- `orchestrator/tests/test_read_phase_draft.py` — Add tests for cleanup and logging