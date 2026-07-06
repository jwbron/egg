# Issue #3364 — Refined Analysis

**Slim the `/sdlc` skill to run + report + HITL; move monitoring/recovery into the orchestrator + overseer**

Refiner artifact. Grounded against `main @ f139716c4` on 2026-07-06. PR A (#3421,
consensus-timeout "Retry phase" → `restart_phase`) has **already landed** and is out
of scope — do not redo it. This pipeline is the three remaining, mutually
independent PRs **B, C, D**.

---

## 1. Problem statement

A long, failure-rich 19-slice run (#3312) surfaced monitoring + recovery behaviors
that kept it alive. The original issue framing was to codify that playbook into
`skills/sdlc/SKILL.md`. **That is the wrong home.** The skill's job is narrow —
*run pipelines, keep the user updated, broker HITL decisions + overseer
escalations*. Detection, classification, and recovery belong in the
**orchestrator + overseer**, which have in-pod liveness and own the pipeline state
machine; there they protect *every* pipeline, not just an attended `/sdlc` session.

`SKILL.md` is 1600 lines and still carries all five host-side detector blocks gated
behind `overseer_owns_host_detection`. The host-detector migration (#1962) is
**closed**, so those blocks are dead/duplicated logic to **delete**, not extend.

Three PRs, three risk profiles:

| PR | Theme | Surface | Risk |
|----|-------|---------|------|
| B | Long-haul monitoring tooling (issue items 3+4) | `bin/wait-status` flags + `slice.closed` event | Additive / low |
| C | Supervision hardening (issue items 1+6) | `JobSupervisor` / `supervision_policy.py` | Behavioral / medium-high |
| D | Slim the skill (cleanup + items 2/7 backstops) | `SKILL.md`, `overseer_owns_host_detection` | Deletion / medium (gated) |

B, C, D are independent. **D carries one hard prerequisite** (§5).

---

## 2. Grounded facts (verified 2026-07-06 @ f139716c4)

Load-bearing claims re-verified live; a few file:line refs in the issue body have
since been refactored — the corrected locations are below.

**PR B**
- `skills/sdlc/bin/wait-status` — 328 lines, pure stdlib. Exposes only
  `--since` / `--inner-timeout` / `--max-iterations` (args at lines 293/301/310).
  No `--exclude-types` / `--quiet`. ✔ greenfield.
- No slice event type exists anywhere: `grep -rn 'slice.closed|slice_closed|
  SLICE_CLOSED' orchestrator/ shared/` → empty. ✔
- Emit sites exist: `SliceScheduler.record_complete` (`slice_scheduler.py:361`),
  `record_failure` (`:371`) — both currently emit no event.
- Allowlist is `_STATUS_WAIT_EVENT_TYPES` at
  **`orchestrator/routes/pipelines/__init__.py:405`** (issue said `pipelines.py:549`
  — the module was split into a package; checked at
  `_routes_status.py:349`).

**PR C**
- `shared/egg_agent/auth_errors.py` (docstring + `_AUTH_FATAL_PATTERNS`, lines
  ~24-48): throttling signatures (429 / "rate limit" / "overloaded") are
  **intentionally absent** from `EX_AUTH_FATAL` — they must "stay on the normal
  backoff-and-respawn path." A weekly/usage cap *is* matched on its own wording and
  routes to `EX_AUTH_FATAL`; a bare 429 / "overloaded" cap wall does **not** — it
  falls through to `abnormal`.
- `orchestrator/supervision_policy.py:19` — `SUPERVISION_BACKOFF_CAP_SECONDS = 30`;
  linear `streak * factor` capped at 30s. Streak anomaly
  `agent-invocation-fail-streak` (`:25-26`). ~10 failures exhaust in minutes.
- Failure-streak exhaustion broadcast lives at
  `orchestrator/concurrent_executor.py:988`.

**PR D**
- `skills/sdlc/SKILL.md` — exactly 1600 lines. Detector blocks present at:
  `### Host detector migration (issue #1962)` **553**; `#### Overseer-Absent
  Fallback` **573**; **Stall detection** **637**; **Silent agent detection** **644**;
  **NACK escalation** **679**; **Long-Running Phase Detection** **722**; **Stuck
  Pipeline Rescue** **750**; short-flow **Stall detection** **1517**. Each is gated
  by `overseer_owns_host_detection`.
- `overseer_owns_host_detection` — declared at
  **`orchestrator/models/_config.py:388`** (`default=False`; issue said
  `models.py:906` — models split into a package), read at
  **`orchestrator/routes/pipelines/_routes_status.py:126-127`**. Only these two
  production references.

**PR D prerequisite — parity is NOT a rubber stamp (new refiner finding):**
- The five host-detector anomaly identifiers (`agent-stall`, `agent-silent`,
  `agent-nack-unresolved`, `phase-long-running`) appear in production **only** in
  `shared/egg_overseer/state.py` as a dedup/last-alert *signature map* — not as
  emitters. Everywhere else they are in **tests**.
- The live overseer monitor (`orchestrator/overseer/monitor/`) emits a *different*
  deterministic set via `_broadcast_alert(...)`: `post_consensus_stall`,
  `rerun_anomaly`, `status_inconsistency`, `hitl_propagation_failure`,
  `cross_phase_inconsistency`, `orchestrator_unreachable`
  (`_consensus_stall.py`, `_anomaly_checks.py`).
- Agent-level classification into the `agent-stall`/`agent-silent`/… vocabulary
  runs through the overseer's **Haiku classifier / adjudicator**
  (`orchestrator/overseer/decision_maker.py`, `shared/egg_overseer/advisor.py`) — an
  LLM path, not a deterministic threshold detector.
- **`run_migrated_detectors`** (named in `SKILL.md:555` as the migration target)
  exists in **no production file** — only in `SKILL.md` prose.

⟹ Deleting a host block is *not* automatically safe. For each block we must
positively identify the overseer emitter (deterministic structural check **or** the
classifier path) that produces an `OVERSEER_ALERT` the skill already surfaces. Where
no emitter exists, that block's coverage would go dark. This is exactly what the
prerequisite gates on, and it is genuine verification work.

---

## 3. Scope

**In scope:** PRs B, C, D as separate slices/units with separate risk profiles.

**Out of scope (do not touch):**
- PR A (item 5) — landed via #3421.
- Item 8 (durable `context-measurement` surface) — deferred to #3249.
- The 13 visibility-gap follow-ups from the issue comments (per-role
  `commits_ahead`, consensus-window countdown, alert feed, …) — tracked in
  #3369 / #3499 / #3508 / #3509, not here.
- Item 2 liveness-before-destructive gating is *largely done* (#3341 via #3343);
  the skill keeps only a thin backstop, it is not re-implemented here.

---

## 4. Acceptance criteria

### PR B — Long-haul monitoring tooling

- **AC-B1** `bin/wait-status` gains `--exclude-types` (comma-separated event-type
  filter, dropping matching JSON lines client-side) and `--quiet`. Filtering is
  purely client-side JSON-line filtering; the launcher stays pure-stdlib and
  self-contained (no new imports/deps).
- **AC-B2** Existing `--since` / `--inner-timeout` / `--max-iterations` behavior and
  the non-filtered default output are unchanged (regression-guarded).
- **AC-B3** A new `slice.closed` `EventType` exists and is emitted at **both**
  `SliceScheduler.record_complete` and `record_failure`, carrying enough payload to
  distinguish success vs. failure and identify the slice.
- **AC-B4** `slice.closed` is added to `_STATUS_WAIT_EVENT_TYPES` so it passes the
  `/status/wait` allowlist and reaches `wait-status` consumers.
- **AC-B5** Tests cover: the two new flags (including a filter that drops a type and
  `--quiet`), and that a slice completion **and** a slice failure each emit exactly
  one allowlisted `slice.closed` event.

### PR C — Supervision hardening

- **AC-C1** A rate-limit / cap-wall classification **distinct from `abnormal`**
  exists: the all-producers `agent-invocation-failure` streak signature (bare 429 /
  "rate limit" / "overloaded", i.e. the throttling signatures `auth_errors.py`
  deliberately excludes from fatal) is classified as **transient rate-limit**, not
  routed into the `agent-invocation-fail-streak` halt.
- **AC-C2** On that classification the supervisor performs a **windowed paced
  retry** across the rolling cap window (hours-scale, not the 30s
  `SUPERVISION_BACKOFF_CAP_SECONDS`) without hammering the API.
- **AC-C3** The paced retry uses a `restart_phase`-equivalent that **preserves
  landed slices** — completed slice work is not discarded on recovery.
- **AC-C4** A **deterministic-loop guard** distinguishes transient from
  deterministic failure: if a restart reproduces the *identical* failure at the
  *same point* (same signature/phase progression), the supervisor **stops and
  escalates** instead of looping.
- **AC-C5** The retry bounding / escalation behaves per the operator's resolution of
  **cq-1** (§6).
- **AC-C6** Genuinely-fatal auth errors (`EX_AUTH_FATAL`, incl. matched weekly/usage
  caps) and ordinary `abnormal` failures are **unchanged** — the new path triggers
  only on the throttling-streak signature; existing halts still fire.
- **AC-C7** Tests cover: throttling-streak → rate-limit classification (not
  `abnormal`); paced-retry pacing/window; landed-slice preservation across a
  restart; and the loop guard escalating on an identical-failure reproduction
  vs. continuing when the restart advances state.

### PR D — Slim the skill (gated on §5)

- **AC-D1** Deleted from `SKILL.md`: the `Host detector migration (issue #1962)`
  section, the `Overseer-Absent Fallback`, and the five host-side detector
  **blocks** — Stall detection, Silent agent detection, NACK escalation,
  Long-Running Phase Detection, and the **host-side detection *trigger*** of Stuck
  Pipeline Rescue (≈ lines 553-793 + the short-flow Stall block ≈ 1517).
- **AC-D2** `overseer_owns_host_detection` is **removed entirely** — the field in
  `orchestrator/models/_config.py` and both references in
  `orchestrator/routes/pipelines/_routes_status.py` — concluding the calibration
  window (not flipping the default). No dangling references remain
  (`grep` clean).
- **AC-D3 (preserve alert-driven renders — critical):** removing the host-side
  *detection* must NOT remove the skill's **render-on-`OVERSEER_ALERT`** paths. The
  `Long-Running Implement Phase` `AskUserQuestion` flow and the `Unresolved NACK`
  `AskUserQuestion` flow still fire when the matching `OVERSEER_ALERT` arrives.
- **AC-D4** Kept intact: argument parsing/seed, pre-refine triage, submit, the
  Monitor `wait-status` loop (render + cursor threading), HITL decision brokering
  (phase_gate / choice / feedback, two-wave surfacing, `cq-N` / `feedback-N`),
  surfacing `OVERSEER_ALERT`s to the user, and the **user-initiated** Stuck Pipeline
  Rescue workflow (Steps 1-3) — only its host-side detection *trigger* is removed.
- **AC-D5** A short last-resort debugging section carries the two thin backstop
  rules: never blind-action a destructive recommendation (always route a
  destructive rec through `AskUserQuestion`); `TaskStop` the Monitor before
  re-arming it.
- **AC-D6 (gate):** each deleted detector block is coverage-mapped to a concrete
  overseer emitter (a `_broadcast_alert(...)` structural check **or** the
  classifier path) that produces an `OVERSEER_ALERT` the skill already surfaces.
  The coverage-map is recorded in the PR/plan. **No block is deleted without a
  confirmed emitter.** Any block found to lack overseer parity is handled per §5
  (raise HITL — do not silently delete into a dark gap).

---

## 5. PR D prerequisite gate (hard dependency)

The detector deletion is **gated** on a coverage-map that, per §2, is real work
because the naive "the overseer already does this" assumption does not hold for all
five blocks. For each deleted block, the map must name the overseer emitter and the
`OVERSEER_ALERT` subject the skill surfaces:

| Deleted host block | Candidate overseer owner | Parity status to confirm |
|---|---|---|
| Stall detection | classifier `agent-stall` and/or `post_consensus_stall` structural check | **Verify** the alert reaches the skill; classifier path is LLM, not deterministic |
| Silent agent detection | classifier `agent-silent` | **Verify** — no deterministic emitter found; may rely solely on classifier |
| NACK escalation | classifier `agent-nack-unresolved` | **Verify** — skill's `Unresolved NACK` render must fire on the alert (AC-D3) |
| Long-Running Phase Detection | classifier `phase-long-running` | **Verify** — `Long-Running Implement Phase` render preserved (AC-D3) |
| Stuck Pipeline Rescue (trigger only) | overseer stall alerts above | Rescue *workflow* stays; only the trigger migrates |

If the coverage-map finds a block with **no** confirmed overseer emitter, that is a
genuine parity gap. It is **not** the refiner's call to delete-anyway vs.
keep-the-block vs. add-an-overseer-detector (scope expansion) — that is an operator
decision, raised as a HITL **at the point of discovery** (plan or implement),
carrying the specific block + evidence. It is deliberately **not** pre-registered
here because it is contingent on the mapping outcome.

---

## 6. HITL decision (cq-1)

One genuine, unconditional, operator-facing decision arises in PR C. Everything else
in the issue is settled by the operator's directive or is planner/implementer
mechanics.

**cq-1 — PR C: how should the windowed paced-retry bound itself when a cap wall
persists?** A weekly/subscription cap can stay shut for hours-to-days. "Retry across
the rolling window, don't hammer" fixes the *pacing* but not the *ceiling*, and the
ceiling is directly operator-visible (an unattended pipeline could sit paused for a
long time). Registered as an `add-decision` choice.

---

## 7. Notes for planning

- B, C, D → three independent slices. C is the highest-risk (live supervision
  behavior); prefer heavy test coverage over the classification + loop-guard.
- Corrected file:line anchors in §2 supersede the issue body's pre-refactor refs
  (`pipelines.py:549`, `models.py:906`, `event_loop.py`).
- PR D plan must schedule the §5 coverage-map **before** any SKILL.md deletion, and
  must explicitly preserve the AC-D3 render-on-alert paths.


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

Resolved Questions:

**PR C — when a rate-limit / subscription cap-wall persists, how should the windowed paced-retry bound itself?**
Answer: Retry until the cap lifts (no hard ceiling) BUT emit an OVERSEER_ALERT once the wait crosses a threshold, so an attended operator is informed while auto-recovery continues. (Resolved as cq-1 on the contract.)
