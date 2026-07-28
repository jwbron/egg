# Documenter BRC memory — issue #3665, slice-1

## Verdict: NO-OP PROPOSED (waiting for coder's slice-1 implementation)

- **My proposal:** No-op (version 1). The documenter has no code changes to make
  in slice-1 — the contract has no documenter-specific tasks. The documenter's
  work (updating `orchestrator/CLAUDE.md` decomposition seams and
  `health_checks/README.md` detector documentation) depends on the coder's
  implementation of snapshot field population.
- **Waiting on:** coder's proposal for slice-1 (snapshot field population in
  `snapshot_from_health_context()`).
- **Worktree state:** clean — no uncommitted changes.

## What I'll do when the coder proposes

Slice-1 populates 5 in-scope `EventStreamSnapshot` fields in
`snapshot_from_health_context()` (in `orchestrator/health_checks/detection_plane.py`):

1. `midturn_messages` (TASK-1-1) — from agent tool-call logs
2. `runtime` (TASK-1-2) — from `driver_heartbeat.tick_age_seconds()` / `spawn_age_seconds()`
3. `consensus` (TASK-1-3) — from `peer_consensus.get_peer_consensus_tracker().evaluate()`
4. `container_transitions` (TASK-1-4) — from `kubernetes_monitor` pod-state log
5. `RunningAgent` role+age fields (TASK-1-5) — fix role from container ID to agent role,
   populate `last_tool_call_age_s` / `last_heartbeat_age_s`

### Documenter deliverables (once coder proposes):

- **`orchestrator/CLAUDE.md`**: If any slice-1 files get decomposed (they're all under
  the 1,000-code-line cap, so decomposition is unlikely), add a "Decomposition seams"
  entry. Otherwise, no CLAUDE.md change needed.
- **`orchestrator/health_checks/README.md`**: Update the `EventStreamSnapshot` type
  documentation to reflect the 5 newly-populated fields. Update the detector catalogue
  if any new detectors are registered. Update the "Adding a new detector" section if
  the registration path changes.
- **`docs/guides/pipeline-health-monitoring.md`**: If the snapshot fields change the
  monitoring story, update the relevant sections.

### Files to watch (from the plan):

- `orchestrator/health_checks/detection_plane.py` — `snapshot_from_health_context()`
- `orchestrator/driver_heartbeat.py` — runtime section population
- `orchestrator/peer_consensus/__init__.py` — consensus section population
- `orchestrator/kubernetes_monitor.py` — container_transitions
- `orchestrator/health_monitor.py` — RunningAgent age fields
- `orchestrator/agent_log_store.py` — midturn_messages source

## If re-spawned (review/NACK handling)

- If a reviewer NACKs my no-op proposal, re-read their reason and respond.
- If asked to ACK a peer producer (coder/tester), that's a reviewer action — not my
  role here (EGG_BRC_REVIEWERS=reviewer_contract,reviewer_code; I'm a producer).
