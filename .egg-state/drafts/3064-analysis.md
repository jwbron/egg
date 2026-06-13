# Analysis: Orchestrator-driven on-demand agent spawning — lift the event pump out of the pod

> Issue: #3064 | Phase: refine | Pipeline: issue-3064

## Problem Statement

The orchestrator spawns the full agent team for a phase up front
(`orchestrator/concurrent_executor.py:311-349`, `spawn_all()` →
`kubernetes_spawner.spawn_agent_job`, `orchestrator/kubernetes_spawner.py:491-940`),
and each agent pod then runs a long-lived in-pod event-pump bash loop
(`orchestrator/consensus_wrapper.py:110-916`, `_EVENT_PUMP_WRAPPER_TEMPLATE`).
The pod long-polls the bus (`egg-orch message wait-loop`, 60s inner timeout,
wrapper ≈379) with a 30s background heartbeat (≈209-230, default
`EVENT_PUMP_HEARTBEAT_INTERVAL_SECS_DEFAULT = 30` at ≈76) and stays alive —
idle, reserving CPU/memory and a gateway session — for the whole phase, until
global consensus completes. The 30-min idle budget (`EGG_BRC_IDLE_BUDGET_MIN`,
≈67) only raises an `OVERSEER_ALERT` (≈702-720); it never terminates the pod.

#3064 proposes inverting the lifecycle: **the orchestrator owns the event
loop and spawns an agent pod only when that role has an actionable event; the
pod handles the one event and exits.** No idle pods. The actionable-event
signal already exists: `_derive_next_action`
(`orchestrator/routes/consensus.py:296-422`) computes per-role
`propose / ack / nack / confirm / complete / wait` — today it is consumed by
the in-pod loop pulling; the issue wants the orchestrator pushing (spawning)
in response to it.

## Current Behavior (verified against the working tree)

### Spawn-up-front lifecycle

- `spawn_all()` (`concurrent_executor.py:311-349`) creates the peer-consensus
  tracker and spawns every role concurrently via ThreadPoolExecutor;
  `_spawn_agent()` (≈418-503) wraps each agent in
  `build_consensus_wrapped_command()` (≈466-468).
- `spawn_agent_job()` (`kubernetes_spawner.py:491-940`): one Job per role,
  named `egg-agent-<pipeline_id>-[<slice_id>-]<role>` (≈352-383); per-agent
  worktree `{pipeline_id}[-{slice_id}]-{role}` created via
  `gateway.create_worktrees()` with retry (≈614-722); gateway session
  registered once at spawn (≈760-799, token-only auth, `EGG_SESSION_TOKEN`
  env). Session and reservation live for the entire pod lifetime.
- Termination: only when the wrapper observes `complete` /
  `consensus_is_complete()`. Cleanup tears down the session
  (`remove_agent_container(cleanup_session=True)`); the worktree persists on
  disk.

### The in-pod loop is already one-shot per *invocation* — only the *pod* is long-lived

