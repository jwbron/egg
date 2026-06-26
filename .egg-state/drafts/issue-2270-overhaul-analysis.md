# Issue #2270 — Overseer Overhaul: Refinement Analysis

**Pipeline:** `issue-2270-overhaul` · **Phase:** refine · **Author:** refiner
**Grounded against the tree:** 2026-06-26 (all file:line anchors verified this run)

> This is the **overseer-overhaul umbrella** (reinstated 2026-06-10, still live). Two
> layers, both in scope: (1) the §1–§6 concrete findings are **commitments**; (2) on
> top of that it is **open season** — question the fundamentals, and net-negative-in-lines
> is a feature. This analysis grounds the findings, flags the stale ones, and isolates the
> single architectural fork that gates everything downstream.

---

## 1. Problem (confirmed)

The overseer is simultaneously **too loud** (a stream of false alarms operators learn to
ignore) and **useless at the moment of need** (real deadlocks surface as a generic
`stuck-phase-transition` or nothing). The `issue-3200` proving run (2026-06-25) made the
failure modes reproducible: its entire alert stream was false positives and the overseer
burned its respawn budget fighting its own bootstrap.

**Live corroboration captured this very phase:** while drafting this analysis the refiner
received two OVERSEER_ALERTs reflected into its context as mid-turn "operator directives" —
a `[high]` `agent-heartbeat-stall` at 00:10:51 (a **false positive**: the agent was making
tool calls every 2–3s) immediately retracted by a `[low]` correction at 00:12:56. This is a
live instance of **two** §2 defects at once: the false stall alert *and* the alert-reflection
vector (agent-bus broadcasts surfaced to a working agent as operator HITL directives).

---

## 2. Grounded facts — claim verification (read before planning)

The issue is heavily author-specified. Most code-claims are **confirmed**; line numbers
drifted and a few claims are **stale**. Plan/architect must not chase the stale ones.

| # | Issue claim | Verdict | Real anchor |
|---|-------------|---------|-------------|
| §1 | `overseer_decision_maker_model` defaults `"sonnet"`, bypasses the per-agent resolver | **CONFIRMED** | `orchestrator/models.py:726-728`; resolved via `classify_model(decision_model)` at `kubernetes_spawner.py:2919`, **not** `resolve_agent_model` (`agent_model_resolution.py:497`) |
| §1.5 | `spawn_overseer_job` is special-case plumbing; OVERSEER bespoke env flags | **CONFIRMED** | `spawn_overseer_job` `kubernetes_spawner.py:2883-2960`; sets `EGG_OVERSEER_MODE`/`EGG_OVERSEER_POLL_INTERVAL`/`EGG_OVERSEER_DECISION_MODEL` at `2922-2926`. Generic `spawn_agent_job` at `1228`; `AgentRole.OVERSEER` already recognized (`672`) |
| §1.5 | Bundled `overseer_monitor.py` "baked-in script" bootstrap | **CONFIRMED** | `sandbox/overseer_monitor.py` (802 lines), baked via `sandbox/Dockerfile`, invoked by prompt at `kubernetes_spawner.py:2931` (`python3 .../overseer_monitor.py --once`) |
| §2 | OVERSEER_ALERTs reflected back as "operator directives" via mid-turn injection | **CONFIRMED** | `shared/egg_agent/client.py:629+` → `midturn_messages.py:76` `_INJECT_FROM_ROLES = {"overseer","orchestrator","human","operator","user"}` — **no distinction** between an overseer agent-bus broadcast and an operator HITL directive |
| §2 | Branch-divergence detector flags `(#NNNN)` subjects, not ancestor/patch-id | **CONFIRMED** | `routes/pipelines.py:15819` `_BRANCH_DIVERGENCE_PR_RE = re.compile(r"\(#\d+\)")`; `_check_branch_divergence_for_alert()` `15822-15907`; tests in `orchestrator/tests/test_branch_divergence_alert.py` |
| §3 | `_check_and_respawn_overseer` overseer-specific respawn machinery | **CONFIRMED — line drift** | NOT `pipelines.py:433-596`; actually `orchestrator/routes/pipelines.py:685-848` (164 lines), invoked during phase polling in the same file |
| §4 | `roles.py:can_modify` 403-denies overseer `register_open_question` | **STALE / NOT FOUND** | `shared/egg_contracts/roles.py:147-170` `can_modify` uses a `FIELD_OWNERSHIP` table (`35-86`) with no overseer entry; `overseer` is not even a `Role` enum value here. The actual authority denial lives elsewhere (gateway action-guards / RBAC) — **plan must locate the real enforcement point before designing §4** |
| §5 | "wire OverseerSelfMonitor into alert path" (implies unwired) | **PARTLY STALE** | `OverseerSelfMonitor` (`overseer/self_monitor.py:24-166`) is **already** instantiated (`monitor.py:111`), recorded throughout (`515,665,669,687,704,1445,1773`) and health-checked at cycle end (`monitor.py:1995`). Open nuance: does `check_health()` actually *emit alerts* or only log? That is the real §5 gap |
| §5 | Tier-1 detectors / Tier-2 `agent_inspector` | **CONFIRMED — relocated** | Tier-1 lives **orchestrator-side** in `orchestrator/health_checks/tier1/` (6 classes: `container_liveness`, `startup_state`, `phase_output`, `state_consistency`, `consensus_stall`, `incomplete_consensus_stall`; barrel `__init__.py:14-21`). No standalone Tier-2 `agent_inspector` class — the overseer agent *is* the Tier-2 layer (`overseer/monitor.py`) |
| §6 | `issue_filer.py` "preserved-but-unused (#1962)" — confirm no imports | **STALE — IT IS USED** | `orchestrator/overseer/issue_filer.py` is imported in `overseer/__init__.py:27` and `monitor.py:36`, and **called** at `monitor.py:675` (`file_diagnostic_issue` inside `_execute_escalation`). It is **not** dead code — do not delete on the #1962 premise without re-confirming |

