# Analysis: Cleanup: holistic overseer overhaul — phase-peer lifecycle, signal coverage, file_issue path

> Issue: #2270 | Phase: refine

## Problem Statement

The overseer subsystem (`orchestrator/overseer/`, `orchestrator/health_checks/`, `sandbox/overseer_monitor.py`, `shared/egg_overseer/`, plus lifecycle hooks in `orchestrator/routes/pipelines.py`) has accreted complexity across eight prior issues (#2096, #2272, #2284, #2290, #2296, #2300, #2442, #2797) and a long tail of point fixes. The current state is:

- `orchestrator/overseer/monitor.py` is 2024 lines (verified via `wc -l`) with 54 async/def methods (counted via grep excluding imports) and 33 `except` blocks (counted via grep) that catch and continue at debug-log level
- `orchestrator/overseer/issue_filer.py` is live-but-always-dead code: imported by `monitor.py:35`, re-exported at `overseer/__init__.py:27`, and called inside the `action == "issue"` branch of `_execute_action` at `monitor.py:669`. However, the orchestrator-side `file_diagnostic_issue` function is a dead path because the decision-maker vocabulary in `overseer/decision_maker.py` *does* include `"issue"` as a first-class action (lines 67, 127, 158), and the Sonnet tier can emit it — so the branch is reachable in principle. Production filing moved to sandbox-side `egg-orch overseer file-issue` in #1962; the orchestrator-side branch is either orphaned or a fallback whose reliability guarantee must be clarified
- Health check results flow through an EventBus (`orchestrator/events.py`) that no production handler subscribes to; the only production consumer is the container monitor inspecting returned `HealthResult` lists directly via `worst_action()`
- The auto-file-issue path has been silent in shadow mode since rollout not because pipelines are healthy, but because the conjunctive gate (Haiku ≥ 0.8 + Tier-1 alert + advisor `file_issue` + dedup miss) is starved upstream: many failure modes never produce a Tier-1 alert at all
- Overseer lifecycle is nominally phase-scoped but spawn ordering, teardown ordering, respawn semantics, and restart-state alignment are enforced by repetition across four teardown call sites rather than by a single architectural seam

The desired outcome is a cleaned-up overseer that:
1. Has explicit lifecycle alignment with phase agents — spawn with agents, tear down with agents, no overseer between phases, no state surviving phase boundaries
2. Closes signal-coverage gaps so failures at every layer (orchestrator runtime, worktree/branch, container/K8s, gateway, BRC/consensus, HITL/decision queue, cost/budget, overseer self-health, external state coupling, LLM substrate) produce Tier-1 checks
3. Removes dead or orphaned code paths, collapses fail-soft scaffolding, de-duplicates advisor-escalation plumbing, and decomposes `monitor.py` (a #2261-slice-7 goal that this issue must not pre-empt but should not fight against)
4. Funnels cross-layer signals through the overseer as the single decision point
5. Keeps `overseer_auto_file_issues_mode=shadow` default until acceptance-rate telemetry validates the gate, then flips

## Current Behavior

**Overseer lifecycle (pipelines.py)**

The overseer container is spawned through `spawner.spawn_overseer_container(...)` — called at `pipelines.py:21888` during the phase-agent spawn window and again at `pipelines.py:618` inside `_check_and_respawn_overseer` when the overseer is restarted. Teardown is driven by `_teardown_phase_overseer` (pipelines.py:792–821), which is called from four sites: lines 22229, 22552, 23062, and the cleanup-callback `_make_overseer_teardown_hook` at lines 20864–20900. The `phase_overseer_active` flag gates teardown. The overseer has its own respawn loop via `_check_and_respawn_overseer` (pipelines.py:520–596, with the spawner re-invocation at 618) using `overseer_max_respawns=3` — an asymmetry versus phase agents, which the overseer itself restarts via `_execute_restart_agent` (overseer/monitor.py:700–786). Spawn ordering: the overseer is spawned *before* the phase-agent spawn loop in `_run_pipeline`; if the agents fail to spawn, the overseer can be left running solo for the catch/teardown window.

**Two-monitor architecture (orchestrator-side + sandbox-side)**

There are **two monitors**, not one, and they have distinct responsibilities:

1. **Orchestrator-side monitor** (`orchestrator/overseer/monitor.py`, 2024 lines). This is the in-process Python monitor that runs inside the orchestrator pod alongside the pipeline. It polls the orchestrator's REST API via `egg-orch` CLI wrappers, classifies anomalies through the Haiku-tier classifier (`overseer/classifier.py`), and decides corrective actions through the Sonnet-tier decision maker (`overseer/decision_maker.py`). It **does not** invoke the advisor — the word "advisor" does not appear anywhere in `monitor.py`.

2. **Sandbox-side monitor** (`sandbox/overseer_monitor.py`, 803 lines). This is the script that runs inside the agent sandbox and is responsible for the **file-issue path**. It implements the tier-1 intersection gate (`run_once` at lines 527–642, advisor gate at lines 609–630), decides whether to invoke `egg-orch overseer consult-advisor` (`should_consult_advisor` at lines 644–672), and runs the migrated detectors (`run_migrated_detectors` at lines 186–525). It's here that the Opus-tier advisor (`shared/egg_overseer/advisor.py`) is invoked as the second call in a two-call pattern (vendored SDK does not expose the native advisor tool directly).

This split is architecturally load-bearing: new signal-routing that reaches the file-issue path must be plumbed through the sandbox-side monitor's tier-1 gate; new corrective-action logic (restart, redirect, nudge) belongs in the orchestrator-side monitor. The draft analysis in the issue body occasionally conflates the two; the plan phase must keep them distinct.

**Overseer monitor state (overseer/monitor.py classes)**

`OverseerMonitor` (lines 82–2024) implements a poll-classify-decide-act loop. The constructor (lines 96–167) instantiates 15+ state-tracking attributes: `_escalation_history` (per-agent deque keyed by agent_id, lines 108, 582–590), `_agents_restart_exhausted` (set, line 151), `_consecutive_orch_failures` (int, line 139), `_last_phase_name` (str|None, line 154), plus per-stall counters, incomplete-consensus absolute-start timestamps (`_incomplete_consensus_absolute_start` at line 123, with activity-deferral cap from #1609), infrastructure-error dedup windows, and post-consensus grace timers. `_poll_cycle()` (lines 330–509) runs a 12-step cycle including consensus query, progress events, health alerts, pipeline status, orchestrator reachability, deterministic health checks, escalation messages, anomaly processing, terminal-state checks, cross-phase consistency, post-consensus stall, and incomplete-consensus stall. The entire cycle is wrapped in a single try/except that logs exceptions and lets the loop continue (lines 356–505). Classifier and decision-maker invocations follow a dependency-injection wrapper pattern (lines 213–300) with `hasattr()` duck-typing for test doubles. The hallucination guard (lines 540–546 inside `handle_escalation`) ensures the Sonnet decision tier only acts on data classified by the Haiku tier first. `OverseerSelfMonitor` (instantiated at line 105, defined in `overseer/self_monitor.py`) is sparsely integrated — it records poll-cycle duration, message counts, and a single placeholder LLM call with zero tokens/cost (line 1767, TODO at line 1764). Cost-tracking wiring is blocked on the classifier returning usage metadata; the classifier return schema is currently `{classification, confidence, reasoning}` without token counts.

**Health checks (orchestrator/health_checks/)**

`HealthCheckRunner` (runner.py:41–256) manages a flat list of registered checks and dispatches them by trigger lifecycle event. Six Tier-1 checks are registered at `orchestrator/cli.py:283–301`: `container_liveness`, `startup_state`, `phase_output`, `state_consistency`, `consensus_stall`, `incomplete_consensus_stall`. Results flow outward through three channels: (A) EventBus emission (runner.py:181–238) — but no production handlers are currently subscribed to `HEALTH_CHECK_*` events; (B) direct return value to callers — the container monitor consumes `HealthResult` lists from `RUNTIME_TICK` triggers and inspects them via `worst_action()`; (C) the `/api/v1/pipelines/<pid>/health` endpoint (routes/health.py:196–288) which serializes on-demand results. The consensus-stall checks encode `recovery_action: drive_phase_transition` or `escalate_to_overseer` in their details dict, but that escalation is driven by the container monitor inspecting the returned list directly rather than by event-bus subscription.

**Note on the issue text's stale Tier-2 claim.** The issue text lists "Tier-2: `agent_inspector`" as existing. This is **not accurate for the current source tree**: no `orchestrator/health_checks/tier2/` directory exists and no `agent_inspector` identifier appears anywhere in the `health_checks/` subtree. The `HealthTier.AGENT` enum and the `PHASE_COMPLETE`/`ON_DEMAND` escalation rules in `runner.py` *anticipate* a Tier 2, but no concrete implementation has been registered. The planner should treat Tier-2 wiring as greenfield, not as migration of an existing check.

**File-issue path (live-but-always-dead)**

`orchestrator/overseer/issue_filer.py` is imported by `monitor.py:35` (`from overseer.issue_filer import file_diagnostic_issue`), re-exported through `overseer/__init__.py:11,27`, and called inside the `elif action == "issue":` branch of `_execute_action` at `monitor.py:665–674`. The decision-maker vocabulary in `overseer/decision_maker.py:67` and the prompt at line 158 both list `"issue"` as a first-class action the Sonnet tier may return. The branch is therefore **reachable in principle** — the Sonnet decision maker is prompted to emit it and the orchestrator-side `file_diagnostic_issue` would execute if it did. In practice, the issue-text's framing as "dead code preserved as a test anchor" reflects that production filing moved to the sandbox-side `egg-orch overseer file-issue` CLI in #1962, so the orchestrator-side branch is either an orphaned fallback or an implicit safety net whose reliability guarantee needs to be decided (see Registered HITL Decisions below).

The sandbox-side filing path dedups via `.egg-state/oversight/filed-issues.jsonl` local cache + `gh issue list --search "<8-char-sig>"` fallback (`sandbox/egg_lib/overseer_issue_body.py:177–281`). The two-tier dedup has known false-negative paths when the local cache is recreated across phase boundaries.

**Advisor escalation (sandbox-side, not orchestrator-side)**

`shared/egg_overseer/advisor.py` implements `consult_advisor()` (lines 201–406) as an Opus-tier async function using a two-call pattern (vendored SDK doesn't expose the native advisor tool directly). It returns a structured `AdvisorVerdict` (lines 77–107) with three decision branches: `watch`, `alert`, `file_issue`. The advisor is invoked from the **sandbox-side monitor** (`sandbox/overseer_monitor.py`) via the `egg-orch overseer consult-advisor` CLI, gated by `should_consult_advisor` (lines 644–672) which requires a Tier-1 alert to be present in the cycle report (decision-18 in the sandbox source). Defense-in-depth secret scrubbing: advisor scrubs before returning verdict (line 387 of `advisor.py`), sandbox composer (`overseer_issue_body.py:174`) scrubs again, gateway scans and rejects.

**Signal-coverage gaps (observed symptoms)**

Concrete failure modes that never produce a Tier-1 alert (from the issue text, verified against current source):
- #2219: implement→PR auto-advance silently wedges. Surfaced only as `wedged_no_successor` on `get_status` (in `orchestrator/mcp_tools.py`). No health check consumes it.
- #2234: state_store `_ensure_worktree` ENOENT race kills `_run_pipeline` thread. Pipeline status stays `awaiting_human`, runner is dead, no alert fires.
- #2222: pipeline branch eats main commits + carries stale prior-run history. No signal until PR opens with `mergeStateStatus: DIRTY`.
- #2210: K8s monitor escalates clean BRC exits to pipeline `FAILED` without overseer triage (K8s tier bypasses overseer entirely).
- #2242: when overseer does fire, plan-phase heartbeat thresholds don't match long-completion tempo.

## Orchestrator-generation-token primitive (research finding)

The issue text identifies "Orchestrator pod recycle" as a gap: "If the orchestrator pod restarts mid-phase but the agent container (and its overseer) survives, the overseer's in-memory counters are aligned with the *old* orchestrator generation." To close this gap, the overseer needs a stable token the orchestrator exposes on each health-status response, which the overseer stores and compares on each poll — mismatch triggers state reset.

**Finding**: The orchestrator currently exposes **no** generation token. Grep for `generation`, `boot_time`, `boot_timestamp`, `pod_uid`, and `generation_token` across `orchestrator/` (excluding test fixtures and unrelated clients) returned zero matches. No route in `orchestrator/routes/` includes boot metadata in its response body. This issue must therefore **define and implement the primitive as a prerequisite** (or hand off to the role that owns orchestrator health-status responses if that's outside the implementer's scope for this issue). Candidate primitives: orchestrator pod UID (if running on K8s), monotonic boot timestamp, or a UUID minted at orchestrator startup.

## Constraints

**Technical**

- `monitor.py` is scheduled for #2261 slice-7 decomposition — this issue must not pre-empt that slice. Cleanup landed here should not re-decompose `monitor.py`; submodule-level extraction candidates (classifier, decision maker, restart/file-issue executors, advisor invocation) already exist as separate modules within `orchestrator/overseer/`
- `#2272` is the parallel track on "how the watcher is shaped internally" (Sonnet executor + specialist subagents + native `advisor_20260301`); this issue is "what to watch + lifecycle + cleanup" inventory. The two can land in either order; specialist subagents from #2272 become natural homes for new tier-1 checks from this issue
- `#2111` (`overseer_owns_host_detection` default flip) and `#2261 slice-7` are out of scope and must not be pre-empted
- Health-check framework already exposes the right primitives (`HealthCheck` protocol, `HealthResult`, `HealthAction`, EventBus) — new checks should conform to existing patterns in `orchestrator/health_checks/tier1/` rather than invent parallel plumbing
- K8s pod transitions currently bypass the overseer and go straight to pipeline `FAILED`; funneling them through the overseer requires either intercepting at the Kubernetes monitor or subscribing to pod-state events in the orchestrator
- Gateway error-rate tracking requires the gateway to emit structured error-rate metrics or the overseer to poll a gateway status endpoint — neither primitive currently exists
- Branch-state sanity checks require git history inspection; must not block on large repos or unpushed commits
- Slice-DAG concurrency: in implement phase, N slices run concurrently but there is one phase-scoped overseer — alerts must carry `EGG_SLICE_ID` (`shared/egg_overseer/state.py` already supports this context), and per-slice dedup/escalation windows must not collide. The planner must thread slice-affinity context into new tier-1 checks
- `OverseerSelfMonitor` exists with bounded deques (poll durations `maxlen=100`, LLM calls `maxlen=500`) and thresholds (`max_llm_cost_per_hour=$5.00`) but integration is sparse — wiring cost-tracking requires the classifier to return usage metadata (currently it returns `{classification, confidence, reasoning}` without token counts). This is a cross-cutting concern that spans multiple seams
- Generation token primitive: orchestrator must expose a stable token in health-status responses before recycle-detection can be wired — this issue must ship the primitive (see research finding above)

**Business**

- `overseer_auto_file_issues_mode=shadow` stays default until acceptance-rate telemetry validates the gate; this issue should track the calibration data needed for the flip
- The operator's stated priority is deletion-favoring cleanup — net-negative LOC is an explicit goal
- Out-of-scope issues (#2272, #2111, #2261 slice-7) must not be blocked or pre-empted by this work

**Dependencies**

- Gateway must emit structured error-rate metrics or expose a status endpoint before gateway-spike checks can be wired
- Orchestrator must ship a generation-token primitive before recycle-detection can be wired
- If cluster-wide LLM substrate signaling is chosen (see HITL decision cq-2 below), a shared cache or status endpoint must exist before the check can consult it
- K8s monitor must be modified to funnel pod transitions through overseer rather than escalating straight to `FAILED`; if the K8s monitor lives outside the implementer's scope for this issue, the planner may need to declare an impasse or split the work

## Options Considered

### Option A: Layered cleanup — delete, extract, wire, in that order

**Approach**: Phase the work into four stages: (1) delete dead and orphaned code and collapsed fail-soft scaffolding; (2) extract reusable primitives (generation token, error-rate metrics, slice-affinity context) into shared modules; (3) wire new Tier-1 checks using existing `HealthCheck` protocol and EventBus subscription; (4) align lifecycle explicitly via a single spawn/teardown seam in pipelines.py.

**Pros**:
- Deletion-first gives immediate LOC reduction and test-surface shrinkage before adding complexity
- Extraction of primitives into shared modules means new checks consume stable APIs, not internal orchestrator state
- Wiring Tier-1 checks after extraction means each check is a thin adapter over a stable primitive — easier to test and migrate
- Lifecycle seam extraction is the riskiest change (four teardown sites, spawn ordering) and lands last when all other surfaces are stable

**Cons**:
- Four stages may produce four PRs; operator may prefer fewer
- Extraction of primitives may reveal that the orchestrator doesn't have stable APIs for some signals (e.g., error-rate metrics); may require orchestrator changes that belong in a separate issue
- Lifecycle seam extraction may conflict with #2272 specialist-subagent architecture if landed first; Option C is more exposed to this than Option A

### Option B: Signal-coverage-first — wire all new Tier-1 checks, then clean up

**Approach**: Start by wiring every new Tier-1 check the issue enumerates (orchestrator runtime, worktree/branch, container/K8s, gateway, BRC/consensus, HITL/decision queue, cost/budget, overseer self-health, external state coupling, LLM substrate), even if it means bolting on fail-soft scaffolding and duplicating primitives. Clean up the accumulated mess afterward.

**Pros**:
- Closes the gate-starvation symptom immediately — every failure mode has a Tier-1 check from day one
- Operator sees progress as each check lands and can validate acceptance criteria independently
- Cleanup phase can be aggressive on deletion because the new checks are already in place

**Cons**:
- Bolting on new checks before cleanup increases `monitor.py` line count further (already 2024 lines, over the 1500-line cap from `scripts/file-size-allowlist.yaml`)
- Duplicated primitives and fail-soft scaffolding must be cleaned up later, risking regression
- Directly conflicts with #2261 slice-7 decomposition — new checks added here would need to migrate into the decomposed submodules, doubling the move work

### Option C: Lifecycle-first — align overseer as phase peer before touching signals or cleanup

**Approach**: Start by extracting a single spawn/teardown seam in pipelines.py, resolving the respawn-loop asymmetry, clearing per-agent tracking on `restart_agent`, and adding orchestrator-generation-token detection. Only after lifecycle is explicit and stable, wire new checks and clean up.

**Pros**:
- Lifecycle is the foundational architectural invariant — getting it right first means all downstream work lands on a stable base
- Respawn-loop asymmetry and restart-state alignment are bugs today, not just tech debt — fixing them early has immediate reliability benefit
- Generation-token detection is a prerequisite for recycle-detection checks; landing it first unblocks downstream work

**Cons**:
- Lifecycle changes are the riskiest (four teardown sites, spawn ordering, respawn semantics) and may introduce regressions that block the rest of the work
- Signal-coverage gaps remain open during lifecycle work, extending the window where failures go undetected
- Most likely of the four options to conflict with #2272 specialist-subagent architecture, since specialist subagents may redefine what "spawn a phase agent" means

### Option D: Parallel-seam approach — decompose by independently-implementable subsystems

**Approach**: Identify independently-implementable subsystems (lifecycle, health-check wiring, signal-primitive extraction, dead-code deletion, advisor-escalation refactor, two-monitor boundary clean-up) and allow the planner to slice them in whatever order minimizes merge conflicts with #2272, #2111, and #2261 slice-7. The refine phase names the seams but does not pre-commit to an ordering or DAG shape.

**Pros**:
- Maximum flexibility for the planner to sequence work around parallel issues
- Each seam is independently testable and landable
- Avoids pre-committing to a layering that may conflict with #2272 specialist-subagent architecture
- Respects the planner's ownership of slice-DAG construction per `docs/architecture/slice-dag.md`

**Cons**:
- Requires the planner to do more work to sequence the DAG
- May produce more PRs than the operator wants
- Risk of interleaving changes across seams producing subtle integration bugs

## Recommended Approach

**Option D with a bias toward Option A's layering within each seam.** The issue text already enumerates the seams (lifecycle, signal coverage, filing path, cleanup) and the operator's stated priority is deletion-favoring cleanup with net-negative LOC. The refine phase names the seams and the primitives they require (generation token, error-rate metrics, slice-affinity context, per-stall absolute-start timestamp) with file:line evidence, and leaves slice-DAG construction and PR packaging to the planner.

The planner should bias toward:
1. **Resolve the orphaned `action == "issue"` branch** in `monitor.py:665–674` — either delete it after confirming the sandbox-side filing is the authoritative path, or re-route it through the sandbox-side `egg-orch overseer file-issue` so that dedup and templating stay canonical. Relocate the test anchor in `issue_filer.py` that keeps `LEGACY_BODY_LITERAL` as a byte-for-byte literal comparison, then delete the module. This requires operator input — see Registered HITL Decisions below.
2. **Extract shared primitives** — generation-token primitive in orchestrator health-status (must be shipped as part of this issue per the research finding above), error-rate metrics in gateway, slice-affinity context in consensus tracker (with `EGG_SLICE_ID` propagation to the health-check seam).
3. **Wire new Tier-1 checks** using the existing `HealthCheck` protocol and EventBus subscription in `monitor.py` (and in the sandbox-side monitor where appropriate, respecting the two-monitor boundary).
4. **Align lifecycle explicitly** via a single spawn/teardown seam in pipelines.py with respawn-loop asymmetry resolved, restart-state alignment fixed (per-agent `_escalation_history` clear on `restart_agent`), orchestrator-generation-token comparison on each poll.

The rationale for deletion-first within each seam is immediate LOC reduction and test-surface shrinkage. The rationale for leaving ordering to the planner is that #2272 (specialist subagents), #2111 (host detection), and #2261 slice-7 (monitor.py decomposition) are all in flight and may create merge conflicts that the planner is better positioned to sequence around.

## Open Questions

### Resolved in Pre-Refine

None — the issue text does not include an `## Additional Context` section with pre-refine HITL answers.

### Research Findings (answered during refine)

- **Orchestrator generation token**: does **not** currently exist (see research finding above). Plan for this issue to ship it.
- **Issue text's `agent_inspector` Tier-2 claim**: **false** for the current source tree (see note under Health checks above).
- **`issue_filer.py` dead-code classification**: **live-but-always-dead** — imported, called, in the decision-maker vocabulary, but production filing moved to sandbox-side path. See HITL decision cq-3 below for the scope question.

### Registered HITL Decisions

<!-- egg-hitl-decision id=cq-2 -->

**For LLM substrate health (sustained Anthropic 5xx / rate-limit across calls, which the issue text lists as a gap), should the overseer diagnose this per-pipeline from its own call-failure rate, or should the operator commit to a shared cluster-wide signal so N concurrent pipelines don't file N duplicate issues for substrate problems?**

- [ ] Per-pipeline inference — aggregate this pipeline's failures over a sliding window
- [ ] Cluster-wide signal — poll a shared cache / status endpoint that another agent or system maintains
- [ ] Both — escalate distinctly only when per-pipeline failure rate and cluster-wide signal correlate
- [ ] Other (explain in reply)

**Rationale**: This is a **scope** question, not a detector-design question: the difference between per-pipeline inference and cluster-wide signaling has a direct product-level impact on duplicate-filing behavior across pipelines during a substrate outage (Anthropic regional 5xx, LiteLLM-proxy-wide outage, K8s node OOM affecting many pods). The issue text calls this out explicitly ("Sentry / aggregated-error consultation — overseer is purely log-tail today. Cluster-level events are diagnosed N times in parallel by N independent overseers."). The planner can choose detector thresholds, window sizes, and correlation heuristics without operator input, but *whether* the overseer consults a cluster signal is a commitment the operator must make because it constrains the architecture of the cluster-side primitive (who produces it, how it's stored, who pays the latency).

<!-- egg-hitl-decision id=cq-3 -->

**The orchestrator-side `action == "issue"` branch (`monitor.py:665–674`) and the `issue_filer.py` module it calls are reachable in principle (the Sonnet decision maker can emit the `"issue"` action and the orchestrator-side code executes it), but production filing has lived sandbox-side since #1962. What should the planner do with this orphaned/fallback path?**

- [ ] Delete the branch entirely — the sandbox-side `egg-orch overseer file-issue` path is authoritative; the orchestrator-side branch is an orphan
- [ ] Re-route the branch through the sandbox-side CLI — keep orchestrator-side visibility but ensure dedup and templating are canonical
- [ ] Preserve as an explicit fallback — document the fallback and keep both paths under active test coverage
- [ ] Other (explain in reply)

**Rationale**: This is a scope-and-reliability-guarantee question only the operator can answer. The sandbox-side path is the one that goes through the defense-in-depth secret scrubbing chain and the two-tier dedup (local JSONL + GitHub search); the orchestrator-side path bypasses that. Whether the orchestrator-side branch is a safety net that must be preserved, or just dead weight masking duplicate filing, is a product-reliability judgment the planner should not guess at. The answer also determines whether `issue_filer.py` can be deleted (with its test anchor relocated) or must be kept alive under test.

### Dropped from Prior Draft

The prior draft included a question on incomplete-consensus absolute time caps versus repetition detector. That question has been removed: it is a detector-design question about how to parameterize `_check_incomplete_consensus_stall`, not a scope or product-commitment question, and the planner is better positioned than the operator to evaluate which approach fits the existing `monitor.py:1290–1457` architecture. The issue text's concern about unbounded deferral should be reflected in the planner's acceptance criteria for that check, not as a HITL gate.

---

*Authored-by: egg*