Each loop iteration calls `egg-orch brc get-state` / `brc next-action`; on
`propose|ack|nack` it invokes the agent one-shot (`invoke_agent_for_event()`,
wrapper ≈404-480; agent spawn ≈847-902) with a per-event prompt composed by
`orchestrator/routes/event_prompt.py` (`compose_event_prompt`); on `wait` it
blocks on the bus. `confirm`/`complete` are handled **without invoking the
agent** — the wrapper just calls `egg-orch consensus confirmed` and moves on.
Failure handling is in-loop: linear backoff `streak * 2s` capped at 30s
(≈897-901), warn at streak 5, `OVERSEER_ALERT` at streak 10 (#3138, ≈597-627).

### Foundation already on main (what makes on-demand feasible)

1. **Stateless per-event invocation** — `compose_event_prompt` builds the
   entire single-event prompt: role banner, event payload, per-producer
   `git log {last_reviewed_sha}..{proposal_sha} --not origin/{base} -p` delta
   (`event_prompt.py:207-312`), open-NACK section, tail-positioned memory
   excerpt (2 KB cap, ≈63; 10 KB envelope budget, ≈75).
2. **Durable per-role continuity** — BRC memory at
   `.egg-state/agent-outputs/<role>/brc-memory.md`
   (`sandbox/egg_agent_tools/handlers/brc_memory.py`: per-producer
   `last_reviewed_commit_sha`, prior verdicts/NACK reasons, decision log
   capped at 20 entries; atomic `os.replace()` write; `EGG_BRC_MEMORY`
   defaults to `full`). The agent's working memory survives across stateless
   invocations.
3. **Cache survives pod death** — the prompt prefix cache is server-side
   (Anthropic/LiteLLM), keyed on the stable prefix, not pinned to a pod.
   Invocations are already one-shot, so inter-call gaps are set by event
   arrival, not pod lifecycle; spawn latency adds tens of seconds to gaps that
   are already minutes long. (Anthropic default cache TTL is 5 min; no
   extended-TTL `cache_control` exists in the repo — true today, unchanged by
   this issue.)
4. **Worktree persistence** — worktrees are hostPath-persistent across pod
   restarts and keyed per `{pipeline_id}[-{slice_id}]-{role}` (#3005, #2403).
   Re-attach across successive spawns is a reuse problem, not research.

### State inventory: what a pod holds vs. what is already durable

Lost on pod exit: container process memory, the background heartbeat
subprocess, the gateway session token, uncommitted worktree staging state.
Already durable: brc-memory.md, `.egg-state/` committed artifacts, the
worktree on disk, the message store (PROPOSE/ACK/NACK replay source), and the
consensus tracker (rebuilt from messages, #2761). The only *correctness*-
relevant pod-held state is the gateway session and any uncommitted worktree
state — both manageable at spawn boundaries.

## Hard Constraint (learned from #3023, the scrapped first attempt)

**The passive-wrapper coexistence guard and the on-demand spawner must land
together, or spawner strictly first.** The first run committed the
`EGG_EVENT_LOOP_OWNER` guard alone and had to revert: silencing the in-pod
loop with nothing replacing it deadlocks BRC. And since #2908 slice-4 deleted
the legacy `EGG_BRC_EVENT_PUMP` flag, the current wrapper has **no rollback
path** — the new ownership flag must default to the in-pod loop and flip only
after the spawner is proven against a live BRC cycle.

The #3023 run's failure modes are fixed on main: contract-verify skipping
slice PRs (#3040 → #3048), post-BRC reviewer-confirm deadlock (#3043 →
#3050), overlapping-but-unordered slices rejected at plan ingestion (#3046 →
#3049). Nothing from #3023 is on main; its leftover remote branches
(`egg/issue-3023/*`, `egg/recovered/issue-3023/*`) don't collide with this
pipeline's namespace.

## Design Questions the Plan Must Answer (grounded in code)

1. **Spawn trigger & idempotency.** The orchestrator's consensus poll will
   re-derive the same actionable event for the ~10-30s of pod startup. The
   spawner needs a dedupe key: role + event identity (e.g. the
   `proposal_commit_sha` already carried in `pending_reviews` payloads,
   `routes/consensus.py:220-221`; for producers, the NACK version being
   addressed). One event → one pod.
2. **Verb→pod mapping.** Only `propose|ack|nack` need judgment and hence a
   pod. `confirm`/`complete` bookkeeping moves orchestrator-side (the wrapper
   already does these agent-free). A naive mapping spawns pods that do
   nothing.
3. **Failure supervision re-homing.** Today the still-running loop retries
   naturally (streak backoff, #3138) and #2806's exit-code path signals
   persistent producer failure. With one-shot pods, a pod dying mid-event
   leaves nothing running — the orchestrator must notice (Job status) and
   respawn bounded or alert. See HITL cq-2.
4. **Orchestrator-restart durability.** In-pod loops let BRC progress survive
   an orchestrator bounce for free. Once the orchestrator owns the loop, the
   spawn bookkeeping must either be persisted or — better — **stateless: re-derived
   from consensus state on startup** (the tracker is already rebuilt from the
   message store, #2761), with the dedupe key making re-derived spawns
   idempotent. Cf. #3070.
5. **Worktree & session reuse.** Per-event pods make worktree setup a hot
   path. Worktrees already persist; the spawner re-attaching instead of
   recreating cuts spawn latency and gateway fetch traffic, but needs a
   staleness/ownership story. Gateway sessions: today one per pod lifetime;
   per-event pods imply setup/teardown per spawn unless sessions are reusable
   per role (the spawner already pre-registers a session per Job).
6. **Health-monitor semantics shift.** `HealthMonitor`
   (`orchestrator/health_monitor.py:106-400`) keys tripwires on heartbeat
   timeouts (120s default, 600s implement) and container exits — both assume
   long-lived pods. "Role X has no pod" becomes the *normal* state; the
   idle-budget overseer alert ("role X stuck in wait N min") moves
   orchestrator-side where the global judgment belongs. The
   `HeartbeatCoordinator` (heartbeat.py:45-211) session-refresh side effect
   (#2076, #2451) also loses its sender when no pod is running.

## Scope Options (HITL cq-1)

- **Option A — spawner + guard, flag off:** orchestrator-side event loop and
  on-demand spawner for `propose|ack|nack`; confirm/complete bookkeeping
  orchestrator-side; `EGG_EVENT_LOOP_OWNER`-style flag defaulting to the
  in-pod loop; spawn dedupe + bounded respawn supervision. The in-pod path
  remains the production default. Smallest honest unit that respects the hard
  constraint.
- **Option B — A + lifecycle re-homing (recommended):** A plus worktree
  re-attach and session reuse across a role's successive spawns, idle-budget
  /stall alerts re-homed orchestrator-side, health-monitor thresholds made
  lifecycle-aware, and #2806 failure signaling relocated. Delivers the full
  mechanism behind the flag; the flip itself stays a follow-up gated on a
  live BRC cycle run with the flag on (the issue's own bar).
- **Option C — B + default flip and in-pod loop retirement:** also flips the
  default to orchestrator-owned and deletes the in-pod wait arm in this same
  pipeline. Violates the spirit of the proven-first bar unless the pipeline
  itself can run a live BRC validation cycle before the flip commit.

Recommendation: **B**. The first attempt died by descoping the spawner itself;
A is defensible but leaves operational re-homing (supervision, monitors) as
debt that makes the flip risky later. C front-loads the flip without the
proving run the issue demands.

## Risks / Trade-offs

- **Deadlock by partial landing** — mitigated by the hard constraint
  (spawner-first ordering; flag defaults to in-pod loop; #3049 now rejects
  unordered overlapping slices at ingestion).
- **Cold-start latency per event** — pod + worktree attach on the hot path;
  worktree reuse (Option B) is the lever. Needs an explicit per-event latency
  budget in the plan.
- **Supervision gaps** — one-shot pods convert "loop retries" into
  "orchestrator must respawn"; bounded-respawn policy (cq-2) prevents both
  silent stalls and runaway spawn loops.
- **Monitor false positives** — health tripwires assuming long-lived pods
  will misfire on ephemeral ones; thresholds must become
  lifecycle-owner-aware in the same change that flips behavior.

## Out of Scope

- The agent primitive (pod image, Agent SDK, permissions, gateway
  restrictions) — untouched.
- BRC protocol semantics (propose/ack/nack/confirm, Delphi redaction,
  multi-reviewer NACK barrier) — unchanged; only *when a pod exists* changes.
- Extended prompt-cache TTL configuration — orthogonal, status quo preserved.

## Related

#2908 (foundation: event pump, compose_event_prompt, durable memory), #3017
(declarative phase model, adjacent), #2958 (producer lifecycle, adjacent),
#2866 (k3s runtime, adjacent), #3002 (GKE split-object-store, adjacent),
#3023/#3041 (scrapped first attempt, fully superseded here).


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

## Resolved Questions

**Scope for #3064 on-demand agent spawning (cq-1):**
Answer: Option B — A + lifecycle re-homing; flip deferred to a gated follow-up. Operator additionally directed that the flip follow-up be filed immediately so the in-pod loop retirement is scheduled work (live BRC proving run with flag on → flip default → delete in-pod wait arm + heartbeat + ownership flag in one cleanup PR), not lingering deprecation. End state must have no dead/deprecated code; the flag window is accepted only as a bounded proving period.

**Failure-supervision policy for one-shot agent pods (cq-2):**
Answer: Bounded automatic respawn with backoff, then OVERSEER_ALERT — mirror the wrapper's #3138 streak semantics (transient failures retry silently within a bounded budget; humans only see persistent exhaustion).