**Overseer subsystem size:** `orchestrator/overseer/` = ~3,216 lines (`monitor.py` 2,030, `classifier.py` 341, `decision_maker.py` 340, `issue_filer.py` 269, `self_monitor.py` 165, `utils.py` 29, `__init__.py` 42) — plus `sandbox/overseer_monitor.py` 802, the Tier-1 `health_checks/` package, and the respawn/branch-divergence code in `routes/pipelines.py`.

---

## 3. The central fork (open-season) — architectural shape

This is the decision that **gates §1, §1.5, §3 and most of §5**, so it must be settled first.

Two facts make the fork concrete:
1. The deterministic Tier-1 detectors **already run orchestrator-side** (`health_checks/tier1/`) — the agent pod is not where most detection happens.
2. #3064 (merged) moved to **orchestrator-owned on-demand spawning** — there is no longer a long-lived phase cohort for a watcher pod to shadow, and a pod respawned every ~90s exit-0 (including multi-hour HITL parks with zero agents running) is pure cost bleed.

Together these strongly suggest the respawning watcher pod is the wrong shape. Options:

- **(A) Normalize-the-agent.** Keep the overseer as an agent; fold `spawn_overseer_job` into `spawn_agent_job(agent_role=OVERSEER)`, fix model tiering via `resolve_agent_model`, delete bespoke flags, fix calibration. Smallest conceptual change; keeps a respawning pod and the bootstrap surface that caused the §1 self-injection loop.
- **(B) Orchestrator-side evaluation.** Retire the agent pod. Promote overseership to deterministic orchestrator-side evaluation over the event stream (extend the existing Tier-1 package), invoking an LLM **only** for adversarial/high-stakes classification via the normal resolver — no standing pod, no respawn machinery, no baked-in monitor script. Largest deletion; directly realizes the issue's "net-negative is a feature" and "successor shape is plausibly orchestrator-side evaluation over the event stream."
- **(C) Hybrid (recommended).** Deterministic detection + corrective vocabulary run orchestrator-side (kills respawn churn, the bootstrap loop, and the false-stall lifecycle blindness structurally); spawn a **normal, on-demand** agent (via `spawn_agent_job`, Opus-tier, no special plumbing) *only* to adjudicate ambiguous/adversarial escalations. Satisfies §1.5's "if it remains an agent, it is a normal agent — no parallel plumbing" while removing the standing-pod cost and churn.

