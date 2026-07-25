# documenter BRC memory — issue #3596, slice-1

## Verdict: PROPOSED (README wiring correction)

- My commit: pending
- Task: task-1-13 (correct `health_checks/README.md` runtime-wiring section)
- Files: `orchestrator/health_checks/README.md`

## What I did (documenter-owned)

- Verified the claim in README line 88: `routes/pipelines._run_overseer_detection_plane`
  builds the snapshot, evaluates the default plane, and routes findings.
  - **CONFIRMED FALSE**: `_run_overseer_detection_plane` exists in
    `orchestrator/routes/pipelines/_overseer.py:309` but has **zero call sites**
    in production code (only imported in `__init__.py`). The detection plane
    is never invoked from `_run_runtime_tick_checks` in `kubernetes_monitor.py`.
  - `HealthCheckRunner.run_detection_plane()` exists in `runner.py:159` but is
    also never called from the runtime tick.
  - All 27 registered detectors are starved in production — they only fire
    when driven by the calibration corpus in tests.
- Verified `snapshot_from_health_context` in `detection_plane.py:511`:
  - Populates only `phase_state` (5 fields) and `running_agents` (3 of 7 fields).
  - Does NOT populate: `container_transitions`, `git_state`, `decision_state`,
    `cost_counters`, `gateway_error_counters`, `midturn_messages`, `raw.*`,
    `RunningAgent.last_tool_call_age_s`, `RunningAgent.last_heartbeat_age_s`,
    `RunningAgent.exit_code`, `RunningAgent.exit_reason`.
  - **CONFIRMED the `role=str(cid)` defect**: `RunningAgent(role=str(cid), ...)`
    puts a container UUID in the role field, not a role name.
- Corrected the README's "Runtime wiring" section to:
  1. State clearly that the detection plane is NOT yet wired into the runtime tick.
  2. Describe the intended wiring path (KubernetesMonitor → HealthCheckRunner →
     run_detection_plane → EventBus → escalation).
  3. Document that snapshot_from_health_context only populates phase_state and
     running_agents, leaving most fields at empty defaults.
  4. Note that task 1a wires the plane and tasks 1b-g enrich the snapshot builder.
- Updated the detector catalogue note to state both conditions (plane wired +
  snapshot populated) must hold for a detector to fire in production.

## If re-spawned (review/NACK handling)

- If a reviewer NACKs my README correction, re-read their reason and the
  current state of `kubernetes_monitor.py` / `detection_plane.py` / `_overseer.py`
  to verify the claim, fix, re-commit, re-propose.
- The key verifiable facts:
  - `grep -rn "run_detection_plane" orchestrator/ --include="*.py" | grep -v "def\|import\|#\|test"`
    returns zero call sites.
  - `snapshot_from_health_context` at `detection_plane.py:511` only sets
    `phase_state` and `running_agents`.
  - `RunningAgent(role=str(cid), ...)` at `detection_plane.py:536` uses
    container ID as role.
