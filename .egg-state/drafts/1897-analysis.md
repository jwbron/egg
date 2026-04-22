# Analysis: Agent wait heuristics — replace sleep/poll loops with event-driven BRC message stream consumption

> Issue: #1897 | Phase: refine

## Problem Statement

During the `issue-1762-membump` pipeline run on 2026-04-22, the orchestrator observed multiple agents using suboptimal wait heuristics that produced one or more of the following pathologies (verbatim timestamps and quotes from the issue body):

1. **Bus pollution from confirmation retry-loops** — `architect` emitted ~20 duplicate `CONSENSUS_CONFIRMED (pending_acks)` messages between `21:10:19 → 21:12:37` (one every ~5s) by wrapping `egg-orch consensus confirmed` in a `for i in 1 2 3 4 5 6 7 8 9 10; do ... done` shell loop.
2. **Multi-minute blocking sleep** — `tester` ran `sleep 300 && egg-orch consensus status 2>&1 && git fetch origin ...`, blackholing the agent for the full 5-minute window so it could not receive NACKs, overseer nudges, or peer proposals.
3. **Multi-iteration poll loops where the underlying primitive is already blocking** — `documenter` ran an 8-iteration loop of `egg-orch message poll --wait 60` starting at `22:22:14`. A NACK addressed to documenter had arrived 6 minutes earlier at `22:16:08`, and an overseer nudge arrived at `22:22:42` during the loop. Documenter's tail logs showed no activity from `22:22:14 → 22:32:29` (10+ minutes of silence) while both messages sat in its inbox. The pipeline only continued because a host-side nudge eventually woke the agent up.
4. **Free-form chatter on the bus via the QUESTION type** — `tester` posted subject `"Tester orienting - any ETA?"` at `22:11:23`. No agent is wired to reply, and the message just sits.

**Desired outcome.** Agents should react to bus events within seconds rather than the 5–10-minute sleep/poll-loop windows currently observed. BRC consensus turn-around should be dominated by reasoning latency, not agent-side sleep heuristics. The orchestrator overseer should not have to nudge live agents to surface state changes that are already in their inbox.

## Current Behavior

### Where the BRC lifecycle prompt comes from

The "## CRITICAL: BRC Consensus Protocol" preamble injected into every concurrent-mode agent's prompt is assembled in `orchestrator/routes/pipelines.py` lines 5905–6038. The producer lifecycle ends with:

```
6. **STAY ALIVE**: Keep polling `egg-orch message poll --wait 30`
   until the orchestrator stops you.
```

…and the reviewer lifecycle ends symmetrically (line 6020–6021). `sandbox/agent-config/rules/mission.md:152` reinforces the same instruction:

> Use `egg-orch message poll --wait 30` for long-polling (not sleep loops)

The phrase **"keep polling"** is what nudges LLM agents into the `for i in 1..N; do egg-orch message poll --wait 60; done` shape — it sounds like a recurring action the agent itself must orchestrate.

### What `--wait` actually does

`GET /api/v1/pipelines/{id}/messages?wait=N` (`orchestrator/routes/messages.py:140–184`) caps `wait` at **60s** (line 165). With the **Redis Streams** backend (`orchestrator/redis_message_store.py:182–188`) it issues `XREAD BLOCK wait*1000`, which returns **immediately** when a matching message arrives or after the timeout. With the **in-memory** store the wait kwarg is dropped (`routes/messages.py:181–184`) and the call returns immediately with an empty list.

So when Redis is in play, **a single `--wait 60` call is event-driven** — the agent does not need to wrap it in a loop to react quickly. The 8-iteration loop in `documenter` was harmful precisely because each call was a fresh long-poll that could not surface messages that were already buffered (and the cap on `wait` is 60s so the agent cannot block for the full stay-alive window in one shot).

### `egg-orch consensus confirmed` idempotency

PR #1896 (commit `ae9535b99`, merged just before this issue was filed) added duplicate-emission protection. Full call stack for `egg-orch consensus confirmed`:

1. **CLI entry** — `sandbox/egg_lib/orch_cli.py:1452–1480` (`cmd_consensus_confirmed`) POSTs `{"signal_type": "consensus_confirmed", "agent_role": role}` to `/api/v1/pipelines/{pid}/signal`.
2. **Signal dispatch** — `orchestrator/routes/signals.py:187` maps `"consensus_confirmed"` → `handle_consensus_confirmed_signal`. (A second mapping at line 1784 covers the SSE variant.) No bypass path exists — every call goes through this handler.
3. **Dedup guard** — `handle_consensus_confirmed_signal` (line 1297) calls `_existing_confirmed_for_role` (line 1241–1294) to fetch `(has_final, has_pending)` from the message store, then skips the bus write when a prior `CONFIRMED` of the same flavor exists (line 1435 for `pending_acks`, line 1456 for `final`).

