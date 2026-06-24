# Analysis: Orchestrator-driven on-demand agent spawning — lift the event pump out of the pod

> Issue: #3229 | Phase: refine | Pipeline: issue-3229
> Supersedes #3064 (and #3023). Nothing from either attempt is on main; clean re-run.

## Problem Statement

The orchestrator spawns the full agent team for a phase up front
(`orchestrator/concurrent_executor.py:394` `spawn_all()` →
`build_consensus_wrapped_command` at `:632`/`:776` → `kubernetes_spawner.spawn_agent_job`,
`orchestrator/kubernetes_spawner.py:1181`), and each agent pod then runs a
long-lived in-pod event-pump bash loop (`orchestrator/consensus_wrapper.py`).
The pod long-polls the bus (`egg-orch message wait-loop`, wrapper `:428`) with a
30 s background heartbeat (`EVENT_PUMP_HEARTBEAT_INTERVAL_SECS_DEFAULT = 30`,
`:125`) and stays alive — idle, reserving CPU/memory and a gateway session — for
the whole phase, until global consensus completes. The 30-min idle budget
(`EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT = 30`, `:109`; env `EGG_BRC_IDLE_BUDGET_MIN`)
only raises an `OVERSEER_ALERT` (anomaly `stuck-phase-transition`); it never
terminates the pod.

#3229 inverts the lifecycle: **the orchestrator owns the event loop and spawns
an agent pod only when that role has an actionable event; the pod handles the one
event and exits.** No idle pods. The actionable-event signal already exists:
`_derive_next_action` (`orchestrator/routes/consensus.py:296`) computes per-role
`propose / ack / nack / confirm / complete / wait` — today consumed by the in-pod
loop *pulling*; the issue wants the orchestrator *pushing* (spawning) in response.

This is **resource-freeing, not latency-sensitive**: the only things that move
are *where the wait lives* and *when a pod exists*. There is no cold-start SLO.
Per-phase reuse of the prompt-cache prefix / gateway session / worktree is
unchanged; pods just don't exist while a role is idle.

## Current Behavior (verified against the working tree, 2026-06-24)

### Spawn-up-front lifecycle

- `spawn_all()` (`concurrent_executor.py:394`) creates the peer-consensus tracker
  and spawns every role concurrently; each agent is wrapped via
  `build_consensus_wrapped_command()` (`:632`, `:776`).
- `spawn_agent_job()` (`kubernetes_spawner.py:1181`): one Job per role; per-agent
  hostPath worktree keyed `{pipeline_id}[-{slice_id}]-{role}`; gateway session
  registered once at spawn (token-only auth, `EGG_SESSION_TOKEN`). Session and
  reservation live for the entire pod lifetime.
- Termination: only when the wrapper observes `complete` / global consensus
  `is_complete`. Cleanup tears down the session; the worktree persists on disk.

### The in-pod loop is already one-shot per *invocation* — only the *pod* is long-lived

