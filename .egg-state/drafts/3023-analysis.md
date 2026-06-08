# Analysis: Orchestrator-driven on-demand agent spawning — lift the event pump out of the pod so idle agents don't exist

> Issue: #3023 | Phase: refine

## Problem Statement

Today the orchestrator spawns the **full agent team for a phase up front**, and every pod runs a long-lived **in-pod event-pump bash loop** (`orchestrator/consensus_wrapper.py:102-715`) that long-polls the message bus and invokes `python3 -m egg_agent` one-shot per actionable BRC event. The pod stays alive — idle, heartbeating, holding a gateway session — for the entire phase, even when it has nothing to do, until global consensus completes.

This issue proposes inverting that lifecycle: **the orchestrator owns the event loop and spawns an agent pod only when a role has an actionable event; the pod handles the single event and exits.** No idle pods. The orchestrator becomes the active party that *reacts* to events and *pushes* work, rather than a fleet of pods *passively pulling* on a bus.

Two goals (verbatim from the issue body, restated):

1. **Reduce resource use.** An idle pod blocked on `egg-orch message wait-loop` still holds its CPU/memory reservation, a gateway session, and a 30 s heartbeat cadence for the entire phase. On a resource-constrained cluster (e.g. `egg-system` running everything on a single k3s node — see `docs/architecture/kubernetes-migration.md`), that idle reservation is pure waste — pods sit in `wait` well past the WS7-observed 10–13 min legitimate-idle ceiling, up to the 30-min `EGG_BRC_IDLE_BUDGET_MIN` budget (`consensus_wrapper.py:59`) and beyond.
2. **Make the system *truly* event-driven.** The current model is poll-based dressed as event-driven: the orchestrator already computes *when* a role is actionable (`_derive_next_action` in `orchestrator/routes/consensus.py:280-406`), but that signal is consumed by an in-pod bash loop that long-polls for it (`consensus_wrapper.py:574-602`). The orchestrator should *push* — spawn the agent pod in direct response to the event — instead of every pod *pulling*.

## Current Behavior

### Spawn — "spawn all at phase start"

At the start of each phase, `ConcurrentExecutor.spawn_all` (`orchestrator/concurrent_executor.py:323-376`) walks `self.get_agent_roles()` and concurrently spawns one container/Job per role via `_spawn_agent` (`concurrent_executor.py:445-524`). Each spawn:

1. Resolves the worktree branch and BRC-aware env (`get_worktree_branch`, `get_agent_env`).
2. Resolves the per-agent model decision via `resolve_agent_model` (`#2769` slice-2, `concurrent_executor.py:472-485`).
3. Builds the wrapper command: `command = build_consensus_wrapped_command(prompt_text, model=decision.claude_code_alias)` (`concurrent_executor.py:489`).
4. Hands the command to `self.spawn_fn` (`concurrent_executor.py:511`), which is either the docker `ContainerSpawner` or the k8s `KubernetesSpawner.create_concurrent_spawn_fn()` factory.

The restart-on-resume path repeats this in `orchestrator/routes/pipelines.py` (search: `build_consensus_wrapped_command`) — same per-role spawn, called when an orchestrator restart finds the pipeline mid-phase.

A surviving helper, `spawn_specific_roles` (`concurrent_executor.py:378-400`), already exists: it was added in #1879 to respawn the subset of roles that died with transient failures, and **does not** touch the consensus tracker (roles were already registered by `spawn_all`). This is the natural seam an on-demand model would reuse.

### Run — the in-pod event-pump

`build_consensus_wrapped_command(prompt_text, model="opus", max_turns=1000)` (`consensus_wrapper.py:770-802`) is a thin alias for `build_event_pump_wrapped_command` (`consensus_wrapper.py:725-767`); since #2908 slice-4 it is the only production path. It composes a `bash -c "..."` invocation around `_EVENT_PUMP_WRAPPER_TEMPLATE` (`consensus_wrapper.py:102-715`) with three composition-time placeholders: the agent-command prefix (`python3 -m egg_agent --model X --max-turns N`), the idle-budget default, and the heartbeat/wait-timeout defaults.

