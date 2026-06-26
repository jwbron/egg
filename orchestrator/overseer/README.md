# `overseer/` — server-side overseer logic

Server-side logic for the overseer, the LLM-assisted layer of pipeline health
monitoring. The orchestrator runs deterministic detection in-process (see
[`health_checks/`](../health_checks/README.md)); this package owns the pieces
that need a model: the **on-demand adjudicator** verdict schema, the **bounded
corrective-action executor**, the model-tiering decision functions, overseer
self-health, and the legacy poll-classify-decide-act monitor loop.

For the end-to-end architecture — detection plane → adjudicator → authority
plane — read [docs/architecture/overseer.md](../../docs/architecture/overseer.md)
first. This README is the module-by-module map.

## Where this sits

After the overseer overhaul ([#2270](https://github.com/jwbron/egg/issues/2270))
the orchestrator-side **detection plane** does the watching: cheap deterministic
detectors run over an `EventStreamSnapshot` on the event loop, and the
overwhelming majority of observations resolve to "no finding" with **no LLM
call**. This package is invoked only at the edges:

- a detector marks a finding `requires_adjudication=True` → the orchestrator
  spawns a **normal on-demand OVERSEER agent** for that one finding, which
  ADVISES using the verdict schema in `decision_maker.py`;
- a finding (adjudicated or routine) resolves to a corrective action → the
  **`CorrectiveExecutor`** in `corrective.py` executes it under the orchestrator
  identity, within a closed vocabulary.

The overseer runs as a normal agent (`AgentRole.OVERSEER`) through the standard
`spawn_agent_job` path — there is no bespoke `spawn_overseer_job`, no
`EGG_OVERSEER_DECISION_MODEL`, and no standing respawn loop.

## Modules

| Module | What it owns |
|--------|--------------|
| `decision_maker.py` | The on-demand **adjudication** schema (`AdjudicationVerdict`, `ADJUDICATION_ACTIONS`, prompt builder + defensive parser) and the legacy escalation-ladder decision functions (`decide_corrective_action`, `decide_escalation_level`, `compose_redirect_message`). |
| `corrective.py` | The **authority plane**: `CorrectiveExecutor` — bounded, audited, idempotent, rate-limited execution of the closed corrective vocabulary `{nudge_agent, respawn_cohort, open_operator_hitl}`. Runs as orchestrator, never an agent. |
| `classifier.py` | Haiku-tier classification (`classify_stall`, `classify_error`, `detect_loop`, `check_alignment`). |
| `self_monitor.py` | `OverseerSelfMonitor` (poll timing, per-model LLM cost, classifier/advisor failure rates) **and** the `detect_overseer_self_health` detection-plane detector. |
| `monitor.py` | The legacy long-lived `OverseerMonitor` poll-classify-decide-act loop (~2,050 lines). Retained for back-compat / the host-detector migration; decomposition is tracked out of band (see [Out of scope](#out-of-scope)). |
| `issue_filer.py` | **Dead code.** Preserved byte-for-byte only for a CI assertion; the canonical issue template lives at `shared/egg_overseer/issue_template.py` ([#1962](https://github.com/jwbron/egg/issues/1962)). |
| `utils.py` | Shared helpers (`parse_json_or_fallback`). |

## The adjudication verdict (`decision_maker.py`)

A detection-plane `Finding` with `requires_adjudication=True` spawns a one-shot
overseer adjudicator (Opus, via the adversarial tier). It returns **only** a
JSON verdict, parsed into an `AdjudicationVerdict`:

- `confirmed` — is the finding a real problem? `False` means the detector
  over-fired (a calibration data point).
- `recommended_action` — one of `ADJUDICATION_ACTIONS = {none, nudge_agent,
  respawn_cohort, open_operator_hitl}`. **Advisory only** — the
  `CorrectiveExecutor` decides whether/how to execute.
- `severity`, `reasoning`.

`parse_adjudication_verdict` is deliberately conservative: an unparseable
response degrades to an *unconfirmed* `open_operator_hitl` (only when the
detector demanded adjudication), so a broken adjudicator never silently drops a
real deadlock.

## The corrective executor (`corrective.py`)

`CorrectiveExecutor.execute(action, …)` is the single authorized actuator. It
walks a fixed precedence and returns an immutable `CorrectiveOutcome`:

1. action ∉ vocabulary → `denied`
2. zero agents running (HITL park) → `barred`
3. duplicate `idempotency_key` → `deduplicated`
4. rate-limit window exceeded → `rate_limited`
5. otherwise → `executed`

The three side effects (`open_operator_hitl`, `nudge_agent`, `respawn_cohort`)
are **injected**, so the authority logic is unit-testable in isolation; the real
wiring lives in `routes/pipelines`. `open_operator_hitl` is the structural §4
authority path — it runs as the orchestrator/control-plane identity, so it can
open an operator HITL even though the OVERSEER *agent* is gateway-denied contract
writes.

## Model tiering

The overseer's work is split across cost/capability tiers sourced from
`agent_model_resolution.OVERSEER_TIER_MODELS` (not the deprecated bespoke field):

| Tier | Model | Module |
|------|-------|--------|
| `classify` | `haiku` | `classifier.py` |
| `routine` | `sonnet` | `decision_maker.py` (`DECISION_MODEL`) |
| `adversarial` | `opus` | the on-demand adjudicator |

`resolve_overseer_model(tier, …)` routes through the same per-agent resolver
(`resolve_agent_model`) every other agent uses. The deprecated
`PipelineConfig.overseer_decision_maker_model` is inert and superseded; set
`agent_models['overseer']` to override the decision tier. Folds
[#2813](https://github.com/jwbron/egg/issues/2813).

## Overseer self-health (`self_monitor.py`)

`OverseerSelfMonitor` tracks the overseer's own metrics: poll-cycle timing,
message volume, **lifetime + hourly LLM cost broken down per model** (§5
cost-tracking fix — lifetime totals are accumulated separately so the bounded
recent-call deque can't undercount them), and classifier/advisor failure rates.
`check_health()` emits a structured alert through an optional `alert_sink` the
moment a **new** concern appears, deduped on the concern signature so a
persistent concern is not re-broadcast every cycle. `detect_overseer_self_health`
is the matching detection-plane detector (deterministic,
`requires_adjudication=False`): it fires when the overseer's own
classifier/advisor failure rate exceeds the snapshot threshold — i.e. the
overseer's reasoning substrate is itself failing and its verdicts can no longer
be trusted.

## Out of scope

- **`monitor.py` decomposition** (~2,050 lines) rides
  [#2817](https://github.com/jwbron/egg/issues/2817) /
  [#2261](https://github.com/jwbron/egg/issues/2261) — it is **not**
  re-decomposed by the overseer overhaul.

## Related

- [docs/architecture/overseer.md](../../docs/architecture/overseer.md) — full
  architecture
- [health_checks/README.md](../health_checks/README.md) — detection plane,
  Finding types, detector catalogue
- [docs/architecture/overseer-calibration-corpus.md](../../docs/architecture/overseer-calibration-corpus.md)
  — calibration corpus contract