Each loop iteration calls `egg-orch brc get-state` / `brc next-action`; on
`propose|ack|nack` it invokes the agent one-shot with a per-event prompt composed
by `compose_event_prompt` (`orchestrator/routes/event_prompt.py:531`); on `wait`
it blocks on the bus (`:428`). `confirm`/`complete` are handled **without invoking
the agent** — the wrapper just calls `egg-orch consensus confirmed` and moves on
(wrapper docstring `:19`). Failure handling is in-loop: linear backoff
`streak × 2 s` capped 30 s, warn at streak 5, sticky `OVERSEER_ALERT` (anomaly
`agent-invocation-fail-streak`) at streak 10 (#3138, wrapper `:25-29`).

### Foundation already on main (what makes on-demand feasible) — #2908 / PR #2949

1. **Stateless per-event invocation** — `compose_event_prompt` builds the entire
   single-event prompt: role banner, event payload, per-producer
   `git log {last_reviewed_sha}..{proposal_sha} --not origin/{base} -p` delta,
   open-NACK section, tail-positioned memory excerpt.
2. **Durable per-role continuity** — BRC memory at
   `.egg-state/agent-outputs/<role>/brc-memory.md`
   (`sandbox/egg_agent_tools/handlers/brc_memory.py`: per-producer
   `last_reviewed_commit_sha`, prior verdicts/NACK reasons, decision log; atomic
   `os.replace()` write). Working memory survives across stateless invocations.
3. **Cache survives pod death** — the prompt prefix cache is server-side
   (Anthropic/LiteLLM), keyed on the stable prefix, not pinned to a pod.
   Invocations are already one-shot, so inter-call gaps are set by event arrival,
   not pod lifecycle; spawn latency adds tens of seconds to gaps already minutes
   long.
4. **Worktree persistence** — worktrees are hostPath-persistent across pod
   restarts and keyed per `{pipeline_id}[-{slice_id}]-{role}` (#3005, #2403).
   Re-attach across successive spawns is a reuse problem, not research.

### State inventory: what a pod holds vs. what is already durable

Lost on pod exit: container process memory, the heartbeat subprocess, the gateway
session token, uncommitted worktree staging state. Already durable: brc-memory.md,
committed `.egg-state/` artifacts, the worktree on disk, the message store
(PROPOSE/ACK/NACK replay source), and the consensus tracker (rebuilt from
messages, #2761). The only *correctness*-relevant pod-held state is the gateway
session and any uncommitted worktree state — both manageable at spawn boundaries.

## Hard Constraint (learned from #3023, the scrapped first attempt)

**The passive-wrapper coexistence guard and the on-demand spawner must land
together, or the spawner strictly first.** #3023 committed the
`EGG_EVENT_LOOP_OWNER`-style guard alone and had to revert: silencing the in-pod
loop with nothing replacing it deadlocks BRC. And since #2908 slice-4 deleted the
legacy `EGG_BRC_EVENT_PUMP` flag, the current wrapper has **no rollback path** —
the new ownership flag must default to the in-pod loop and flip only after the
spawner is proven against a live BRC cycle (the #3164 follow-up). A plan that
separates guard from spawner must make the guard depend on the spawner, never the
reverse.

The #3023 failure modes are fixed on main: contract-verify skipping slice PRs
(#3040 → #3048), post-BRC reviewer-confirm deadlock (#3043 → #3050),
overlapping-but-unordered slices rejected at plan ingestion (#3046 → #3049).

## Design Questions the Plan Must Answer (grounded in code)

1. **Spawn trigger & idempotency.** The consensus poll re-derives the same
   actionable event for the ~10–30 s of pod startup. The spawner needs a dedupe
   key: role + event identity — the `proposal_commit_sha` already carried in
   `pending_reviews` payloads (`routes/consensus.py:220`) for review verbs; target
   version + the open-NACK set for proposes. One event → one pod; at most one live
   pod per role+slice. The key must be derivable from durable state (message-store
   versions / commit SHAs), never from orchestrator process memory alone.
2. **Verb→pod mapping.** Only `propose|ack|nack` need judgment and hence a pod.
   `confirm`/`complete` bookkeeping moves orchestrator-side (the wrapper already
   does these agent-free). A naive verb→pod mapping spawns pods that do nothing.
3. **Failure supervision re-homing.** With one-shot pods, a pod dying mid-event
   leaves nothing running — the orchestrator must notice (Job status) and respawn
   bounded or alert. #2806's exit-code signaling and #3138's streak backoff need a
   new home. Distinguish pod-infrastructure failure from a legitimate NACK loop —
   only the former consumes respawn budget. (Decided policy below.)
4. **Orchestrator-restart durability.** In-pod loops let BRC progress survive an
   orchestrator bounce for free. Recommended: **stateless re-derivation rather
   than persisted bookkeeping** — on restart re-derive next-actions from the
   consensus tracker (rebuilt from the message store, #2761) and reconcile against
   live Kubernetes Jobs before spawning, with the dedupe key making re-derived
   spawns idempotent (cf. #3070).
5. **Worktree & session reuse across a role's successive spawns.** Re-attach the
   role's existing hostPath worktree (validate expected branch / `.git` integrity
   / no foreign lock; fall back to create-with-retry on mismatch) and, on every
   successful re-attach, discard uncommitted/untracked residue and hard-sync to
   the role branch tip before invocation — so a predecessor killed mid-event never
   leaks unproposed changes into a successor's commit. Reuse gateway sessions
   across a role's spawns (re-register only when none is live or token aged out),
   teardown at phase end / supervision exhaustion. Justified by **correctness and
   resource hygiene, not a latency budget.**
6. **Health-monitor semantics shift.** `HealthMonitor`
   (`orchestrator/health_monitor.py:106`) keys tripwires on heartbeat timeouts
   (120 s default, 600 s implement; `:274-279`) and container exits — both assume
   long-lived pods. "Role X has no pod" becomes the *normal* state under
   orchestrator ownership; the idle-budget overseer alert ("role X stuck in wait
   N min") moves orchestrator-side where the global judgment belongs. Tripwires
   must become lifecycle-owner-aware so they do not misfire on ephemeral pods.

## Scope (DECIDED — Option B minus the latency SLO)

The operator fixed scope in the issue body and carried the prior run's gate
resolutions; these are **binding** for plan, not open questions:

**In scope (deliver the full mechanism behind a flag defaulting to the in-pod loop):**
- Orchestrator-side event loop + on-demand spawner for `propose|ack|nack`;
  `confirm`/`complete` executed orchestrator-side with no pod.
- An ownership flag (e.g. `EGG_EVENT_LOOP_OWNER`) defaulting to the in-pod loop.
- Failure supervision re-homed: **bounded automatic respawn with backoff mirroring
  #3138 streak semantics; `OVERSEER_ALERT` only on persistent exhaustion.**
- Spawn idempotency via a durable-state dedupe key (proposal_commit_sha /
  version + open-NACK set); at most one live pod per role+slice.
- Stateless restart re-derivation reconciled against live Jobs.
- Worktree re-attach + residue-discard/hard-sync; gateway-session reuse with
  phase-end/exhaustion teardown.
- Idle/stall alerts and health-monitor thresholds made lifecycle-owner-aware;
  #2806 failure signaling relocated.
- Docs for the final shape.

**Out of scope (this pipeline):**
- The default flip and in-pod-loop retirement — tracked in **#3164** (flip gated
  on a live BRC proving run with the flag on). The flag window is a *bounded
  proving period*, not a permanent dual-mode state; #3164 retires the in-pod wait
  arm + heartbeat + ownership flag in one cleanup PR so the end state carries no
  dead/deprecated code.
- The agent primitive (pod image / worktree / Agent SDK / permissions / gateway
  restrictions).
- BRC protocol semantics (propose/ack/nack/confirm, Delphi redaction,
  multi-reviewer NACK barrier) — only *when a pod exists* changes.
- Extended prompt-cache TTL configuration.

No HITL decision is registered at this gate: the two questions the prior run
(#3064) escalated — scope (Option B) and failure-supervision policy (bounded
respawn + alert) — are pre-resolved verbatim in this issue's "Scope (decided)"
and "Recommended resolutions carried from the prior run's gates" sections. If a
reviewer surfaces a *new* structural question, register it then.

## Risks / Trade-offs

- **Deadlock by partial landing** — mitigated by the hard constraint
  (spawner-first ordering; flag defaults to in-pod loop; #3049 now rejects
  unordered overlapping slices at ingestion).
- **Supervision gaps** — one-shot pods convert "loop retries" into "orchestrator
  must respawn"; the bounded-respawn policy prevents both silent stalls and
  runaway spawn loops.
- **Monitor false positives** — health tripwires assuming long-lived pods misfire
  on ephemeral ones; thresholds must become lifecycle-owner-aware in the same
  change that introduces the spawner.
- **Spawn races** — the dedupe key must hold across the pod-startup window and
  across an orchestrator restart (durable-state derived), or two pods race one
  event.

## Acceptance Criteria (proposed; reviewer to confirm/refine in plan)

- AC1: On-demand spawner spawns exactly one pod per actionable `propose|ack|nack`
  event for a role+slice (dedupe key holds across the startup window).
- AC2: `confirm`/`complete` advance consensus orchestrator-side with no pod spawned.
- AC3: `EGG_EVENT_LOOP_OWNER` defaults to the in-pod loop; the in-pod path remains
  the production default and BRC consensus still completes end-to-end with the flag
  unset.
- AC4: A pod that dies mid-event is respawned within a bounded budget with backoff;
  persistent exhaustion raises a single `OVERSEER_ALERT`; a legitimate NACK loop
  does not consume respawn budget.
- AC5: Orchestrator restart re-derives outstanding actions from durable state and
  reconciles against live Jobs without double-spawning.
- AC6: Worktree re-attach discards uncommitted/untracked residue and hard-syncs to
  the role branch tip before invocation; no predecessor residue leaks into a
  successor's commit.
- AC7: Health-monitor heartbeat/container tripwires do not misfire when a role
  legitimately has no pod under orchestrator ownership.
- AC8: Docs describe the final lifecycle-owner model and the #3164 flip path.

## Out of Scope

See "Scope (DECIDED)" above.

## Related

- **#3164** — flip event-loop ownership to the orchestrator + retire the in-pod
  wait arm (the gated follow-up to this work).
- **#2908** (closed) — BRC event-pump + durable agent memory; the foundation
  (PR #2949 added `compose_event_prompt` + the cache-preserving prompt shape).
- #3017 — declarative phase/stage abstraction (adjacent). #2958 — streaming
  per-task commits / producer lifecycle (adjacent). #2866 — k3s as the sole
  runtime (adjacent). #3070 — restart durability (cf. point 4). #2761 — tracker
  rebuilt from message store. #3138 — wrapper streak backoff. #2806 — producer
  failure exit-code signaling.
- Superseded: #3064, #3023 (nothing on main).