Inside the pod the wrapper bash loop runs `while true`:

1. `egg-orch brc get-state --json` (`consensus_wrapper.py:565`) — snapshot the global BRC matrix.
2. If `consensus_is_complete`, call `egg-orch consensus confirmed` and `exit 0` (`consensus_wrapper.py:567-570`).
3. `egg-orch brc next-action --role $EGG_AGENT_ROLE --json` (`consensus_wrapper.py:574-602`) returns `{action, event_payload, reason}` where `action ∈ {wait, propose, ack, nack, confirm, complete}` (the closed set lives in `routes/consensus.py:77`).
4. Dispatch:
   - `complete` → confirm + exit 0.
   - `confirm` → `egg-orch consensus confirmed` (with linear backoff on transient failures).
   - `wait` → `wait_for_event`: spawn a 30 s background heartbeat subshell (`consensus_wrapper.py:188-234`), call `egg-orch message wait-loop` with a typed filter set and 60 s inner timeout (`consensus_wrapper.py:345-360`).
   - `propose|ack|nack` → `invoke_agent_for_event` (`consensus_wrapper.py:400-449`): run the event-prompt composer script (`orchestrator/routes/event_prompt.py`), then invoke the agent one-shot with the rendered prompt on stdin.

A 30-min idle budget (`EGG_BRC_IDLE_BUDGET_MIN`, `consensus_wrapper.py:59`) raises an `OVERSEER_ALERT` (`stuck-phase-transition`) at 1× and 2× thresholds (`consensus_wrapper.py:531-549`) but **does not** terminate the pod or fail the pipeline.

### Terminate — only when consensus completes

Only when global consensus completes (`is_complete=True`) does each wrapper see `complete`/`consensus_is_complete()` and exit; the orchestrator's run-loop then stops any stragglers. So agent *invocation* is already one-shot-per-event — but the *pod* is long-lived, and the event loop lives inside it.

### The foundation that makes the inversion feasible (#2908 slices 1–4)

Before #2908 the agent process held in-memory continuity across events and the pod could not be one-shot per event without re-priming context. PR #2949 (slice-3) and the surrounding work severed that:

| Primitive | Where it lives | What it guarantees |
|---|---|---|
| `compose_event_prompt(role, event_payload, memory_excerpt, nacks, git_log_delta, base_branch) -> str` | `orchestrator/routes/event_prompt.py:337-447` | Per-event-stateless prompt assembly — takes inputs only, returns the full prompt. No process-local state survives between calls. |
| Per-role durable BRC memory | `.egg-state/agent-outputs/<role>/brc-memory.md` (read at `event_prompt.py:486`; written by `brc_ack` / `brc_nack` handlers) | Externalizes the agent's working memory (notably `last_reviewed_commit_sha` per producer) across stateless invocations. |
| Cache-preserving prompt shape | `event_prompt.py:28, 75, 413-436` — tail-position memory excerpt, 10 KB envelope cap, 2 KB memory cap | The Anthropic / LiteLLM prefix cache is **server-side**, ≥ 60 min TTL, **not pinned to a pod**. A *fresh pod* composing the same stable prefix hits the same warm cache. |
| Closed-set next-action route | `orchestrator/routes/consensus.py:280-492` | The orchestrator already derives the verb that would drive a spawn decision; turning that into the spawn trigger is wiring, not algorithm. |

In other words, the only thing still keeping a pod alive is the in-pod bash loop and the session keep-alive heartbeat — neither is fundamental to correctness or to caching.

### Where the "wait" already lives in the orchestrator

The orchestrator's consensus tracker (`orchestrator/peer_consensus.py`, `routes/consensus.py`) already maintains the per-role action state and exposes it on `POST /api/v1/pipelines/<pipeline_id>/consensus/next-action`. The orchestrator's run loop in `routes/pipelines.py` already polls consensus state on a short cadence to decide phase completion. The seam for an on-demand spawner is: hook into the same tick that already runs, and convert the wait-for-completion poll into a wait-for-`next-action != "wait"` poll *per role*.

