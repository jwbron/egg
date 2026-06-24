# Analysis: Orchestrator-driven on-demand agent spawning — lift the event pump out of the pod

> Issue: #3229 | Phase: refine | Pipeline: issue-3229
> **Correction (v2):** the issue body's "nothing from #3064 is on main; clean
> re-run" premise is factually false. The full mechanism #3229 describes is
> already merged on main behind `EGG_EVENT_LOOP_OWNER` (default `pod`). This
> analysis is reconciled against that reality. The adopt-vs-reimplement
> conflict is registered as HITL decision **cq-1** and must be resolved by the
> operator before plan.

## ⚠️ Blocking premise conflict — resolve before plan (HITL cq-1)

Both refine reviewers NACked v1 on the same load-bearing fact, and they are
correct. The issue body (lines 226–228) and the contract `task_description`
("## History") both state:

> "#3064's … work branch ended even with main. **Nothing from either attempt is
> on main; this is a clean re-run.**"

This is **contradicted by `origin/main` @ `74838edb4`.** All six #3064 slices are
**merged**:

| Slice | PR | What landed |
|-------|-----|-------------|
| slice-1 | #3167 (`758a85612`) | `EGG_EVENT_LOOP_OWNER` flag + dormant one-shot wrapper arm |
| slice-2 | #3169 | `OrchestratorEventLoop`, `_start_event_loop`, `_ExecutorEventSpawner`, agent-free confirm/complete |
| slice-3 | #3181 | `JobSupervisor` (bounded respawn, #3138 streak backoff) wired into the loop |
| slice-4 | #3192 | worktree re-attach + dirty-state discard + gateway-session reuse across spawns |
| slice-5 | #3198 | health-monitor / heartbeat orchestrator-mode awareness; convergence-stall notifier |
| slice-6 / docs | #3202, #3204, #3205 | flag doc, on-demand agent lifecycle doc, slice-5 health-monitoring doc |

There are no `TODO`/`FIXME`/`unproven` markers in `event_loop.py`, and the
mechanism has test coverage (`orchestrator/tests/test_event_loop.py` plus
orchestrator-mode tests in `test_concurrent_executor.py`,
`test_consensus_wrapper.py`, `test_kubernetes_spawner.py`, `test_heartbeat.py`).

