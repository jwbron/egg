# Overseer Architecture

> **Status:** This document describes the **delivered shape** of the overseer
> subsystem after the overseer overhaul ([#2270](https://github.com/jwbron/egg/issues/2270)).
> It supersedes the older "phase-scoped, auto-respawning overseer pod"
> description still referenced in
> [orchestrator.md](orchestrator.md) (the "Pipeline health monitoring" section)
> and the [Orchestrator README](../../orchestrator/README.md#overseer-agent); those
> sections predate the overhaul and are being retired. When the two disagree,
> this document is authoritative for the detection plane, the on-demand
> adjudicator, the bounded corrective vocabulary, model tiering, and the
> authority path.

## What the overseer is for

The overseer is the part of the pipeline that watches for trouble the
deterministic state machine can't name on its own — a phase that is RUNNING but
making no progress, a reviewer/producer thrash that never converges, a gateway
that has started denying every push, an LLM substrate that has gone dark. Its
job is to **notice**, **judge whether the concern is real**, and **act within a
bounded vocabulary** (or hand a real decision to a human).

The overhaul ([#2270](https://github.com/jwbron/egg/issues/2270)) was driven by
two failure modes that coexisted:

- **Too loud.** A respawning watcher pod ran an LLM over every observation and
  broadcast a steady stream of false `[high]` alerts — including alerts that
  were the overseer mis-classifying its own bootstrap as a prompt-injection
  attack, refusing, exiting, and getting respawned. Operators learned to ignore
  the channel.
- **Useless at the moment of need.** Genuine deadlocks surfaced as a generic
  `stuck-phase-transition`, or as nothing at all, and the overseer had no
  structural authority to open an operator decision even when it was sure.

The delivered design replaces the standing watcher pod with an
**orchestrator-side overseership model**: cheap deterministic evaluation over the
event stream, with an LLM spawned *only* for the genuinely ambiguous minority.

## The model: detection plane → adjudicator → authority plane

```
                       orchestrator event loop (in-process, no LLM)
                                     │
                   EventStreamSnapshot (point-in-time pipeline state)
                                     │
                          ┌──────────▼───────────┐
                          │    Detection plane    │  health_checks/detection_plane.py
                          │  N deterministic       │
                          │  detectors: pure        │
                          │  snapshot -> Finding|None│
                          └──────────┬───────────┘
                                     │ findings
                     ┌───────────────┴────────────────┐
                     │                                 │
        requires_adjudication=False        requires_adjudication=True
                     │                                 │
          ┌──────────▼───────────┐         ┌───────────▼──────────────┐
          │  Authority plane      │         │  On-demand adjudicator    │
          │  CorrectiveExecutor   │◄────────│  a NORMAL overseer agent  │
          │  (bounded vocabulary) │ verdict │  spawned for ONE finding, │
          │  runs as orchestrator │         │  ADVISES only (Opus tier) │
          └───────────────────────┘         └──────────────────────────┘
```

The cost guard is the heart of the design: **the overwhelming majority of
observations are normal and yield `None` with no LLM call.** An LLM is spawned
only when a detector marks its finding `requires_adjudication=True`.

### 1. Detection plane (deterministic, in-process)

`orchestrator/health_checks/detection_plane.py` defines the plane and is the
structural replacement for the watcher pod. The orchestrator builds one plane
(`default_detection_plane()`) and calls `DetectionPlane.evaluate(snapshot)` on
each runtime tick / lifecycle event. Each detector is a pure function
`snapshot -> Finding | None`:

- `EventStreamSnapshot` — a frozen, point-in-time view of pipeline state
  (running agents annotated with their `LifecycleOwner`, consensus, phase
  state, decision state, container transitions, gateway/cost counters,
  mid-turn messages, git state). Built on the event loop by
  `snapshot_from_health_context()`; in tests it is parsed from the slice-1
  calibration corpus fixtures (same field names — see
  [Calibration](#calibration-the-stop-crying-wolf-deliverable)).
- A `Detector` is any callable carrying `detector_key` / `name` attributes, so a
  production detector is driven verbatim by the calibration harness with no
  adapter.
- Detector execution is **exception-isolated**: a buggy detector degrades to "no
  finding" and is logged, never crashing the loop.

The "stop crying wolf" fix is structural here: a detector only fires on a
condition it can *prove* from the snapshot. The canonical example is
`PhaseStallDetector`, which is **lifecycle-owner-aware** — under
orchestrator-owned on-demand spawning ([#3064](https://github.com/jwbron/egg/issues/3064))
a phase can be RUNNING with zero live containers for a beat while the next
one-shot agent is about to spawn. That is *not* a stall (the
[#3230](https://github.com/jwbron/egg/issues/3230) false-alert root cause). The
detector fires only when nothing is queued to make progress
(`LifecycleOwner.NONE`), no HITL is parked, and the phase has been wedged past a
conservative grace window.

### 2. On-demand adjudicator (LLM, advisory only)

When a finding sets `requires_adjudication=True` (genuinely ambiguous /
high-stakes), the orchestrator escalates it through
`escalate_findings(findings, spawn_adjudicator=…)`. This spawns a **normal,
on-demand OVERSEER agent for that one finding** — not a standing watcher — via
the ordinary `spawn_agent_job(agent_role=OVERSEER, …)` path
(`routes/pipelines._escalate_finding_to_adjudicator`).

The adjudicator **ADVISES only**. It returns an `AdjudicationVerdict`
(`overseer/decision_maker.py`):

| Field | Meaning |
|-------|---------|
| `confirmed` | Does the adjudicator agree the finding is real? `False` is a calibration data point — the detector over-fired. |
| `recommended_action` | One of `none`, `nudge_agent`, `respawn_cohort`, `open_operator_hitl`. Advisory only. |
| `severity` | The adjudicator's own assessment (may differ from the detector's). |
| `reasoning` | Human-facing explanation. |

Parsing is defensive: a malformed/unparseable verdict degrades to a
conservative *unconfirmed* `open_operator_hitl`, so a broken adjudicator never
silently swallows a genuine deadlock.

### 3. Authority plane — the bounded corrective vocabulary

The overseer **advises**; the control plane **executes**.
`overseer/corrective.py`'s `CorrectiveExecutor` is the only thing that acts on a
recommendation, and it runs under the **orchestrator identity, never an agent**.

- **Closed vocabulary** — exactly `{nudge_agent, respawn_cohort,
  open_operator_hitl}` (`CORRECTIVE_ACTIONS`). `none` is the adjudicator's
  "false alarm / no action" and is deliberately **not** executable.
- **Dependency-injected** — the three side effects are passed in, so the
  authority logic is unit-testable and the real wiring lives in
  `routes/pipelines`.
- **Decision precedence** (first gate that trips wins):
  1. action ∉ vocabulary → `denied`
  2. zero-agent HITL park → `barred` (nothing running, nothing to correct — the
     §3 invariant)
  3. duplicate idempotency key → `deduplicated` (at-most-once)
  4. rate-limit window exceeded → `rate_limited` (sliding window)
  5. otherwise → `executed`
- **Audited** — every attempt is recorded to the `audit_sink`.

## Authority to act (§4 — make it structural)

Before the overhaul, alerts were informational only and the overseer agent's
attempt to open an operator decision was 403-denied. The delivered design makes
authority **structural** by splitting identities:

- The **OVERSEER agent** remains denied writes to the contract / decision store
  at the gateway — that denial *is* the gateway 403, and it is correct (an agent
  should not be able to forge operator decisions).
- The **control plane** (orchestrator identity) is not one of the gateway's
  per-role agent patterns, so its `open_operator_hitl` write is a control-plane
  operation, not an agent write. The `CorrectiveExecutor` runs there.

So a confirmed deadlock can now open a real operator HITL through an authorized,
bounded, audited path — without granting the agent that authority.

## Model tiering — stop running the overseer on Sonnet (§1)

Sonnet mis-classified the overseer's own legitimate bootstrap as a
prompt-injection attack and crash-looped. The fix splits the overseer's work
across cost/capability tiers, sourced from a single table
(`agent_model_resolution.OVERSEER_TIER_MODELS`) instead of the bespoke field:

| Tier | Model | Used for |
|------|-------|----------|
| `classify` | `haiku` | high-volume, single-shot classification (`overseer/classifier.py`) |
| `routine` | `sonnet` | routine corrective decisions (`overseer/decision_maker.py`) |
| `adversarial` | `opus` | high-stakes / adversarial adjudication — the on-demand adjudicator |

`resolve_overseer_model(tier, …)` turns a tier into a full model decision through
the **same per-agent resolver every other agent uses** (`resolve_agent_model`),
so the spawn path gets the Claude-Code alias + upstream identically — no bespoke
plumbing. The adversarial/decision tier resolves to **Opus** (the fleet
standard) and is the operator override surface via the `overseer` entry in
`agent_models`.

## Run it like every other agent (§1.5)

The overseer runs through the **same sandbox image and the same spawn/entrypoint
path** as every other agent — it is just a particular role
(`AgentRole.OVERSEER`) with different permissions and a different prompt. The
bespoke shape was the direct cause of the §1 self-injection loop, so it was
removed:

- `spawn_overseer_job` is **deleted**, folded into
  `spawn_agent_job(agent_role=OVERSEER, …)`.
- `EGG_OVERSEER_DECISION_MODEL` is **removed** from the spawned container's
  environment.
- The standing-pod respawn loop in `_run_pipeline` is **removed**; detection now
  runs in-process and the only agent spawned is the on-demand adjudicator.

Whatever monitoring the on-demand overseer needs arrives via its prompt/rule and
available tools/MCP — not a trust-and-run baked-in script. See
[`sandbox/agent-config/rules/overseer.md`](../../sandbox/agent-config/rules/overseer.md).

## Detector catalogue

`DetectionPlane.default()` registers the lifecycle-owner-aware phase-stall
detector (slice-4) plus the §5 coverage-gap survey (slice-8). Each detector
carries a stable `detector_key` that ties it to its calibration-corpus rows.
Detectors marked **adjudicate** set `requires_adjudication=True` and spawn the
on-demand adjudicator; the rest are deterministic and flow straight to the
bounded corrective vocabulary with no LLM.

| Layer | `detector_key` | Adjudicate? | Fires on |
|-------|----------------|:-----------:|----------|
| core | `phase_stall` | ✅ | RUNNING phase, zero agents, no owner queued, no HITL parked, past grace ([#3230](https://github.com/jwbron/egg/issues/3230)) |
| container / k8s | `container_death` | — | container exited abnormally |
| container / k8s | `container_oom_evicted` | — | OOMKilled / evicted ([#2948](https://github.com/jwbron/egg/issues/2948)) |
| container / k8s | `container_restart_loop` | ✅ | repeated same-role restarts |
| container / k8s | `overseer_self_injection` | — | overseer mis-reading its own bootstrap (the §1 loop signature) |
| orchestrator runtime | `runtime_thread_liveness` | ✅ | `_run_pipeline` thread dead ([#2234](https://github.com/jwbron/egg/issues/2234)/[#3233](https://github.com/jwbron/egg/issues/3233)) |
| orchestrator runtime | `duration_drift` | — | phase running far past its expected duration |
| orchestrator runtime | `agent_restart_propagation` | — | agent restart not propagated past its deadline |
| decision queue | `auto_advance_wedge` | ✅ | auto-advance wedged ([#2219](https://github.com/jwbron/egg/issues/2219)) |
| decision queue | `approved_decision_orphaned` | — | approved decision never acted on |
| decision queue | `restarted_decision_replay` | — | decision replay after restart |
| decision queue | `hitl_queue_backlog` | — | HITL queue backing up |
| worktree / branch | `worktree_corruption` | — | worktree corruption / lock anomaly |
| worktree / branch | `disk_inode_pressure` | — | disk / inode pressure |
| worktree / branch | `pr_external_mutation` | — | PR mutated outside the pipeline |
| worktree / branch | `pushed_pr_not_updated` | — | pushed but PR not updated |
| gateway | `gateway_error_spike` | — | gateway error-rate spike |
| gateway | `gateway_repeated_denial` | — | repeated identical denials |
| gateway | `gateway_token_expiry` | — | gateway token expiry |
| BRC / thrashing | `brc_thrash` | ✅ | reviewer/producer thrash that won't converge |
| BRC / thrashing | `incomplete_consensus_deferral` | — | unbounded incomplete-consensus deferral |
| cost / budget | `cost_anomaly` | — | token-cost anomaly vs. budget |
| LLM substrate | `llm_substrate_unreachable` | — | LiteLLM proxy unreachable ([#2769](https://github.com/jwbron/egg/issues/2769)) |
| LLM substrate | `effective_model_drift` | — | effective model ≠ requested model |
| LLM substrate | `anthropic_5xx` | — | sustained Anthropic 5xx |
| overseer self-health | `overseer_self_health` | — | overseer's own classifier/advisor failure rate over threshold |

> Detectors only fire in a live run once `snapshot_from_health_context()`
> populates the field they read; until then they are silent. The calibration
> corpus drives every detector with fully-populated fixtures today.

## Calibration — the "stop crying wolf" deliverable

Signal trustworthiness is deliverable #1, and it is an explicit, tested
deliverable rather than a hope. Detectors are calibrated against a
known-normal / known-bad corpus (`tests/overseer_calibration/`, documented in
[overseer-calibration-corpus.md](overseer-calibration-corpus.md)). The corpus
asserts, per detector, that a known-normal snapshot yields `None` and a
known-bad snapshot yields a `Finding` of the expected class/severity. Because
the production `EventStreamSnapshot` is field-compatible with the corpus
snapshot, a detector written against the production type is driven verbatim by
the harness — and the corpus never imports production code.

## The legacy LLM monitor modules

The `orchestrator/overseer/` package still carries the original LLM monitor
modules (`monitor.py`'s `OverseerMonitor` poll-classify-decide-act loop,
`classifier.py`, `decision_maker.py`'s escalation-ladder functions). These
remain in the tree for back-compat and the host-detector migration
(`overseer_owns_host_detection`), and they share the model-tiering and
adjudication types described above. New work should target the detection plane +
adjudicator + authority plane, not the standing-loop path. See the
[overseer package README](../../orchestrator/overseer/README.md) for the
module-by-module breakdown.

## Deprecation notes

- **`overseer_decision_maker_model`** (`orchestrator/models.py`) is
  **deprecated** and made inert by the overhaul. The overseer's base model now
  resolves through `resolve_agent_model(OVERSEER)` (→ Opus by default; override
  via `agent_models['overseer']`). Setting the field logs a one-line deprecation
  warning; the value is still honoured for back-compat by
  `resolve_overseer_model`. Folds [#2813](https://github.com/jwbron/egg/issues/2813).
- **`EGG_OVERSEER_DECISION_MODEL`** is no longer injected into agent
  environments.
- **`spawn_overseer_job`** is deleted (folded into
  `spawn_agent_job(OVERSEER)`).
- **`orchestrator/overseer/issue_filer.py`** is dead code — the canonical issue
  template now lives at `shared/egg_overseer/issue_template.py`. The
  orchestrator-side literal is retained byte-for-byte only for a CI assertion
  ([#1962](https://github.com/jwbron/egg/issues/1962)).
- **`overseer_auto_file_issues_mode`** defaults to `"shadow"` (the decision
  surfaces as an `OVERSEER_ALERT` + HITL gate); flipping to `"live"` is gated on
  telemetry validating the gate.

## Out of scope

- **`monitor.py` decomposition** (~2,050 lines) rides
  [#2817](https://github.com/jwbron/egg/issues/2817) / the
  [#2261](https://github.com/jwbron/egg/issues/2261) decomposition program — it
  is **not** re-decomposed by the overseer overhaul.

## Related

- [Health check framework README](../../orchestrator/health_checks/README.md) —
  the detection plane, Finding types, and the legacy `HealthCheck` framework
- [Overseer package README](../../orchestrator/overseer/README.md) —
  server-side module breakdown
- [Calibration corpus](overseer-calibration-corpus.md) — the row schema and
  red→green workflow
- [Orchestrator architecture](orchestrator.md)
- [`sandbox/agent-config/rules/overseer.md`](../../sandbox/agent-config/rules/overseer.md)
  — the on-demand overseer's rule/prompt
- Issue [#2270](https://github.com/jwbron/egg/issues/2270) — the overseer
  overhaul umbrella