## Constraints

- **No regression on BRC semantics.** The BRC protocol (propose / ack / nack / confirm, Delphi redaction, multi-reviewer NACK barrier, open-NACK auto-repropose) is unchanged. Only *when a pod exists* changes.
- **Cache TTL discipline.** The Anthropic / LiteLLM prefix cache TTL is **≥ 60 min** server-side. For the on-demand model to keep its cache hit rate, consecutive events for the same role must land inside that window. Pipelines whose inter-event gap routinely exceeds 60 min would pay the cold cache cost on every spawn — that is acceptable for refine (low event volume) but stresses tester/coder (high event volume during NACK loops).
- **Cold-start latency budget.** A per-event pod spawn pays pod-scheduling + worktree-clone + gateway-session-setup + agent-startup latency that the long-lived model amortizes once per phase. The acceptance test must measure this end-to-end and confirm it stays inside an operator-defined budget. (See cq-1 below — operator-set.)
- **Gateway session model.** Today a long-lived pod holds one gateway session alive via heartbeat (`gateway/session_manager.py`, idle timeout 60 min, TTL 24 h). A per-event pod implies either session setup/teardown per spawn (extra latency + log noise) or some form of session reuse across a role's successive spawns (extra orchestrator state). The router-injection plumbing (`#2769` slice-2) is per-session, so the right design must respect that interface.
- **Worktree lifecycle.** The worktree is created once per phase + role today, on a persistent volume the pod mounts. A per-event pod must reuse the same worktree across its lifetime — re-cloning per spawn would dwarf the gateway-session cost and likely blow the cold-start budget. The mount pattern in `kubernetes_spawner.py` (per-role PVC) already supports this; the on-demand spawner must keep using it.
- **Heartbeat / idle-budget alert path stays operator-visible.** The 30-min idle-budget OVERSEER_ALERT (`stuck-phase-transition`) is a real signal operators rely on. If pods stop existing during idle, the **orchestrator** must take over that responsibility — same alert, different emitter — so operator UX does not regress.
- **`consensus_wrapper.py` retirement is part of the change.** The wrapper's responsibilities (event loop, heartbeat, idle budget, per-event invocation) are exactly what moves up. The plan must decide: delete the wrapper entirely and have the pod's entrypoint be a single `python3 -m egg_agent` invocation with the composed prompt, or keep a thin wrapper that still does heartbeat / signal handling. (See cq-5 below.)
- **No-op rollout shape.** This is a topology change that touches spawn, run, and terminate paths simultaneously. A behind-flag rollout (env or pipeline-config gate) lets operators flip per pipeline / per phase rather than all-at-once. (See cq-4 below — operator-set risk tolerance.)
- **Out of scope.** The agent primitive (pod image, worktree, Agent SDK, permissions, gateway restrictions) is untouched. The per-agent model routing from #2769 is also untouched — the on-demand spawner consumes the same `resolve_agent_model` decision per spawn. Overseer agents (decision-maker, advisor) are a separate lifecycle today (see cq-6 below).
- **File-size discipline (#2261).** `orchestrator/routes/pipelines.py` is ~16 K lines (mid-decomposition under #2261 slice-15 — see `orchestrator/CLAUDE.md`) and `consensus_wrapper.py` is ~800 lines but template-heavy. New on-demand-spawner symbols should land in a dedicated submodule under `orchestrator/` (candidate: `orchestrator/on_demand_spawner.py` or under the in-flight `routes/pipelines/_run_loop/` cluster) rather than accreting into the barrels.

## Options Considered

### Option A: Orchestrator-side per-event spawner + per-spawn worktree-PVC reuse (recommended)

**Approach.** The orchestrator's existing per-phase run loop in `routes/pipelines.py` adds a per-role inner tick that calls `_derive_next_action(tracker, role)` directly (the same function the route already exposes). On `action ∈ {propose, ack, nack, confirm}`, the orchestrator composes the per-event prompt by calling `compose_event_prompt` in-process (no HTTP round-trip), then invokes a new `OnDemandSpawner.spawn_event(role, prompt, model_decision)` that reuses `KubernetesSpawner.spawn_agent_job` with a one-shot command (`python3 -m egg_agent --model X --max-turns N` with the composed prompt) — **no wrapper bash, no in-pod loop**. The worktree-PVC for `(pipeline_id, role)` is created at phase start and **reused across every spawn for that role**, so worktree-clone latency is paid once per phase, not per event. The gateway session token is also created once per `(pipeline_id, role)` at phase start and re-attached as an env var on every spawn — so session setup is amortized too. The pod's only job is one agent invocation; it exits naturally when the agent does.

The heartbeat / idle-budget responsibility moves to the orchestrator: a per-role "no actionable event for X minutes" timer driven by the same tick that drives the spawn trigger.

**Pros**:
- Removes the in-pod bash loop entirely — one less moving part, no template indirection, no heartbeat subshell, no idle-budget bookkeeping inside a containerized bash script.
- Cold-start cost is bounded: worktree + gateway session are pre-created and reused; only pod-schedule + agent-startup are per-spawn.
- Cache-hit-rate-preserving: `compose_event_prompt` is unchanged; same stable prefix; cache lookups hit server-side regardless of pod identity.
- Reuses every existing primitive (`_derive_next_action`, `compose_event_prompt`, `spawn_specific_roles`, BRC memory file, `resolve_agent_model`). Net new code is the on-demand spawner module and the orchestrator-side idle-budget alert.
- Natural place to surface a per-spawn metric (latency, success rate, cache hit) for the SLO that decides whether on-demand is actually winning.

**Cons**:
- Touches the orchestrator's hot run-loop in `routes/pipelines.py` — a regression here affects every pipeline. Mitigated by: (a) feature flag on the spawn path so operators can revert per pipeline (see cq-4), (b) the in-flight #2261 slice-15 decomposition gives a cleaner place to land the new code than today's barrel.
- Per-role session reuse adds a small amount of orchestrator-side state (`{role: session_token}` per pipeline). Mitigated by: piggyback on the per-role worktree PVC lifecycle already tracked in `KubernetesSpawner`.
- The orchestrator must own the idle-budget alert that the wrapper owns today. Slight increase in orchestrator complexity, but the orchestrator already aggregates phase-level liveness for `pipeline_health_monitoring`.

### Option B: Lift only the wait, keep the pod long-lived (incremental refactor)

**Approach.** Leave the pod lifecycle untouched but move the `egg-orch message wait-loop` blocking call out of the wrapper bash and into a thin orchestrator-side push. The orchestrator notifies pods of actionable events via a websocket / SSE / signalfile, and the wrapper consumes the push instead of polling.

**Pros**:
- Smaller blast radius — no change to spawn / terminate paths.
- Preserves today's amortized worktree + session costs.

**Cons**:
- **Does not satisfy the issue's primary goal.** The pod is still long-lived; the CPU / memory reservation is unchanged; the gateway session is still pinned for the phase. The only thing recovered is the elimination of the long-poll round-trip, which is already cheap (60 s timeout, single HTTP call) and not the resource hog.
- Adds a push channel from orchestrator to pod that doesn't exist today, with reconnect / replay semantics to design and test — for a marginal win.
- Misses the second goal too: the system is still polling, just over a different transport.

### Option C: Status quo (do nothing, document the trade-off)

**Approach.** Accept that idle-but-heartbeating pods are the price of a warm worktree, warm session, and a single bash loop driving the protocol. Document the cost in `docs/architecture/orchestrator.md`. Punt on-demand spawning to a future iteration.

**Pros**:
- Zero code change. Zero rollout risk. Today works.

**Cons**:
- Leaves both stated goals unaddressed.
- The cost gets worse over time: every new phase concurrent-roles addition (e.g. adding a `documenter` role to refine) multiplies the idle-pod count linearly. The slice-DAG (#2137 hybrid) already spawns N parallel sub-pipelines, so the idle-pod count scales with slice count too.
- Foregoes the architectural cleanup that #2908 set up for (the comment at `consensus_wrapper.py:744-746` explicitly notes that a future revision could rework this).

### Option D: Per-event spawn with fresh worktree + fresh session each time (purist)

**Approach.** Same as Option A, but **do not** pre-create the worktree PVC or gateway session. Each pod spawn pays the full cold start: clone worktree, create gateway session, set up the agent, run one event, exit.

**Pros**:
- Strictly stateless between events — orchestrator state is trivial.
- No persistent-volume orchestration needed.

**Cons**:
- **Almost certainly blows the cold-start budget.** A fresh worktree clone on a fresh PVC costs single-digit seconds; a fresh gateway session creation + Anthropic credential resolve costs hundreds of ms; pod-schedule + agent-startup adds 5-10 s on a busy node. Summed: 10-20 s per spawn vs. ~3-5 s for Option A's pre-warmed variant.
- Hits the prefix cache hard: even though the cache is server-side and pod-independent, every fresh gateway session is treated as a new caller by Anthropic — the prefix-match path may still hit, but per-session rate-limit headroom is reset, and the LiteLLM router's per-session "model-aware credential" cache is invalidated.
- Magnifies the supply-chain blast radius from #2769: every spawn re-resolves credentials and re-validates LiteLLM upstream.

## Recommended Approach

**Option A.**

It is the only option that:

1. Eliminates idle pods (the stated resource-use goal).
2. Makes spawning event-triggered, not poll-driven (the stated architecture goal).
3. Keeps the cold-start cost within plausible budget by reusing the worktree PVC and the gateway session per `(pipeline_id, role)`.
4. Reuses every existing per-event primitive (`_derive_next_action`, `compose_event_prompt`, `spawn_specific_roles`, durable BRC memory) without re-implementing them.
5. Leaves the BRC protocol, the per-agent model routing (#2769), and the agent primitive itself untouched.

Option B fails goal 1; Option C fails both goals; Option D is the same shape as A but blows the cold-start budget. The remaining open questions are operator-decidable scope/intent items (cold-start budget, rollout shape, retirement scope) and are surfaced below.

## Runtime-primitive assumptions for the downstream plan

| Primitive | Where it lives today | Used by | Execution context |
|---|---|---|---|
| `_derive_next_action(tracker, role) -> (action, event_payload, reason)` | `orchestrator/routes/consensus.py:280-406` | `POST /<pipeline_id>/consensus/next-action`; in-pod wrapper via `egg-orch brc next-action` | Orchestrator pod |
| `_VALID_ACTIONS = {wait, propose, ack, nack, confirm, complete}` | `orchestrator/routes/consensus.py:77` | Spawn-trigger contract | Orchestrator pod |
| `compose_event_prompt(role, event_payload, memory_excerpt, nacks, git_log_delta, base_branch) -> str` | `orchestrator/routes/event_prompt.py:337-447` | `consensus_wrapper.py:400-449` `invoke_agent_for_event` | In-pod today; would move in-process orchestrator under Option A |
| Per-role BRC memory at `.egg-state/agent-outputs/<role>/brc-memory.md` | Written by agent's `brc_ack` / `brc_nack` handlers; read by `event_prompt.py:486` | `compose_event_prompt` | On per-role worktree PVC |
| `_EVENT_PUMP_WRAPPER_TEMPLATE` bash template | `consensus_wrapper.py:102-715` | `build_event_pump_wrapped_command` | In-pod — candidate for full retirement (cq-5) |
| `build_consensus_wrapped_command(prompt_text, model, max_turns)` | `consensus_wrapper.py:770-802` (alias for `build_event_pump_wrapped_command`) | `concurrent_executor.py:489`, `routes/pipelines.py` restart path | Orchestrator pod (composes the command) |
| `ConcurrentExecutor.spawn_all(agent_prompts)` | `orchestrator/concurrent_executor.py:323-376` | Orchestrator run loop at phase start | Orchestrator pod |
| `ConcurrentExecutor.spawn_specific_roles(roles, agent_prompts)` | `orchestrator/concurrent_executor.py:378-400` | Transient-failure retry path (#1879) — does not touch tracker | Orchestrator pod — natural seam to reuse for per-event spawn |
| `ConcurrentExecutor._spawn_agent(role, prompt_text)` | `orchestrator/concurrent_executor.py:445-524` | `_spawn_roles` thread-pool | Orchestrator pod |
| `resolve_agent_model(role, pipeline_config, repo) -> AgentModelDecision` | `#2769` slice-2 | `_spawn_agent` (`concurrent_executor.py:472-485`) | Orchestrator pod |
| `KubernetesSpawner.spawn_agent_job(role, branch, extra_env, command, upstream?, upstream_model?)` | `orchestrator/kubernetes_spawner.py` | `_spawn_agent` via `self.spawn_fn` | Orchestrator pod |
| `_PROTECTED_ENV_KEYS` (incl. `EGG_SESSION_TOKEN`, `GATEWAY_URL`, `EGG_AGENT_ROLE`, `EGG_SLICE_ID`, `EGG_BASE_BRANCH`, `EGG_WAIT_PRODUCER_ALLOWLIST`, `EGG_BRANCH`) | `orchestrator/kubernetes_spawner.py:138-201` | Pod env composition | Orchestrator pod |
| Gateway session: token, idle timeout (60 min default), TTL (24 h), refresh on `validate_session` | `gateway/session_manager.py` (idle timeout const `DEFAULT_SESSION_IDLE_TIMEOUT_MINUTES`, TTL const `DEFAULT_SESSION_TTL_HOURS`) | Pod traffic through gateway | Gateway pod (shared) |
| Per-role worktree PVC mount | `orchestrator/kubernetes_spawner.py` | Per-pod mount path | k8s cluster |
| Idle-budget OVERSEER_ALERT (`stuck-phase-transition`, 1× / 2× thresholds) | `consensus_wrapper.py:531-549` | Operator alert path | In-pod today — would move to orchestrator under Option A |
| 30 s wrapper-owned heartbeat (`EVENT_PUMP_HEARTBEAT_INTERVAL_SECS_DEFAULT`) | `consensus_wrapper.py:68, 188-234` | Gateway session refresh, overseer liveness | In-pod today — gateway session refresh would move to orchestrator-side keep-alive under Option A |
| `egg-orch consensus confirmed` / `egg-orch brc next-action` CLI | `sandbox/egg_lib/orch_cli.py` (via gateway) | In-pod wrapper today | In-pod today — orchestrator can hit `_derive_next_action` in-process instead |
| Consensus tracker reconstruct-from-messages fallback | `orchestrator/routes/consensus.py:111-154` | Survives orchestrator restart | Orchestrator pod |

## Open Questions

### Resolved in Pre-Refine

_(None — the issue body's "Open considerations" section names these explicitly as not pre-decided; every question below is genuinely open and operator-decidable.)_

### Decisions

<!-- egg-decision id=cq-1 -->

**What is the acceptable cold-start latency budget for a per-event pod spawn (from `_derive_next_action != "wait"` to the agent process running)?**

The on-demand model trades amortized warm cost for per-event cold cost. The plan needs an operator-set budget so the acceptance test can verify the design before rollout. WS7 observed legitimate-idle gaps of 10–13 min between actionable events in a typical refine/plan phase; in implement phase, tight NACK→re-propose loops can run multiple events per minute.

- [ ] ≤ 3 s p95 — tight budget; effectively requires worktree-PVC pre-warm AND gateway-session reuse (Option A's full optimization). Any larger overhead than agent-process startup is rejected
- [ ] ≤ 8 s p95 — loose budget; allows per-spawn gateway-session setup (worktree still pre-warmed). Simpler orchestrator state but slower per-event cycle
- [ ] ≤ 20 s p95 — generous; would allow Option D (fresh PVC + fresh session per spawn) at the cost of a measurably slower BRC cycle
- [ ] No fixed budget — measure-then-decide post-rollout, gated on a kill-switch flag
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-2 -->

**How should the gateway session be managed across a role's successive on-demand spawns?**

Today a long-lived pod holds one session via 30 s heartbeat. Under Option A the orchestrator could either (a) create one session per `(pipeline_id, role)` at phase start and refresh it from the orchestrator until phase end, reusing it across every spawn for that role; (b) create a fresh session per spawn and accept the per-event credential-resolve cost; or (c) hold a single orchestrator-wide session and impersonate roles via headers.

- [ ] Per-role session held by the orchestrator for the whole phase, refreshed by an orchestrator-side keep-alive (mirrors today's lifecycle but moves the heartbeat emitter; reuses today's `gateway/session_manager.py` semantics)
- [ ] Fresh session per spawn (simpler orchestrator state, more gateway log noise, pays credential resolve every event)
- [ ] Orchestrator-wide session multiplexed across all roles (smallest gateway-side footprint, but breaks today's per-role audit and per-role model routing from #2769)
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-3 -->

**Under on-demand spawning, who owns the "no actionable event for X minutes" idle-budget overseer alert that today fires from inside the wrapper at the 30-min `EGG_BRC_IDLE_BUDGET_MIN`?**

The alert is operator-visible (`stuck-phase-transition` anomaly tag at 1× and 2× thresholds, `consensus_wrapper.py:531-549`). With no pod to host the timer, the orchestrator must take over — or the alert must change shape.

- [ ] Orchestrator-side per-role timer that emits the same anomaly tag with the same thresholds (drop-in replacement; operator UX unchanged)
- [ ] Replace with a phase-level "no spawns for X minutes" alert at the orchestrator (different signal — fires once per phase regardless of role count, simpler to operate but loses per-role granularity)
- [ ] Drop the idle-budget alert entirely; rely on the existing phase-timeout SLO (smallest code, weakest signal)
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-4 -->

**What is the rollout shape?**

The change touches spawn / run / terminate paths simultaneously. A behind-flag rollout lets operators flip per pipeline / per phase during burn-in; a big-bang rollout matches #2908's "no env-flag rollback, revert by git" stance (see `consensus_wrapper.py:36-41`).

- [ ] Feature-flagged per-pipeline (e.g. `PipelineConfig.on_demand_spawn=True/False`, default False initially, flip to True after burn-in) — operator can pin a known-good pipeline to the long-lived model while the on-demand path soaks elsewhere
- [ ] Feature-flagged per-phase (e.g. enable for refine only first, since it has the lowest event volume and the cleanest cache hit pattern) — graduated rollout per phase
- [ ] Big-bang with rollback-by-git-revert (mirrors #2908 slice-4 stance) — smallest config surface, requires a clean revert path in the PR
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-5 -->

**What is the retirement scope for `orchestrator/consensus_wrapper.py` after on-demand spawning is the default?**

Today the wrapper does five things: event loop, heartbeat, idle budget, agent invocation, signal-handler / SIGTERM cleanup. Under Option A the first three move to the orchestrator. Agent invocation collapses to a single `python3 -m egg_agent` exec. Signal handling could stay in a thin wrapper or be folded into the agent's own startup.

- [ ] Delete `consensus_wrapper.py` entirely; the pod entrypoint becomes `python3 -m egg_agent --model X --max-turns N` with the composed prompt on stdin (smallest in-pod surface)
- [ ] Keep a thin one-shot wrapper that handles only signal trapping + heartbeat-once-on-start + agent invocation (preserves the kubelet SIGTERM contract that today's wrapper handles via the `cleanup` trap at `consensus_wrapper.py:117-124`)
- [ ] Keep the full wrapper but skip the loop when an `EGG_ON_DEMAND=1` env var is set (graceful migration with both paths coexisting until burn-in completes; doubles the test surface)
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-6 -->

**Are overseer agents (decision-maker, advisor — see `orchestrator/overseer/monitor.py`) in scope for on-demand spawning, or out of scope for this change?**

Overseer agents have a different lifecycle today: they are spawned by `orchestrator/overseer/monitor.py` on a different cadence (event-driven on anomalies + periodic), not via `concurrent_executor.spawn_all`. They are already arguably on-demand. They share the gateway session/credential plumbing but not the BRC consensus loop.

- [ ] Out of scope — leave overseer agent lifecycle as-is; this issue is about BRC-loop roles only
- [ ] In scope — unify the spawn path so overseer agents go through the same `OnDemandSpawner` (cleaner long-term shape, more diff in this PR, may require fixing edge cases the overseer's bespoke path already solved)
- [ ] In scope, but as a follow-up issue — note the unification target in this issue's closeout but defer the work
- [ ] Other (explain in reply)

### Feedback

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: What is the concrete resource-saving target operators care about? (e.g. "halve the idle-pod-minutes per phase", "support 10× current concurrent pipelines on the same k3s node", "reduce per-phase memory reservation by X%"). This shapes how aggressively the acceptance test must drive the cold-start budget down vs. accept a higher latency for a cleaner code shape.**

> _Your answer here_

**Q2: Are there any pipelines or repos currently relying on the in-pod wrapper's debug-shell-able-ness during incidents? (e.g. `kubectl exec` into a stuck wrapper, inspect the heartbeat subshell, read AGENT_OUTPUT_LOG). If so the on-demand model loses that affordance — would a per-spawn pod log retained in `.egg-state/` be a sufficient replacement, or do operators need a long-lived debug shim?**

> _Your answer here_

**Q3: The plan will rely on per-role worktree PVCs already being reusable across multiple spawns. Is there any in-flight work (#2866 k3s-as-sole-runtime, #3017 declarative phase/stage abstraction) that is about to change the PVC lifecycle or the spawner's worktree-mount pattern in a way the plan should anchor on?**

> _Your answer here_

**Q4: The issue references #2958 (streaming per-task commits / producer lifecycle) as adjacent. Does the on-demand spawner need to coexist with a producer that "keeps working" across multiple commits in one logical session, or does the per-event spawn model assume one event ≈ one commit / one BRC verb cleanly?**

> _Your answer here_

**Q5: The current 30-min `EGG_BRC_IDLE_BUDGET_MIN` was tuned for the in-pod wrapper (`consensus_wrapper.py:52-58` comment). Once the orchestrator owns the equivalent timer, is there appetite to tighten or widen it (e.g. tighten to 10 min now that there's no risk of a wrapper-side false positive, or widen to 60 min to match the prefix-cache TTL so we don't alert on legitimately-idle phases that are still within cache window)?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

## Complexity Assessment

**high** — this is a cross-cutting architectural change. It touches:

- the orchestrator's per-phase run loop in `routes/pipelines.py` (mid-decomposition under #2261 slice-15)
- the spawn path in `concurrent_executor.py` and `kubernetes_spawner.py` (per-spawn worktree + session reuse semantics)
- the retirement of `consensus_wrapper.py` (or a substantial trimming of it — see cq-5)
- a new orchestrator-side timer for the idle-budget alert that today fires from inside the wrapper
- a new operator-set SLO (cold-start budget — see cq-1) plus an acceptance-test surface that measures it
- the interaction with the per-agent model routing (#2769) and the credential-injection / gateway-session machinery (cq-2)
- a rollout decision (cq-4) with implications for how revertible the PR must be

It is decomposable into at least three independently-shippable slices (orchestrator-side per-role timer + alert relocation; on-demand spawner module + per-event prompt composition in-process; consensus_wrapper.py retirement + entrypoint simplification), but per the analysis-template guidance, the slice-DAG shape is the architect's call in the plan phase. The decisions above are the operator-decidable scope/intent inputs the architect will rely on.

---

*Authored-by: egg*
