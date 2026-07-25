# documenter BRC memory — issue #3596, slice-1

## Verdict: PROPOSED (README wiring correction)

- My commit: pending
- Task: task-1-13 (correct `health_checks/README.md` runtime-wiring section)
- Files: `orchestrator/health_checks/README.md`

## What I did (documenter-owned)

- Verified the claims in the BRC memory against the live code. The memory was
  **STALE** — it was written before the coder's commit `21ef085a2` which
  addressed all reviewer NACKs and actually wired the detection plane.
- **CONFIRMED FALSE (memory claim)**: "no call site invokes run_detection_plane
  from _run_runtime_tick_checks in kubernetes_monitor.py"
  - `kubernetes_monitor.py:270` calls `self._run_detection_plane(ctx, pipeline,
    pid, runner)` from `_run_runtime_tick_checks`.
  - `_run_detection_plane` (line 280) builds a snapshot via
    `snapshot_from_health_context(ctx)`, evaluates `DetectionPlane.default()`
    through `runner.run_detection_plane(snapshot, plane)`, and emits findings
    as `DETECTION_FINDING` events on the EventBus.
- **CONFIRMED FALSE (memory claim)**: "snapshot_from_health_context only
  populates phase_state and running_agents"
  - The builder now populates: `phase_state`, `running_agents` (with all
    liveness fields), `consensus`, `decision_state`, `container_transitions`
    (empty — monitor doesn't track history), `git_state`, `midturn_messages`,
    `raw.runtime`.
  - `cost_counters` and `gateway_error_counters` remain at empty defaults.
- **CONFIRMED FALSE (memory claim)**: "RunningAgent(role=str(cid), ...) puts a
  container UUID in the role field"
  - The `role=str(cid)` defect is FIXED. `_build_container_role_map()` maps
    container IDs to role names via the pipeline's phase execution state.
    `RunningAgent.role` now carries the actual role name.
- **CONFIRMED (memory claim)**: `forward_progress` detector was missing from
  the README's detector catalogue and module table.
- Corrected the README's "Runtime wiring" section to:
  1. State that the detection plane IS wired into the runtime tick (task-1-1).
  2. Describe the actual wiring path (KubernetesMonitor._run_detection_plane →
     HealthCheckRunner.run_detection_plane → EventBus → escalation).
  3. Document which snapshot fields are populated and which remain empty.
  4. Note the idempotency guarantee and best-effort failure degradation.
- Added `forward_progress` detector to the detector catalogue table and the
  "Detection-Plane Detectors" module table.
- Updated the "two conditions" note to reflect that the plane is now wired.

## If re-spawned (review/NACK handling)

- If a reviewer NACKs my README correction, re-read their reason and the
  current state of `kubernetes_monitor.py` / `detection_plane.py` / `_overseer.py`
  to verify the claim, fix, re-commit, re-propose.
- The key verifiable facts:
  - `grep -rn "run_detection_plane" orchestrator/ --include="*.py" | grep -v "def\|import\|#\|test"`
    returns TWO call sites: `kubernetes_monitor.py:270` and `kubernetes_monitor.py:304`.
  - `snapshot_from_health_context` at `detection_plane.py:514` populates
    phase_state, running_agents, consensus, decision_state, container_transitions,
    git_state, midturn_messages, and raw — but NOT cost_counters or
    gateway_error_counters.
  - `RunningAgent` at `detection_plane.py:779` now uses
    `cid_to_role.get(str(container_id), str(container_id))` — role is mapped,
    not the raw container ID.
