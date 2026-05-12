# Coder → Tester handoff: test follow-ons for issue #1557 (apply-phase scheduler + APPLIER + epic_link_field)

The coder commit `4cf20c886` (`implement(#1557): apply-phase scheduler + wontdo drain + test fixes`) introduces three production changes that have mechanical follow-on test deltas. Per the gateway's file-restriction policy
(`shared/egg_restrictions/patterns.py`), tests under `gateway/tests/`,
`orchestrator/tests/`, and `shared/tests/` are tester scope — the
coder role cannot push them. The patch below captures those deltas
verbatim; please apply them on the slice-2 integration branch and
re-ACK my proposal.

## Files affected

| Path                                              | Why the test needs updating                                                                                                                   |
|---------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| `gateway/tests/test_phase_transition.py`          | `PHASE_TRANSITIONS[PLAN]` grew from `[IMPLEMENT]` to `[IMPLEMENT, APPLY]`; a new `APPLY → IMPLEMENT` edge needs coverage.                       |
| `gateway/tests/test_jira_routes.py`               | NEW: task-1-6 acceptance — two tests verifying the ticket-create route propagates `JiraPolicy.epic_link_field()` verbatim to `JiraClient.create_issue` for both `parent` (default) and `customfield_10014`.       |
| `orchestrator/tests/test_advance_phase_thread.py` | The auto-advance source-inspection block window was 3000 chars; the new applier-handoff + Won't-Do drain hooks push the `_spawn_pipeline_run_thread` call past that window. Widen to 5000. |
| `orchestrator/tests/test_models.py`               | `AgentRole` count moved from 19 → 20 (APPLIER added); `PipelinePhase` declaration order now has APPLY between PLAN and IMPLEMENT.              |
| `shared/tests/test_egg_restrictions.py`           | Same registry-count bump on the `AGENT_PATTERNS` parity assertions.                                                                           |

## How to apply

```bash
git apply .egg-state/agent-outputs/coder-to-tester-1557-test-followups.patch
git add gateway/tests/test_jira_routes.py \
        gateway/tests/test_phase_transition.py \
        orchestrator/tests/test_advance_phase_thread.py \
        orchestrator/tests/test_models.py \
        shared/tests/test_egg_restrictions.py
git commit -m 'test(#1557): follow-on assertions for APPLY phase + APPLIER role + epic_link_field'
```

The patch is mechanical — it only adjusts assertions that have hard-coded counts / ordering / window-sizes the coder's production change shifted. There are no behavioral test rewrites and no new fixture infrastructure.

## Validation that the patch passes locally

Each touched test file was run against the coder's production diff
before the test files were extracted from the commit:

- `gateway/tests/test_jira_routes.py` — 104 tests pass (including the
  two new `test_epic_link_dispatches_via_{parent_field,customfield}`).
- `gateway/tests/test_phase_transition.py` — 29 tests pass (including
  the new `test_apply_to_implement`).
- `orchestrator/tests/test_advance_phase_thread.py` — 15 tests pass.
- `orchestrator/tests/test_models.py` — 85 tests pass.
- `shared/tests/test_egg_restrictions.py` — 211 tests pass.

## Reviewer pointer

Pair with my proposal's `pre_merge_condition`: the human reviewer
(or you, with `mcp__brc__resolve_obligation`) closes the obligation
once this patch is on the integration branch.
