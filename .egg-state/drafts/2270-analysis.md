# Analysis: Cleanup: holistic overseer overhaul — phase-peer lifecycle, signal coverage, file_issue path

> Issue: #2270 | Phase: refine

## Problem Statement

The overseer subsystem (`orchestrator/overseer/`, `orchestrator/health_checks/`, `sandbox/overseer_monitor.py`, `shared/egg_overseer/`, plus lifecycle hooks in `orchestrator/routes/pipelines.py`) has accreted complexity across eight prior issues (#2096, #2272, #2284, #2290, #2296, #2300, #2442, #2797) and a long tail of point fixes. The current state is:

- `monitor.py` is ~2050 lines with 53 methods and 43 try/except blocks that silently swallow errors at debug level
- `issue_filer.py` is dead code but retained for a test regression anchor
- Health check results flow through an EventBus that no production handler subscribes to — the only production consumer is the container monitor inspecting returned `HealthResult` lists directly
- The auto-file-issue path has been silent in shadow mode since rollout not because pipelines are healthy, but because the conjunctive gate (Haiku ≥ 0.8 + Tier-1 alert + advisor `file_issue` + dedup miss) is starved upstream: many failure modes never produce a Tier-1 alert at all
- Overseer lifecycle is nominally phase-scoped but spawn ordering, teardown ordering, respawn semantics, and restart-state alignment are enforced by repetition across four teardown call sites rather than by a single seam

The desired outcome is a cleaned-up overseer that:
1. Has explicit lifecycle alignment with phase agents — spawn with agents, tear down with agents, no overseer between phases, no state surviving phase boundaries
2. Closes signal-coverage gaps so failures at every layer (orchestrator runtime, worktree/branch, container/K8s, gateway, BRC/consensus, HITL/decision queue, cost/budget, overseer self-health, external state coupling, LLM substrate) produce Tier-1 checks
3. Removes dead code (`issue_filer.py` once test anchor is relocated), collapses fail-soft scaffolding, de-duplicates advisor-escalation plumbing, and decomposes `monitor.py`
4. Funnels cross-layer signals through the overseer as the single decision point
5. Keeps `overseer_auto_file_issues_mode=shadow` default until acceptance-rate telemetry validates the gate, then flips

## Current Behavior

**Overseer lifecycle (pipelines.py)**

The overseer is spawned via `spawn_overseer_container` (pipelines.py:21498) and torn down by `_teardown_phase_overseer` (pipelines.py:433–596), which is called from four separate sites (22229, 22552, 23062, and cleanup callback at 20505). The `phase_overseer_active` flag (pipelines.py:20463) gates teardown. The overseer has its own respawn loop via `_check_and_respawn_overseer` (pipelines.py:433–596) with `overseer_max_respawns=3`, which has no analog for phase agents — agents are restarted by the overseer itself via `_execute_restart_agent` (overseer/monitor.py:700–786). Spawn ordering: overseer is spawned *before* the phase-agent spawn loop in `_run_pipeline`; if agents fail to spawn, the overseer can be left running solo for the catch/teardown window.

**Overseer monitor (overseer/monitor.py, 2050 lines)**

`OverseerMonitor` class (lines 82–2024) implements a poll-classify-decide-act loop. The constructor instantiates 15+ state tracking variables including `_escalation_history` (per-agent deque keyed by agent_id, lines 108, 582–590), `_agents_restart_exhausted` (set, line 151), `_consecutive_orch_failures` (int, line 139), `_last_phase_name` (str|None, line 154), plus per-stall counters, incomplete-consensus absolute-start timestamps, infrastructure-error dedup windows, and post-consensus grace timers. `_poll_cycle()` (lines 330–509) runs 12 steps per cycle including consensus query, progress events, health alerts, pipeline status, orchestrator reachability, deterministic health checks, escalation messages, anomaly processing, terminal-state checks, cross-phase consistency, post-consensus stall, and incomplete-consensus stall. The entire cycle is wrapped in a single try/except that logs exceptions and allows the loop to continue (lines 356–505). Classifier and decision-maker invocations follow a dependency-injection wrapper pattern (lines 213–300) with `hasattr()` duck-typing for test doubles. The hallucination guard (lines 540–546) ensures the Sonnet decision tier only acts on data classified by the Haiku tier first. `OverseerSelfMonitor` is instantiated at line 105 but is sparsely integrated — it records poll-cycle duration, message counts, and a single placeholder LLM call with zero tokens/cost (line 1767, TODO at line 1764).

**Health checks (orchestrator/health_checks/)**

`HealthCheckRunner` (runner.py:41–256) manages a flat list of registered checks and dispatches them by trigger lifecycle event. Six Tier-1 checks are registered at `orchestrator/cli.py:283–301`: `container_liveness`, `startup_state`, `phase_output`, `state_consistency`, `consensus_stall`, `incomplete_consensus_stall`. Results flow outward through three channels: (A) EventBus emission (runner.py:181–238) — but no production handlers are currently subscribed to `HEALTH_CHECK_*` events; (B) direct return value to callers — the container monitor consumes `HealthResult` lists from `RUNTIME_TICK` triggers and inspects them via `worst_action()`; (C) the `/api/v1/pipelines/<pid>/health` endpoint (routes/health.py:196–288) which serializes on-demand results. The consensus-stall checks encode `recovery_action: drive_phase_transition` or `escalate_to_overseer` in their details dict, but that escalation is driven by the container monitor inspecting the returned list directly rather than by event-bus subscription. No Tier-2 directory or `agent_inspector` exists — the framework anticipates it (`HealthTier.AGENT`, `HealthTrigger.PHASE_COMPLETE`/`ON_DEMAND` escalation rules) but no concrete Tier-2 implementations have been registered.

**File-issue path**

`orchestrator/overseer/issue_filer.py` is dead code (header lines 3–16 state "DEAD CODE"; no production imports found — only references in comments, docs, and plan files). It retains `LEGACY_BODY_LITERAL` (lines 49–70) as a byte-for-byte test anchor for `test_overseer_issue_filer.py`. Production filing moved sandbox-side in #1962 via `egg-orch overseer file-issue`. Dedup is via `.egg-state/oversight/filed-issues.jsonl` local cache + `gh issue list --search "<8-char-sig>"` fallback (sandbox/egg_lib/overseer_issue_body.py:177–281). The two-tier dedup has known false-negative paths when the local cache is recreated.

**Advisor escalation**

`shared/egg_overseer/advisor.py` implements `consult_advisor()` (lines 201–406) as an Opus-tier async function using a two-call pattern (vendored SDK doesn't expose native advisor tool). Returns structured `AdvisorVerdict` (lines 77–107) with three decision branches: `watch`, `alert`, `file_issue`. The advisor is invoked via sandbox-side CLI `egg-orch overseer consult-advisor`, not from `monitor.py` — the word "advisor" does not appear anywhere in monitor.py. Defense-in-depth secret scrubbing: advisor scrubs before returning verdict, sandbox composer scrubs again, gateway scans and rejects.

**Signal-coverage gaps (observed symptoms)**

Concrete failure modes that never produce a Tier-1 alert:
- #2219: implement→PR auto-advance silently wedges. Surfaced only as `wedged_no_successor` on `get_status`. No health check consumes it.
- #2234: state_store `_ensure_worktree` ENOENT race kills `_run_pipeline` thread. Pipeline status stays `awaiting_human`, runner is dead, no alert fires.
- #2222: pipeline branch eats main commits + carries stale prior-run history. No signal until PR opens with `mergeStateStatus: DIRTY`.
- #2210: K8s monitor escalates clean BRC exits to pipeline `FAILED` without overseer triage (K8s tier bypasses overseer entirely).
- #2242: when overseer does fire, plan-phase heartbeat thresholds don't match long-completion tempo.

## Constraints

**Technical**

- `monitor.py` is scheduled for slice-7 of #2261 decomposition — this issue should not pre-empt that slice; cleanup landing here should not re-decompose monitor.py
- `#2272` is the parallel track on "how the watcher is shaped internally" (Sonnet executor + specialist subagents + native `advisor_20260301`); this issue is "what to watch + lifecycle + cleanup" inventory. The two can land in either order; specialist subagents from #2272 become natural homes for new tier-1 checks from this issue
- `#2111` (`overseer_owns_host_detection` default flip) and `#2261 slice-7` are out of scope and should not be pre-empted
- Health check framework already exposes the right primitives (`HealthCheck` protocol, `HealthResult`, `HealthAction`, EventBus) — new checks should conform to existing patterns, not invent parallel plumbing
- K8s pod transitions currently bypass overseer and go straight to pipeline `FAILED`; funneling them through overseer requires either intercepting at the Kubernetes monitor or subscribing to pod-state events
- Gateway error-rate tracking requires gateway to emit structured error-rate metrics or overseer to poll gateway status endpoint — neither primitive currently exists
- Branch-state sanity checks require git history inspection; must not block on large repos or unpushed commits
- Slice-DAG concurrency: in implement phase, N slices run concurrently but there is one phase-scoped overseer — alerts must carry `EGG_SLICE_ID` and per-slice dedup/escalation windows must not collide
- `OverseerSelfMonitor` exists with bounded deques (poll durations maxlen=100, LLM calls maxlen=500) and thresholds (`max_llm_cost_per_hour=$5.00`) but integration is sparse — wiring cost-tracking requires classifier to return usage metadata (currently returns `{classification, confidence, reasoning}` without token counts)
- Generation token for orchestrator-recycle detection: orchestrator must expose a stable token in health-status responses; overseer stores and compares on each poll. If orchestrator doesn't expose one, this issue must define and implement that primitive as a prerequisite

**Business**

- `overseer_auto_file_issues_mode=shadow` stays default until acceptance-rate telemetry validates the gate; this issue should track the calibration data needed for the flip
- The operator's stated priority is deletion-favoring cleanup — net-negative LOC is an explicit goal
- Out-of-scope issues (#2272, #2111, #2261 slice-7) should not be blocked or pre-empted by this work

**Dependencies**

- Gateway must emit structured error-rate metrics or expose a status endpoint before gateway-spike checks can be wired
- Orchestrator must expose a generation token (pod UID or boot timestamp) in health-status responses before recycle-detection can be wired
- If cluster-wide LLM substrate signaling is chosen, a shared cache or status endpoint must exist before the check can consult it
- K8s monitor must be modified to funnel pod transitions through overseer rather than escalating straight to `FAILED`; this may require a separate issue if K8s monitor lives outside overseer scope

## Options Considered

### Option A: Layered cleanup — delete, extract, wire, in that order

**Approach**: Phase the work into four stages: (1) delete dead code and collapsed fail-soft scaffolding; (2) extract reusable primitives (generation token, error-rate metrics, slice-affinity context) into shared modules; (3) wire new Tier-1 checks using existing `HealthCheck` protocol and EventBus subscription; (4) align lifecycle explicitly via a single spawn/teardown seam in pipelines.py.

**Pros**:
- Deletion-first approach gives immediate LOC reduction and test-surface shrinkage before adding complexity
- Extraction of primitives (generation token, error-rate metrics) into shared modules means new checks consume stable APIs, not internal orchestrator state
- Wiring Tier-1 checks after extraction means each check is a thin adapter over a stable primitive — easier to test and migrate
- Lifecycle seam extraction is the riskiest change (four teardown sites, spawn ordering) and lands last when all other surfaces are stable

**Cons**:
- Four stages may produce four PRs; operator may want fewer
- Extraction of primitives may reveal that the orchestrator doesn't have stable APIs for some signals (e.g., error-rate metrics); may require orchestrator changes that belong in a separate issue
- Lifecycle seam extraction may conflict with #2272 specialist-subagent architecture if landed first

### Option B: Signal-coverage-first — wire all new Tier-1 checks, then clean up

**Approach**: Start by wiring every new Tier-1 check the issue enumerates (orchestrator runtime, worktree/branch, container/K8s, gateway, BRC/consensus, HITL/decision queue, cost/budget, overseer self-health, external state coupling, LLM substrate), even if it means bolting on fail-soft scaffolding and duplicating primitives. Clean up the accumulated mess afterward.

**Pros**:
- Closes the gate-starvation symptom immediately — every failure mode has a Tier-1 check from day one
- Operator sees progress as each check lands and can validate acceptance criteria independently
- Cleanup phase can be aggressive on deletion because the new checks are already in place

**Cons**:
- Bolting on new checks before cleanup increases `monitor.py` line count further (already 2050 lines)
- Duplicated primitives and fail-soft scaffolding must be cleaned up later, risking regression
- May conflict with #2261 slice-7 decomposition of monitor.py — new checks added here would need to migrate into decomposed modules

### Option C: Lifecycle-first — align overseer as phase peer before touching signals or cleanup

**Approach**: Start by extracting a single spawn/teardown seam in pipelines.py, resolving the respawn-loop asymmetry, clearing per-agent tracking on `restart_agent`, and adding orchestrator-generation-token detection. Only after lifecycle is explicit and stable, wire new checks and clean up.

**Pros**:
- Lifecycle is the foundational architectural invariant — getting it right first means all downstream work lands on a stable base
- Respawn-loop asymmetry and restart-state alignment are bugs today, not just tech debt — fixing them early has immediate reliability benefit
- Generation-token detection is a prerequisite for recycle-detection checks; landing it first unblocks downstream work

**Cons**:
- Lifecycle changes are the riskiest (four teardown sites, spawn ordering, respawn semantics) and may introduce regressions that block the rest of the work
- Signal-coverage gaps remain open during lifecycle work, extending the window where failures go undetected
- May conflict with #2272 if specialist-subagent architecture changes spawn/teardown semantics

### Option D: Parallel-seam approach — decompose by independently-implementable subsystems

**Approach**: Identify independently-implementable subsystems (lifecycle, health-check wiring, signal-primitive extraction, dead-code deletion, advisor-escalation refactor) and allow the planner to slice them in whatever order minimizes merge conflicts with #2272, #2111, and #2261 slice-7. The refine phase names the seams but does not pre-commit to an ordering.

**Pros**:
- Maximum flexibility for the planner to sequence work around parallel issues
- Each seam is independently testable and landable
- Avoids pre-committing to a layering that may conflict with #2272 specialist-subagent architecture

**Cons**:
- Requires the planner to do more work to sequence the DAG
- May produce more PRs than the operator wants
- Risk of interleaving changes across seams producing subtle integration bugs

## Recommended Approach

**Option D with a bias toward Option A's layering within each seam.** The issue text already enumerates the seams (lifecycle, signal coverage, filing path, cleanup) and the operator's stated priority is deletion-favoring cleanup with net-negative LOC. The refine phase should name the seams and the primitives they require (generation token, error-rate metrics, slice-affinity context, per-stall absolute-start timestamp) with file:line evidence, but leave slice-DAG construction and PR packaging to the planner. The planner should bias toward: (1) delete dead code first (`issue_filer.py` after relocating test anchor, collapsed fail-soft blocks, duplicated advisor-escalation plumbing); (2) extract shared primitives (generation token in orchestrator health-status, error-rate metrics in gateway, slice-affinity context in consensus tracker); (3) wire new Tier-1 checks using existing `HealthCheck` protocol and EventBus subscription in `monitor.py`; (4) align lifecycle explicitly via a single spawn/teardown seam in pipelines.py with respawn-loop asymmetry resolved and restart-state alignment fixed.

The rationale for deletion-first within each seam is that it gives immediate LOC reduction and test-surface shrinkage before adding complexity, and it aligns with the operator's stated priority. The rationale for leaving ordering to the planner is that #2272 (specialist subagents), #2111 (host detection), and #2261 slice-7 (monitor.py decomposition) are all in flight and may create merge conflicts that the planner is better positioned to sequence around.

## Open Questions

### Resolved in Pre-Refine

None — the issue text does not include an `## Additional Context` section with pre-refine HITL answers.

### Registered HITL Decisions

<!-- egg-hitl-decision id=cq-1 -->

**Incomplete-consensus deferral is effectively unbounded because agents in tool-call loops emit activity without progress. Should we impose a hard absolute time cap on a single incomplete-consensus session, after which escalation is unconditional?**

- [ ] Hard cap (e.g. 2 hours) — unconditional escalation regardless of activity
- [ ] Soft cap with repetition detector — escalate if recent logs are >N% repetitive content
- [ ] No cap — keep activity-based deferral as-is
- [ ] Other (explain in reply)

**Rationale**: `_check_incomplete_consensus_stall` (monitor.py:1290–1457) defers escalation while blocking agents are active, tracked via `_incomplete_consensus_absolute_start` (line 123) and `max_deferral` — but the issue text states this is "effectively unbounded" and agents in repetitive tool-call loops emit activity without progress, causing pipelines to dangle for hours. The answer determines whether the planner implements a time-based cap, a content-based repetition detector, or leaves the current activity-based deferral as a known gap.

<!-- egg-hitl-decision id=cq-2 -->

**For LLM substrate health (sustained Anthropic 5xx / rate-limit across calls), should the overseer diagnose this per-pipeline from its own call-failure rate, or consult a cluster-wide signal to prevent N pipelines filing N duplicate issues?**

- [ ] Per-pipeline inference — aggregate this pipeline's failures over a sliding window
- [ ] Cluster-wide signal — poll a shared cache / status endpoint
- [ ] Both — escalate distinctly only when correlated with cluster-wide signal
- [ ] Other (explain in reply)

**Rationale**: The issue text identifies "Anthropic API health window — sustained 5xx / rate-limit across multiple calls should escalate distinctly from per-agent stall (it is a substrate problem, not an agent problem)." If the overseer diagnoses per-pipeline, N concurrent pipelines experiencing the same regional outage may each file an issue. If cluster-wide, a shared primitive must exist. The answer determines whether the planner builds per-pipeline inference, integrates a cluster-wide poll, or both.

### Registered Feedback Requests

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: Does the orchestrator currently expose a persistent generation token (e.g., pod UID or boot timestamp) in its health-status response, or must this issue define and implement that primitive as a prerequisite for the overseer's recycle-detection logic?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

*Authored-by: egg*

**Rationale**: The issue text identifies "Orchestrator pod recycle" as a gap: "If the orchestrator pod restarts mid-phase but the agent container (and its overseer) survives, the overseer's in-memory counters are aligned with the *old* orchestrator generation." The fix requires the orchestrator to expose a generation token and the overseer to store and compare it on each poll. If the orchestrator already exposes one, the planner only needs to wire the overseer side. If not, this issue must define and implement the primitive as a prerequisite, or report an impasse to the role that owns orchestrator health-status responses.

---

*Authored-by: egg*