**Refiner recommendation: (C), leaning toward (B) for the standing-pod portions.** Both eliminate the respawn loop, the baked-in bootstrap (root cause of §1's self-injection), and the lifecycle-owner-blind stall detector — structurally, not by tuning a threshold. This is an **operator/architect-level decision** → registered as HITL cq-1.

---

## 4. Scope, commitments, and proposed acceptance criteria

§1–§6 are commitments; the architectural fork reshapes *how* §1/§1.5/§3 are met but not *whether*. Proposed acceptance criteria (to be ratified in plan):

- **AC-1 (model, §1):** overseer decision tier no longer defaults to Sonnet; model resolved through `resolve_agent_model` with tiering (Haiku classify / Sonnet routine / Opus adversarial); `overseer_decision_maker_model` deprecated. Folds #2813.
- **AC-2 (no special case, §1.5):** no `spawn_overseer_job`, no `EGG_OVERSEER_*` bespoke flags, no baked-in `overseer_monitor.py` trust-and-run bootstrap. Any surviving agent runs through `spawn_agent_job` + the standard entrypoint/image.
- **AC-3 (calibration — deliverable #1, §2):** a **tested** known-normal/known-bad corpus (self-injection loop, alert-reflection, #3230 false stall, #2242 heartbeat-stall, #2222/#2224 branch-divergence via ancestor/patch-id not subject regex, #2948 eviction-misread) that detection is calibrated against; stall detector made lifecycle-owner-aware; alert-reflection path stops surfacing agent-bus broadcasts as operator directives. Add #2059/#2132 thrashing/spinning/improper-tool-use definitions.
- **AC-4 (lifecycle, §3):** respawn churn eliminated (no overseer during HITL parks with zero agents running); `_check_and_respawn_overseer` folded into general agent-restart machinery or removed; escalation-history/generation-token reset hygiene on restart/recycle.
- **AC-5 (authority, §4):** a structural, authorized control-plane path to open operator HITLs + a bounded corrective vocabulary (nudge, cohort respawn). **Precondition:** locate the *real* authority-denial enforcement point (the `roles.py:can_modify` claim is stale — see §2).
- **AC-6 (coverage gaps, §5):** verified/extended detector coverage — prioritized subset (see HITL cq-2), each new class lifecycle-owner-aware and corpus-tested; resolve the `OverseerSelfMonitor` alert-emission nuance.
- **AC-7 (cleanup, §6):** net-negative line count; collapse fail-soft scaffolding; de-dup advisor-escalation plumbing; harden two-tier `file_issue` dedup. **Do not** delete `issue_filer.py` on the #1962 "unused" premise — it is used (§2); re-confirm before any removal. `monitor.py` decomposition rides #2817 — **not** re-decomposed here.

---

## 5. Open questions (registered as HITL)

- **cq-1 — Architectural shape:** A (normalize the agent) / B (orchestrator-side evaluation, retire the pod) / C (hybrid). Refiner recommends **C/B**. Gates §1, §1.5, §3, §5.
- **cq-2 — Pipeline scope:** deliver all §1–§6 in this pipeline, or land the **spine** first (§1+§1.5+§2-calibration+§4-authority+§6-cleanup) and gate the §5 coverage-gap *expansion* on the calibrated corpus? Refiner recommends **spine-first**; coverage-gap breadth (the long §5 survey) is where scope can run away.

## 6. Explicitly out of scope / deferred

- `monitor.py` (~2,030 lines) structural decomposition → rides **#2817**, not here.
- Full §5 coverage-gap survey delivered breadth-first in one pipeline (gate on cq-2).
- Flipping `overseer_auto_file_issues_mode` shadow→enforce → only after telemetry validates the gate (§6); not a refine-time commitment.
