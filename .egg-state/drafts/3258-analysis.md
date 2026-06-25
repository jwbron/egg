# Refine Analysis — issue #3258

**Complete #3200 slice-10: emit-only BRC context-discipline measurement surfaces (AC-4 + AC-5 of #3200).**

Pipeline: `issue-3258` · base branch: `main` · phase: refine · author: refiner

---

## 1. Problem statement

#3200 ("BRC context discipline") was sliced 1–10. Slices 1–9 build the *mechanism*
(token-occupancy capture, real-window/threshold, #3189 anchors, protected root, queryable
environment, #3186 resume substrate, mid-phase persistence, threshold reseed, all-roles
rollout behind a flag). **Slice-10 — the emit-only measurement surfaces — was never built:**
its producers died on quota exhaustion and the restart-bootstrap false-completed the empty
slice (#3253). This pipeline carries slice-10 to completion as its own standalone SDLC run,
which also serves as a live proof-of-fix for #3253 / #3245 / restart-resume reconciliation.

slice-10 must **emit** the per-event measurement signals a later measurement pass (#3249)
will consume — routed through the **existing** progress / heartbeat / metrics surfaces.
It must NOT run a measurement, compare, aggregate-to-verdict, or gate any control flow on the
emitted values (emit-only — AC-5 of #3200).

## 2. Grounded codebase reality (read at HEAD `2aafbe273`, base = `origin/main`)

| Element | Expected (per issue) | Actual on `main` | Evidence |
|---|---|---|---|
| slice-1 `AgentResult` window-occupancy field | merged | **ABSENT** — `AgentResult` has only `cost_usd`, `num_turns`, `duration_ms`, `session_id`, `metadata` | `shared/egg_agent/result.py:7-34` |
| slice-8 reseed decisions/signals | merged | **ABSENT** — `grep -rniE 'reseed\|occupanc'` over repo = 0 hits | repo-wide grep |
| Token extraction that *could* feed occupancy | — | exists downstream only, in the litellm cost callback (`cache_read_input_tokens`, `cache_creation_input_tokens`, `prompt_tokens`) | `config/litellm/cost_callback.py:173-206` |
| Progress emit surface | exists | `progress_emit` → `POST /api/v1/pipelines/<pid>/progress`; fields `agent_role/step/state/detail/blocker`; orchestrator emits `EventType.PROGRESS_EMITTED` | `sandbox/egg_agent_tools/handlers/progress.py:35-84`; `orchestrator/routes/progress.py:56-131` |
| Heartbeat surface | exists | `progress_heartbeat` → signal endpoint; orchestrator `HeartbeatCoordinator` rate-limits 20/min per `(pipeline,slice,role)`, dedupes on `(state,waiting_on)` | `sandbox/egg_agent_tools/handlers/progress.py:114-132`; `orchestrator/heartbeat.py:45-168` |
| Metrics surface | exists | `get_metrics_registry()` (counters/gauges/histograms); exposed at `GET /api/v1/metrics` + `/prometheus`. **Aggregated/queried post-hoc — no per-event emit today** | `orchestrator/routes/metrics.py:1-81` |
| Per-event seam (event pump) | — | orchestrator-owned one-shot loop spawns agent per event; AgentResult is **not** read back / token usage **not** reconstructed by orchestrator post-event | `orchestrator/event_loop.py`; `orchestrator/consensus_wrapper.py:93-100,~429`; `orchestrator/kubernetes_spawner.py` |

### CRITICAL STRUCTURAL FINDING — substrate is unmerged (RESOLVED → Option D)

The issue and the operator `task_description` both state slice-10 "builds on the
**already-merged** slice-1 `AgentResult` window-occupancy capture and slice-8 reseed
decisions (both merged)". **This premise is false for this pipeline's base branch.**

- slice-1 PR **#3236** ("Token-occupancy capture") is **OPEN**, base = `egg/issue-3200/work` (not main).
- slice-8 PR **#3251** ("Threshold reseed") is **OPEN**, base = `egg/issue-3200/slice-7`, `mergeStateStatus=UNSTABLE`.
- `origin/main` contains **no** occupancy field and **no** reseed code.
- All of #3200 slices 1–9 are open PRs (#3234, #3236–#3240, #3243, #3248, #3251, #3252); the
  "issue-3200 (complete)" git entries are pipeline-state-branch commits, not slice merges.

**Operator resolution (iteration 0, HITL gate):** OQ-1 is resolved → **Option D — emit
against existing `AgentResult` fields plus a single adapter/seam that degrades gracefully
(null/zero) until the real fields land.** The operator's binding rationale:

- #3258 is a **standalone pipeline rooted on `main`** and must stay **self-contained**.
- Confirmed UNMERGED on main: slice-1 occupancy (PR #3236) and slice-8 reseed (PR #3251).
  Therefore: **do NOT** take a hard dependency on them; **do NOT** stack on the
  `egg/issue-3200/*` branches; **do NOT** vendor a duplicate substrate.
- Define **one** adapter/seam where occupancy and reseed signals are read. Bind it to the real
  fields **when present**; degrade to **null/zero when absent** (the case on `main` today).
  When PRs #3236/#3251 later merge, the real values flow through this seam automatically — **no
  rework**.

**Consequence:** slice-10 is deliverable on `main` now. The emit surfaces read all input
signals exclusively through the adapter seam; on `main` the seam returns null/zero, and the
six metrics are still emitted (with null/zero payloads) so the surface wiring, emit-only
invariant, and tests are fully exercised today. Options A (stack), B (vendor substrate), and C
(block until merge) are **rejected** by the operator resolution above.

## 3. Metrics to emit (each read through the single adapter seam)

**The adapter seam (Option D).** Per the operator resolution, all input signals — window
occupancy and reseed — are read through **one** adapter/seam, never directly off `AgentResult`
or reseed internals scattered across the emit code. The seam exposes a small, typed read API
(e.g. `occupancy_for(result) -> int | None`, `reseed_signals_for(event) -> ReseedInfo | None`).
On `main` today the seam returns `None`/`0` (substrate absent); once slice-1 (#3236) /
slice-8 (#3251) merge, the same seam binds to the real fields and live values flow through
with no change to the emit code. Plan phase fixes the seam's exact signature and the
field-to-surface binding; refine asserts the seam is the **sole** read point and that every
metric has a real source field behind it and a real existing surface in front of it.

Per event, routed through the existing surfaces (§2), each value sourced via the seam:

1. **Window occupancy** = `cache_read + cache_creation + input` tokens (from slice-1 field).
2. **Peak context utilization under resume** — max occupancy observed across a resumed run.
3. **Single-event working set vs real backend window** — the recursion-escalation signal
   (one event's working set measured against the true backend window size).
4. **Reseed frequency per phase** — count of slice-8 reseed decisions, bucketed by phase.
5. **Root-cache hit rate** — fraction of events hitting the protected-root cache.
6. **Tokens / event** — total tokens attributed to the single event.

Each maps to a defined source field (slice-1 occupancy / slice-8 reseed signal) and a defined
existing surface (progress event payload, heartbeat body/fields, or metrics
counter/gauge/histogram). The plan phase will fix the exact field-to-surface binding; refine's
job is to assert each metric HAS a real source and a real surface, and that none is invented.

## 4. Emit-only constraint (AC-2 / AC-5 of #3200)

- No measurement run, no A/B, no status-quo comparison, no aggregation into a verdict.
- **Nothing gated:** no `if`/branch/early-return/threshold/flag anywhere may read an emitted
  metric value to choose behavior. The metrics are write-only sinks.
- Testable invariant: a test asserts no control-flow path consumes the emitted metric values
  for a decision (e.g. the producing functions return `None`/void and their values are never
  bound into a conditional; verified by structural assertion over the emit module).

## 5. Acceptance criteria (refined, inherited from the issue)

- **AC-1** — each of the six metrics (§3) is emitted **per event**, sourced **through the
  single adapter seam** (§3) from the slice-1 occupancy field + slice-8 reseed signals, through
  the existing progress/heartbeat/metrics surfaces. No new external surface invented. On `main`
  the seam returns null/zero, and the metrics are still emitted with those payloads.
- **AC-2** — emit-only / nothing gated: no code path consumes the metrics for a decision; a
  test **structurally asserts no decision branches** read any emitted metric value (proves
  AC-4 + AC-5 of #3200). The emit functions are write-only sinks; no `if`/branch/early-return/
  threshold/flag anywhere binds an emitted value into a conditional.
- **AC-3** — tests: surfaces emit correct values for a **synthetic event sequence including ≥1
  reseed, fed through the adapter seam** (so the emit logic is fully validated even though
  production values are null on `main` until the substrate lands); the no-decision-branch
  assertion passes; `make test` green.

## 6. Substrate dependency — RESOLVED (Option D)

**OQ-1 (was BLOCKING) — substrate dependency.** slice-1 occupancy + slice-8 reseed are
unmerged on this pipeline's base (`main`); see §2. **Operator resolution (iteration 0 HITL
gate): Option D.** Build emit-only against existing `AgentResult` fields plus a single adapter
seam that degrades gracefully (null/zero) until the real fields land. Options A (stack on
`egg/issue-3200/*`), B (vendor a duplicate substrate), and C (block until merge) are
**rejected** — they would make #3258 non-self-contained or non-deliverable on `main`.

**Direction for plan + implement (binding):**
1. **Single seam.** One adapter is the sole read point for occupancy and reseed signals. Bind
   to the real fields when present; return null/zero when absent. Real values flow through
   automatically once #3236/#3251 merge — no rework, no second integration point.
2. **Six metrics, existing surfaces only.** Window occupancy (`cache_read + cache_creation +
   input`), peak utilization under resume, single-event working set vs real backend window,
   reseed frequency per phase, root-cache hit rate, tokens/event — each routed ONLY through the
   existing progress / heartbeat / metrics surfaces. Invent no new external surface.
3. **EMIT-ONLY is hard scope.** No measurement run, no A/B, no comparison, no
   aggregation-to-verdict, and NOTHING gated — no control-flow path may read an emitted metric
   to choose behavior. Ship the AC-2 structural test that asserts this.
4. **AC-3 synthetic sequence.** Tests drive a synthetic event sequence (≥1 reseed) through the
   seam so emit logic is fully validated on `main` despite null production values. `make test`
   green.

## 7. Out of scope (owned by #3249, unchanged from #3200)

The measurement pass itself, go/no-go decision, generalization beyond slices 1–9, sub-agent
recursion, and the preserved fallback.


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

approve