So the `architect`-style retry loop **should no longer** pollute the bus under the current code. That said, the issue's observation was on a pipeline that ran very close to the merge of #1896, so we should still verify whether the symptom persists under the fixed build (see `feedback-1` Q1) before declaring item #3 closed.

Note also that a separate read-only command **already exists**: `egg-orch consensus status` (`sandbox/egg_lib/orch_cli.py:1452–1525`) is a pure GET against `/api/v1/pipelines/{pid}/status`. There is no need to introduce a new "query" form — the issue's item #3 second clause is already satisfied.

### The consensus_wrapper stay-alive loop

`orchestrator/consensus_wrapper.py:322–351` already implements a 30-second `sleep` poll loop at the shell level, capped at `MAX_READY_POLLS` cycles (default 10 → 5 minutes total). This is the *wrapper's* loop, not the agent's, but it has the same cache-cost / latency profile as the agent-side patterns this issue critiques.

### Message types currently defined

`orchestrator/message_store.py:19–37` defines: `PROGRESS`, `QUESTION`, `STATUS`, `AGENT_FAILED`, `HANDOFF`, `CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `CONSENSUS_WITHDRAW`, `CONSENSUS_CONFIRMED`, `CONSENSUS_RE_REVIEW`, `OVERSEER_ALERT`, `NUDGE`. There is **no per-agent heartbeat** type and no readiness/state broadcast (`WORKING | WAITING_ON_ROLE | PROPOSED | IDLE`). `QUESTION` is only used in test fixtures (`tests/shared/egg_contracts/test_checkpoint_cli_inter_agent.py:77,120`); no production agent currently sends or replies to one.

### Documentation

`docs/guides/concurrent-execution.md` covers the BRC protocol thoroughly (200+ lines) but does not have a section explicitly named "how to wait" or "wait anti-patterns". `docs/reference/agent-wait-patterns.md` does **not** exist (the issue calls for it as work item #4).

## Constraints

- **No backwards-incompatible change to the bus schema.** Pipelines mid-flight at deploy time must continue to work with whatever message types were defined at their start.
- **In-memory store fallback must not regress.** Tests rely on `EGG_MESSAGE_STORE_BACKEND=memory`. Whether we patch the silent-fallback behavior is itself a decision (see `decision-4`). **Inter-decision dependency:** if `decision-1` picks an option that includes a new blocking primitive AND `decision-4` picks "leave as-is", then CI tests running with `EGG_MESSAGE_STORE_BACKEND=memory` will silently exercise the non-blocking path and produce false green. These two decisions must be resolved together.
- **`--wait` blocking is an HTTP connection cost.** Raising the 60s cap means the orchestrator API holds open more long-lived connections per agent. Concrete load figures: there are typically 3–7 concurrent agents per pipeline (producer + reviewers + overseer), each holding one long-lived poll socket; a typical multi-pipeline deployment runs O(10) concurrent pipelines → order-of-magnitude 30–70 simultaneous long-poll sockets. The `HTTP_PROXY` idle timeout in the sandbox's gateway (`HTTP_PROXY=gateway.egg-system.svc.cluster.local:3129`) is the operative cap — raising `--wait` beyond that will produce spurious 504s unless the gateway timeout is raised in lockstep.
- **Redis is the only event-driven backend.** XREAD BLOCK is what makes `--wait` actually event-driven. Any new "wait for type" primitive must work over the same XREAD plus client/server filtering, or be implemented as SSE.
- **Agent prompt is shared between models.** The BRC preamble is rendered for every concurrent-mode role; a wording change applies to all of them. Tests in `orchestrator/tests/test_pipeline_prompts.py` lock in the current "STAY ALIVE" / lifecycle structure and will need updating — the relevant test functions are `test_reviewer_lifecycle_renumbered`, `test_directed_coordination_after_reviewer_lifecycle`, `test_directed_coordination_after_producer_lifecycle`, and the lifecycle-order assertions near those. (Line numbers drift between commits; search by test name.)
- **Container lifecycle (decision-8).** Replacing the `consensus_wrapper.py` shell sleep loop with a long XREAD BLOCK (or SSE listener) changes shutdown semantics: a blocked Redis connection must respect SIGTERM and exit the shell within the orchestrator's graceful-shutdown grace period, otherwise the orchestrator's kill path (`SIGKILL` after grace) masks a clean "consensus reached" exit. This cuts across the wrapper's current `exit 0`/`exit 1` classification logic.
- **Issue #1890 is closely related** — the overseer needed to manually intervene because agents weren't observing state changes promptly. Fewer agent sleeps → fewer overseer interventions → less work for the Tier 1 health check.

## Options Considered

### Option A: Tighten prompt language and add a wait-patterns doc only (no code changes)

**Approach.** Edit the BRC preamble in `orchestrator/routes/pipelines.py` to replace "Keep polling …" with "After confirming, run `egg-orch message poll --wait 60` in a *single* `while true; do … done` loop — the call already blocks until a message arrives or the timeout elapses. Do NOT use `for i in 1..N`. Do NOT call `sleep N`." Add explicit prohibitions in `sandbox/agent-config/rules/mission.md`. Write `docs/reference/agent-wait-patterns.md` as a one-page guide. Update the test fixtures.

**Pros:**
- Smallest blast radius — text-only changes, no schema or API change.
- Addresses items #1 (audit prompts) and #4 (docs) cleanly; relies on the already-merged #1890 idempotency fix to cover item #3.
- LLMs follow explicit "don't" instructions reasonably well in current frontier models.

**Cons:**
- Prompt-only fixes are easy to regress. A future prompt edit could re-introduce a sleep idiom without tripping any guard.
- Does not raise the 60s `--wait` cap, so agents still need *some* outer loop. The line between "single while-true loop" and "for i in 1..N" is fuzzy.
- Doesn't address item #2 (no blocking-on-typed-message primitive) or #5 (heartbeats).

### Option B: Add a typed blocking primitive + tighten prompts + docs

**Approach.** Build `egg-orch message wait --for TYPE [--from ROLE] [--timeout N]` (or extend `message poll` with `--until-type TYPE`) that hits a server route which loops over `XREAD BLOCK` server-side until a matching message is seen. Server-side wait can be hours; client gets a single response. Update prompts to recommend `egg-orch message wait --for CONSENSUS_REACHED` for stay-alive, `egg-orch message wait --for CONSENSUS_NACK` while waiting on reviews, etc. Write `agent-wait-patterns.md`. Optionally raise the cap on `message poll --wait`.

**Pros:**
- Eliminates the ambiguity that drives the loop pattern: agents have a single command that returns when something *actionable* happens.
- Clean for both LLM agents and humans debugging — the command name says what you're waiting for.
- Reduces bus chatter and HTTP request volume.

**Cons:**
- New API surface to design, document, version, and test.
- Server holds open very long connections (potentially hours) per agent, which interacts with deployment topology (load balancer idle timeouts, container shutdown grace periods).
- Doesn't address #5 (heartbeats) — orchestrator still has to *probe* agent liveness rather than receive it.

### Option C: Add per-agent state heartbeats + everything in B

**Approach.** Define a new `AGENT_STATUS` (or extend `STATUS`) message type with a structured `state` field (`WORKING | PROPOSED | WAITING_ON: <role> | CONFIRMED | IDLE`). Agents emit on every state transition (not periodically). The overseer's Tier 1 health check reads these directly instead of inferring from message timing. Combine with Option B's blocking primitive and the prompt/docs work.

**Pros:**
- Solves the "is it dead or just sleeping?" problem at the source — overseer doesn't need to nudge.
- Eliminates the need for the `consensus_wrapper.py` shell sleep loop too (it can listen for `is_complete`).
- Long-term path to richer agent observability (frontend dashboards, etc.).

**Cons:**
- Largest scope. Touches message store schema, all agent role prompts, the consensus_wrapper, and the overseer monitor.
- Risk of "heartbeat noise" if not carefully tied to actual state transitions vs. fixed intervals.
- Most rework to roll back if it doesn't pan out.

### Option D: Minimal/safe — verify #1890 fix landed, ship docs, no other code

**Approach.** Confirm the `consensus confirmed` dedup fix is sufficient by replaying the pathological pipeline (or instrumenting a synthetic test). Write `docs/reference/agent-wait-patterns.md` documenting the *current* idiom (single `while true; egg-orch message poll --wait 60`). Update `mission.md` to point at it. No prompt-preamble change in `pipelines.py`, no new CLI, no new message types.

**Pros:**
- Lowest risk. Almost certainly safe to ship.
- Lets us measure whether prompt audits + docs alone change agent behavior before investing in API design.

**Cons:**
- Doesn't solve the "agents still wrap things in for-loops" root cause if the docs aren't loaded into the system prompt or aren't surfaced when the agent is first told to "stay alive".
- Defers items #2 and #5 indefinitely.

## Recommended Approach

**Option B** is the recommended primary path, with the prompt audit (Option A) folded in as a prerequisite step and documentation (Option D's strength) included as standard hygiene.

Reasoning:

- The root cause is **not** that agents don't know polling is blocking — it is that the cap on `--wait` (60s) and the phrasing "keep polling" together push agents into outer loops, and once you have an outer loop the LLM picks shapes (`for i in 1..N`, `sleep 300 && …`) that come from training-data idioms rather than from anything we taught it.
- A typed wait command (`egg-orch message wait --for CONSENSUS_REACHED`) collapses the entire stay-alive loop into one command. There is nothing for the agent to wrap in `for i in …` — there is no iteration to perform.
- The idempotency fix in #1890 (already merged) covers item #3, and Option B bundles items #1, #2, and #4. **Item #5 (heartbeats) is deferrable** — once Option B is in place, overseer-side liveness inference is a cleaner problem to attack later.
- Option C is attractive but carries enough scope risk that we should validate the simpler primitive first.

The recommendation is contingent on the answers to `decision-1` (scope), `decision-2` (CLI shape), and `decision-3` (whether to raise the `--wait` cap independently).

**Explicit fallback trigger:** if `decision-1` returns "Items 1, 3, 4 only (prompt audit, idempotent CLI verification, docs) — minimal/safe scope", pivot to **Option D** (skip the new blocking primitive entirely and just ship the prompt audit and docs). If `decision-1` returns "Items 1, 4, 5" (observability-focused), shift to **Option C** (heartbeats + prompts/docs, no new wait primitive). If `decision-1` returns "All five" (full scope), implement Option C but plan the heartbeat subsystem as its own parallel track in the plan phase.

## Open Questions

The following decisions and feedback items have been registered with `egg-contract`. Each must be resolved before the plan phase begins.

<!-- egg-hitl-decision id=decision-1 -->

**What is the scope for issue #1897 — which of the five proposed work items should be in scope?**

- [ ] All five items (audit prompts, blocking primitive, idempotent CLI, docs, heartbeats) — full scope
- [ ] Items 1, 3, 4 only (prompt audit, idempotent CLI verification, docs) — minimal/safe scope
- [ ] Items 1, 2, 4 (prompt audit + new blocking primitive + docs) — UX-focused scope
- [ ] Items 1, 4, 5 (prompt audit + docs + heartbeats) — observability-focused scope
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-2 -->

**How should the new blocking primitive be exposed (if in scope)?**

- [ ] Add new dedicated CLI: egg-orch message wait --for TYPE [--from ROLE] [--timeout N]
- [ ] Extend existing message poll with --until/--for filters: egg-orch message poll --wait N --until-type TYPE
- [ ] No new primitive — fix only by tightening prompt instructions and documentation
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**Should the 60-second cap on message poll --wait be raised?**

*Cost context: with 3–7 agents per pipeline × O(10) concurrent pipelines, the orchestrator holds order-of-magnitude 30–70 simultaneous long-poll sockets. The binding constraint is the sandbox HTTP proxy idle timeout (`HTTP_PROXY=gateway.egg-system.svc.cluster.local:3129`), which must be raised in lockstep with the cap or requests will 504.*

- [ ] Keep 60s cap (force agents to re-poll, server holds fewer long blocking connections)
- [ ] Raise to 300s (5 min) — matches consensus_wrapper MAX_READY_POLLS interval
- [ ] Raise to 600s (10 min) — minimize bus chatter, rely on Redis XREAD BLOCK semantics
- [ ] Make configurable via env var (EGG_MESSAGE_POLL_MAX_WAIT) with current 60s default
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**How should the existing in-memory message store behave for --wait > 0?**

*Coupling note: if `decision-1` includes a new blocking primitive and this decision picks "Leave as-is", any CI test using `EGG_MESSAGE_STORE_BACKEND=memory` will silently exercise the non-blocking path and false-green. Resolve `decision-1` and `decision-4` together.*

- [ ] Leave as-is: silent fallback to non-blocking (test environments only)
- [ ] Implement true blocking via condition variable on the in-memory store
- [ ] Add a server-side sleep-until-empty-or-message loop in the route as fallback
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**What should happen with the QUESTION message type?**

- [ ] Remove it — it's only used in tests, encourages off-protocol chatter
- [ ] Formalize as a heartbeat/STATUS channel with required structured fields
- [ ] Keep but document that it is best-effort and unreplied — only for human triage
- [ ] Replace with a typed REQUEST/REPLY pattern that names a target peer and times out
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-6 -->

**⚠️ SUPERSEDED — please answer decision-7 instead.**

*This decision was registered twice against the contract due to a shell-quoting bug during `egg-contract add-decision` (the first invocation had its option text mangled by bash command substitution on inline backticks). The contract has no deletion mechanism; this block exists so the HITL gate can render a comment for `decision-6` and complete the phase. Decision-7 below carries the canonical question and correct option text. Please tick "Superseded — see decision-7" here.*

- [ ] Superseded — see decision-7 (Recommended)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-7 -->

**Should the agent prompt regression-guard against sleep/for-loop anti-patterns, and how aggressively?**

- [ ] Yes — add explicit Don'ts to the prompt preamble: "Do NOT use for-loops to wrap message poll; do NOT call sleep N to wait; rely on --wait blocking semantics"
- [ ] No — positive guidance only ("use a single while loop with --wait 30") and trust the LLM
- [ ] Yes plus a sandbox-side enforcement: gateway rejects bash commands matching "sleep [0-9]+ &&" followed by an orch CLI invocation
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-8 -->

**Should the consensus_wrapper.py "stay alive" loop (currently MAX_READY_POLLS × 30s = 300s) be replaced with event-driven blocking on consensus completion?**

- [ ] Replace shell sleep loop with a long XREAD BLOCK or SSE listener tied to is_complete signal
- [ ] Keep shell loop but extend MAX_READY_POLLS or accept the suggested poll interval from message_poll_hint_seconds
- [ ] Out of scope for this issue (the wrapper is internal, not the source of agent-side patterns)
- [ ] Other (explain in reply)

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: The 'consensus confirmed' idempotency fix from PR #1896 (commit ae9535b9) was merged just before this issue was filed. Was the bus pollution observed in pipeline issue-1762-membump from BEFORE or AFTER that fix landed? If after, the dedup logic in routes/signals.py:1241-1294 (_existing_confirmed_for_role) needs another bug fix; if before, item #3 in the proposed work may already be done.**

> _Your answer here_

**Q2: How important is supporting the in-memory message store backend for true blocking (--wait > 0)? Currently the in-memory store silently falls back to non-blocking (routes/messages.py:181-184). Is this only a concern for tests, or do production deployments ever run without Redis?**

> _Your answer here_

**Q3: Are there any constraints on adding new message types (like a new HEARTBEAT type) or new CLI subcommands? Backward compatibility with current pipelines mid-flight, schema versioning of the message store, etc.**

> _Your answer here_

**Q4: Should the proposed instrumentation be a separate observability subsystem (#1897 item 5: agent heartbeats with WORKING/WAITING_ON_ROLE/PROPOSED/IDLE) or piggyback on the existing PROGRESS message type? PROGRESS already covers 'what you're doing'; STATUS covers 'who you're waiting on' is the gap.**

> _Your answer here_

**Q5: What is the desired UX for an LLM-driven agent that should NOT loop or sleep — should we ship a single recommended idiom (e.g. 'while true; do egg-orch message poll --wait 60 --until consensus_reached || break; done') and ban everything else, or document several patterns and trust the agent to choose?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

## Complexity Assessment

**high**

The work touches multiple subsystems even at the recommended Option B scope:

- Agent system-prompt scaffolding (`orchestrator/routes/pipelines.py`, `sandbox/agent-config/rules/mission.md`).
- A new CLI subcommand (`sandbox/egg_lib/orch_cli.py`).
- A new HTTP route + Redis XREAD BLOCK plumbing (`orchestrator/routes/messages.py`, `orchestrator/redis_message_store.py`, `orchestrator/message_store.py`).
- New documentation (`docs/reference/agent-wait-patterns.md` and a section in `docs/guides/concurrent-execution.md`).
- Test updates across `orchestrator/tests/test_pipeline_prompts.py`, `orchestrator/tests/test_concurrent_integration.py`, and message-store tests.
- Optional (decision-dependent) work on the consensus wrapper, in-memory store, and `QUESTION` type.

These are largely independent and could be parallelized in the implement phase: prompt edits + tests can land separately from the new CLI/route, and the docs are unblocked by either landing first. If the human picks the maximal scope (Option C with heartbeats), this is **clearly high**; even Option B is high because of the cross-cutting nature of prompt / CLI / route / docs / tests changes.

---

*Authored-by: egg*
