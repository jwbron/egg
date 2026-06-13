# BRC memory — architect (issue-3064, plan phase)

## My proposal (v1)
- Artifacts: `.egg-state/agent-outputs/3064-architect-output.json`, `.egg-state/agent-outputs/3064-architect-slices.yaml`
- Design: Option B per HITL cq-1 — EGG_EVENT_LOOP_OWNER flag (default pod), one-shot wrapper arm,
  orchestrator event loop + deduped on-demand spawner (propose|ack|nack only; confirm/complete
  orchestrator-side, no pod), #3138-mirrored bounded respawn supervision (HITL cq-2), worktree
  re-attach + session reuse, lifecycle-aware monitors, docs + flip follow-up package.
- Slice DAG: single serialized chain 1->2->3->4->5->6 (file overlap on kubernetes_spawner.py /
  event-loop module forces serialization; slice 6 docs at the tail).
- Key invariants I will defend in review:
  - #3023 hard constraint: flag defaults to pod; guard env only set by the spawner on its own Jobs;
    pod-mode wrapper byte-identical (golden-file test).
  - Dedupe key = sha256(pipeline, slice, phase, role, action, event-identity); Job label + in-memory
    set; stateless restart re-derivation (#2761), no persisted spawn bookkeeping.
  - Supervision keys ONLY on Job/exit-code failure, never on BRC outcomes (NACK is not a failure).
  - Flip/in-pod-loop deletion is OUT of this pipeline — packaged follow-up per operator directive.

## Peer state
- No peer proposals reviewed yet (first invocation, 2026-06-12).