**The operator directive makes adopt-vs-reimplement BINDING** ("what to adopt vs.
implement from scratch"). The issue's premise conflicts with verifiable git
state, so this is surfaced to the operator as HITL **cq-1**, not silently
propagated (which would direct the implementer to rebuild working code) nor
silently resolved (which would discard the merged #3064 code). **The scope and
ACs below are provisional and conditional on cq-1.**

## Problem Statement (as originally framed by #3229)

The orchestrator spawns the full agent team for a phase up front, and each agent
pod then runs a long-lived in-pod event-pump bash loop
(`orchestrator/consensus_wrapper.py`). The pod long-polls the bus
(`egg-orch message wait-loop`) with a 30 s heartbeat and stays alive — idle,
reserving CPU/memory and a gateway session — for the whole phase, until global
consensus completes. The 30-min idle budget (`EGG_BRC_IDLE_BUDGET_MIN`) only
raises an `OVERSEER_ALERT`; it never terminates the pod.

#3229's stated goal: invert the lifecycle so the orchestrator owns the event
loop and spawns a one-shot pod only when a role has an actionable event; the pod
handles the one event and exits. No idle pods. Resource-freeing, not
latency-sensitive; no cold-start SLO.

**This is exactly what #3064 already built and merged** (see the table above and
"Current state" below). So #3229 as written is mostly a description of completed
work; the genuine open question is what — if anything — remains undone, which is
why cq-1 must be answered first.

## Current state — what is ALREADY on main (the #3064 foundation)

The orchestrator-owned on-demand mechanism is **present on main, gated off by
default** (`EGG_EVENT_LOOP_OWNER` defaults to `pod`, so production behavior is
byte-identical to the in-pod loop until the flag is flipped).

### Orchestrator-side event loop + spawn trigger — PRESENT
- `orchestrator/event_loop.py`: `OrchestratorEventLoop` derives per-role
  next-actions against the consensus tracker and spawns one-shot Jobs for
  `propose|ack|nack`; `compute_dedupe_key` keys review events on
  `proposal_commit_sha` (the durable-state idempotency key #3229 asks for);
  restart reconcile seeds from live Job labels; `convergence_stall_notifier`
  re-uses the `OVERSEER_ALERT` surface.
- `concurrent_executor.py:431` `_start_event_loop` (slice-2): in
  `EGG_EVENT_LOOP_OWNER=orchestrator` mode, `spawn_all` spawns **no** agents up
  front and starts the loop instead (`spawn_all` returns `[]`). `_ExecutorEventSpawner`
  (`:118`) routes event spawns to `spawn_event_job`. Agent-free confirm/complete
  via `_orchestrator_side_confirm`.

### On-demand spawner + idempotency — PRESENT
- `kubernetes_spawner.py:1923` `spawn_event_job` (rejects non-spawn actions);
  `LABEL_EVENT_DEDUPE = "egg.event.dedupe-key"` (`:216`) + dedupe-label adoption
  (`:283`/`:330`/`:1874`) gives at-most-one-live-pod-per-role+event across the
  pod-startup window and across an orchestrator restart, derived from durable
  state — not process memory.

### Failure supervision re-homed — PRESENT
- `JobSupervisor` (slice-3, PR #3181): bounded respawn with backoff mirroring
  the wrapper's #3138 streak semantics; sticky `OVERSEER_ALERT` on exhaustion via
  `_emit_supervision_alert`; propose-arm exhaustion engages the existing
  `AGENT_FAILED` path (`_handle_propose_arm_exhaustion`);
  `_teardown_exhausted_session` releases the session on exhaustion.

### Worktree re-attach + gateway-session reuse — PRESENT
- slice-4 (PR #3192): pre-spawn worktree cleanup + adoption in
  `kubernetes_spawner`; dirty-state discard / hard-sync so a predecessor killed
  mid-event does not leak residue into a successor; gateway-session reuse across
  a role's successive spawns with phase-end/exhaustion teardown.

### Health-monitor lifecycle-owner awareness — PRESENT
- `health_monitor.py:212` `set_orchestrator_mode`; `_orchestrator_skip_tripwire`
  (`:464`) so "role X has no active Job" is the **normal** state under
  orchestrator ownership; `_publish_active_roles` /
  `_enable_orchestrator_mode_surfaces` wire `_active_jobs` from the loop.

### Ownership flag — PRESENT
- `env_config.py:498` `get_event_loop_owner` ∈ {`pod`, `orchestrator`}, default
  `pod`, no silent fallback (raises on invalid — the #3023 no-silent-default
  contract). `consensus_wrapper.py:1207` `_event_loop_owner()` + the
  `ONE_SHOT_OWNER` (`:1039`) `if owner=="orchestrator"` arm.

### What is NOT done (the apparent real delta)
- **The default flip is not done** — `EGG_EVENT_LOOP_OWNER` still defaults to
  `pod`, so the orchestrator-owned path has (apparently) never been exercised
  against a live BRC cycle in production. The issue body **explicitly defers the
  flip + live proving run to #3164**, out of #3229's scope. That leaves #3229
  with no obvious in-scope structural work versus main — hence cq-1.

### The in-pod loop (still the production default)
Each loop iteration calls `egg-orch brc get-state` / `brc next-action`; on
`propose|ack|nack` it invokes the agent one-shot with a per-event prompt from
`compose_event_prompt` (`orchestrator/routes/event_prompt.py`); on `wait` it
blocks on the bus. `confirm`/`complete` are handled agent-free. Failure handling:
linear backoff capped 30 s, `OVERSEER_ALERT` at streak 10 (#3138). The pod does
not exit between events.

> Attribution fix (v1 nit): `build_consensus_wrapped_command` is **defined** at
> `consensus_wrapper.py:1216`; `concurrent_executor.py` only *calls* it.

## The #2908 / PR #2949 foundation (unchanged, accurate from v1)

Stateless per-event invocation (`compose_event_prompt`), durable per-role BRC
memory (`.egg-state/agent-outputs/<role>/brc-memory.md`, `last_reviewed_commit_sha`),
server-side prefix cache (survives pod death), hostPath-persistent worktrees keyed
`{pipeline_id}[-{slice_id}]-{role}` (#3005, #2403). These are what made #3064
feasible and remain the substrate.

## Hard constraint (still binding for any residual work)

The passive-wrapper coexistence guard and the on-demand spawner must land
together, or the spawner strictly first; the guard must depend on the spawner,
never the reverse. #3023 committed the `EGG_EVENT_LOOP_OWNER`-style guard alone
and had to revert (silencing the in-pod loop with nothing replacing it deadlocks
BRC); and since #2908 slice-4 deleted `EGG_BRC_EVENT_PUMP` there is no rollback
path. **On main this constraint is already satisfied** — the flag defaults to the
in-pod loop and the spawner is fully present — so any residual #3229 work (and
the #3164 flip) inherits a safe ordering rather than re-establishing it.

## Real gap & provisional scope — CONDITIONAL ON cq-1

Because the mechanism is on main, the honest scope question is *what remains*, and
that depends on the operator's answer to cq-1. The three plausible shapes:

- **cq-1 → adopt + verify/gap-fill (opt-1).** Re-scope #3229 to: (a) a written
  reconciliation of the on-main #3064 mechanism against #3229's intent; (b) a
  concrete defect/gap audit of the landed code (correctness of dedupe across
  restart, supervision exhaustion paths, worktree residue discard, health-monitor
  mode tripwires) producing a punch-list of anything missing/broken/unproven; (c)
  ACs that target only that punch-list. If the audit finds the delta is solely the
  proving run + flip, #3229 explicitly converges on #3164.
- **cq-1 → named defect (opt-2).** The operator identifies the specific unproven
  behaviour that motivated re-filing; refine/plan re-derive scope + ACs against
  that concrete gap.
- **cq-1 → collapse into #3164 (opt-3).** #3229 is redundant with merged #3064 +
  pending #3164; close/collapse rather than run a fresh refine→plan→implement.

**Do NOT plan a greenfield build of the mechanism** — that is the
adopt-vs-reimplement hazard both reviewers flagged and the operator directive
forbids.

### Out of scope (unchanged)
- The default flip + in-pod-loop retirement → **#3164** (gated on a live BRC
  proving run). The agent primitive (pod image / worktree / Agent SDK /
  permissions / gateway restrictions). BRC protocol semantics. Extended
  prompt-cache TTL configuration.

## Provisional acceptance criteria — to be finalized after cq-1

These replace v1's AC1–AC8 (which described building already-merged components).
Phrased as verify/gap-fill against on-main #3064:

- AC1: The analysis/plan reconciles against `origin/main` — the on-main #3064
  mechanism is treated as the foundation, not as absent.
- AC2: A defect/gap audit of the landed #3064 code produces an explicit
  punch-list (each item: file:symbol, missing/incomplete/unproven/broken, why it
  matters). If the punch-list is empty modulo the flip, that is stated and #3229's
  overlap with #3164 is recorded.
- AC3: Any code work targets only audited gaps; no reimplementation of components
  already present and tested on main.
- AC4: `EGG_EVENT_LOOP_OWNER` remains defaulted to `pod`; the default flip stays
  with #3164. Production BRC consensus completes end-to-end with the flag unset.
- AC5: cq-1 is resolved by the operator before plan finalization; the chosen
  disposition (adopt/verify, named-defect, or collapse-into-#3164) governs the
  final plan.
- AC6: Docs updated only where the audit changes the documented behaviour
  (avoid duplicating the slice-1/5 + on-demand-lifecycle docs already on main).

## Risks / trade-offs

- **Reimplementation hazard** — the dominant risk; mitigated by cq-1 + the
  verify/gap-fill framing.
- **Redundant pipeline** — if cq-1 → opt-3, running implement at all is waste;
  surfacing the question now avoids it.
- **Residual-work ordering** — any gap-fill still inherits the spawner-first hard
  constraint, already satisfied on main.

## Related

- **#3064** (MERGED, slices 1–6) — the orchestrator-owned event loop + on-demand
  one-shot pods. **This is on main; #3229's "nothing on main" premise is wrong.**
- **#3164** — flip `EGG_EVENT_LOOP_OWNER` default to orchestrator + retire the
  in-pod wait arm; gated on a live BRC proving run. The only undone piece of the
  #3229 vision.
- **#2908** (closed, PR #2949) — BRC event-pump + durable memory + `compose_event_prompt`;
  the substrate.
- #3138 — wrapper streak backoff (mirrored by `JobSupervisor`). #2806 — producer
  failure exit-code signaling (re-homed into supervision). #2761 — tracker rebuilt
  from message store (restart durability). #3005 / #2403 — hostPath-persistent
  worktrees. #3070 — restart-durability reference.
- Superseded attempt #3023 (reverted; its failure modes — #3040→#3048,
  #3043→#3050, #3046→#3049 — are fixed on main).
