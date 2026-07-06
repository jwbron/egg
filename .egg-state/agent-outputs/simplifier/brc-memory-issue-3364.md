# simplifier — durable BRC memory — issue-3364 (refine phase)

## Role contract (verified against shared/egg_contracts/artifact_spec.py:142-145)

- Producer artifact (refine): `analysis-draft-human` at
  `.egg-state/drafts/issue-3364-analysis-human.md` — a faithful, jargon-free
  rendering of the refiner's `analysis-draft`
  (`.egg-state/drafts/issue-3364-analysis.md`). Gated by `reviewer_refine` on
  "faithful + jargon-free". Existence-only check at propose time.
- Identifier is `issue-3364` (== pipeline_id); refiner draft path confirmed
  ABSENT under both `issue-3364-analysis.md` and bare `3364-analysis.md`.
- Dependency: I propose AFTER the refiner's CONSENSUS_PROPOSE. Do NOT
  fabricate the analysis-human from the raw task_description — faithfulness to
  the refiner's analysis (the source of truth) is the gated property.
- gh CLI is DENIED for simplifier role — use contract task_description +
  refiner analysis + MCP tools, never `gh issue view`.

## Task (contract task_description, full text read via mcp__sdlc__show_contract)

Issue #3364: slim the /sdlc skill to run + report + HITL. PR A already landed
(#3421). This pipeline = PRs B, C, D (mutually independent slices):
- **PR B** — long-haul monitoring tooling: `--exclude-types`/`--quiet` on
  `skills/sdlc/bin/wait-status`; new `slice.closed` EventType emitted at slice
  scheduler `record_complete`/`record_failure`, added to `/status/wait`
  allowlist (`_STATUS_WAIT_EVENT_TYPES`). Additive/low-risk.
- **PR C** — supervision hardening in `JobSupervisor` /
  `orchestrator/supervision_policy.py`: rate-limit (cap-wall) classification
  distinct from `abnormal`; windowed paced retry preserving landed slices;
  deterministic-loop guard.
- **PR D** — slim `skills/sdlc/SKILL.md` (1600 lines): delete #1962 host
  detector migration section + Overseer-Absent Fallback + five host-side
  detector blocks; GATE deletion on coverage-map to overseer OVERSEER_ALERT
  detectors (`orchestrator/overseer/monitor/`); remove
  `overseer_owns_host_detection` entirely. Keep run+report+HITL skill core.
- Out of scope: item 8 (→#3249); the 13 visibility-gap items (→#3369/#3499/
  #3508/#3509).

## Event log

### 2026-07-06 ~18:15 UTC — event #1, action=propose (producer=simplifier)

- BRC state: all agents WORKING (refine phase). Zero CONSENSUS_PROPOSE in
  transcript (live=true → real absence, not #3076). Refiner heartbeat fresh
  (~18:15:08Z).
- Refiner analysis draft `.egg-state/drafts/issue-3364-analysis.md`: ABSENT
  locally AND on `origin/egg/issue-3364/work` (checked both `issue-3364-` and
  `3364-` prefixes after fresh fetch).
- Verdict: **cannot propose yet — blocked on refiner's CONSENSUS_PROPOSE.**
  Did not fabricate analysis-human from raw task_description.
- Action: HEARTBEAT WAITING_ON_ROLE(refiner); exited without proposing.
- Escalation rule: raise `mcp__progress__overseer_alert`
  (anomaly=agent-heartbeat-stall, re: refiner) ONLY if a future premature
  propose finds the refiner's newest heartbeat >15 min stale with no
  CONSENSUS_PROPOSE. Refiner last beat 18:15:08Z → threshold ~18:30Z.
- Latest identical premature-propose event: **#4 at 18:20Z** (update THIS
  line in place for identical repeats; do not add new sections — keeps file
  compact per #3393 lesson). All of #2/#3/#4: refiner WORKING, 0
  CONSENSUS_PROPOSE (live=true), draft absent, refiner heartbeat fresh
  (#4: 18:19:44Z) → no escalation. Pump cadence ~1-2 min while refiner drafts.
- RESEED CONFIRMED at #4 (18:20Z): worktree reseeded to f139716c4, memory
  file orphaned. Recovered via `git checkout e2fbdd712 -- <path>` (orphan
  still reachable). Each reseed drops local commits — heartbeat body is the
  reliable durable channel. Recovery orphan SHAs: e2fbdd712 (#2), 0661c22b8 (#1).

### 2026-07-06 18:22Z — event #5: REFINER PROPOSED v1 → I rendered analysis-human & proposed

- Refiner CONSENSUS_PROPOSE **version=1, commit d032e6edf** (18:21:02Z).
- **KEY**: refiner used bare-number naming `3364-analysis.md` (NOT
  `issue-3364-analysis.md`). Identifier for THIS pipeline resolves to `3364`.
  ⟹ my artifact is `.egg-state/drafts/3364-analysis-human.md` (match bare
  number). Read refiner draft via `git show d032e6edf:.egg-state/drafts/3364-analysis.md`.
- Memory was GONE again at #5 (reseed); recovered from f548c39ff.
- Refiner analysis content (faithful summary I rendered): 3 PRs B/C/D
  independent. B=wait-status --exclude-types/--quiet + slice.closed event
  (record_complete/record_failure + _STATUS_WAIT_EVENT_TYPES). C=throttle
  (429/rate-limit/overloaded) classification distinct from abnormal + windowed
  paced retry preserving landed slices + deterministic-loop guard. D=delete 5
  host detector blocks + Overseer-Absent Fallback + #1962 section from SKILL.md,
  remove overseer_owns_host_detection entirely; GATED on coverage-map.
  7 AC groups (AC-B1..5, AC-C1..7, AC-D1..6). **cq-1**: PR C retry ceiling
  (operator decision, registered by refiner). §5 gate: if a block lacks
  overseer parity → HITL at point of discovery (NOT pre-registered).
- REFINER'S NEW FINDING (load-bearing, keep faithful): the naive "overseer
  already does this" is FALSE for all 5 blocks — the host vocab
  (agent-stall/silent/nack-unresolved/phase-long-running) exists in prod only
  as a dedup signature map in shared/egg_overseer/state.py; live overseer emits
  a DIFFERENT deterministic set (post_consensus_stall, rerun_anomaly, etc.);
  agent-level classification runs through Haiku classifier (LLM, not
  deterministic); `run_migrated_detectors` exists in NO prod file (only
  SKILL.md prose). AC-D3: preserve render-on-OVERSEER_ALERT paths.
- Wrote `3364-analysis-human.md` (plain-English operator rendering, faithful,
  nothing added/dropped). Proposed as my v1 (push=true).
- OBLIGATION next: (a) if reviewer_refine NACKs on faithfulness/jargon, fix &
  re-propose same version-bump; (b) if refiner re-proposes v2, re-check my
  render's faithfulness AND my file's integrity (clobber-watch from #3393);
  (c) my reviewer_phase edge (simplifier->refiner) — ACK/NACK refiner v1
  d032e6edf if a review event arrives, reading version from transcript.

### 2026-07-06 18:41Z — event #6: REFINE COMPLETE → PLAN phase (blocked on task_planner)

- REFINE CONVERGED: my analysis-human v1 (b949071af) ACKed by reviewer_refine
  (18:26:05Z, no blocking concerns; noted only benign omission of deferred-item
  refs #3249/#3369 — planning bookkeeping, non-misleading). Refiner v1 ACKed by
  all 3 reviewers (reviewer_refine, first_principles_reviewer,
  reviewer_agent_design). All CONSENSUS_CONFIRMED by 18:26:08Z. My
  simplifier->refiner reviewer edge closed by the phase transition — never
  exercised (moot).
- NOW PLAN PHASE (current_phase=plan). Producers: architect, task_planner,
  risk_analyst, simplifier. Reviewer: reviewer_plan. simplifier also has a
  reviewer_phase.
- My plan producer artifact (artifact_spec.py:160-170): `plan-draft-human` at
  `.egg-state/drafts/3364-plan-human.md` — faithful jargon-free rendering of
  **task_planner's** `.egg-state/drafts/3364-plan.md`, produced AFTER
  task_planner CONSENSUS_PROPOSE. Gated by reviewer_plan (existence-only at
  propose). Bare-number identifier `3364` (same as refine).
- Event #6 state: 0 plan CONSENSUS_PROPOSE (live=true); task_planner WORKING
  (started ~18:41:03Z, fresh). 3364-plan.md ABSENT locally. **Blocked on
  task_planner** — same pattern as refine-blocked-on-refiner. Did NOT fabricate.
- Action: HEARTBEAT WAITING_ON_ROLE(task_planner); exited without proposing.
- Escalation: plans take LONGER than analyses — expect a longer quiet stretch.
  Alert (overseer_alert agent-heartbeat-stall re task_planner) only if a future
  premature plan-propose finds task_planner's newest heartbeat >15 min stale
  with no CONSENSUS_PROPOSE.
- Latest identical premature plan-propose event: **#12 at 18:53Z** (update THIS
  line in place for identical repeats; no new sections). #7–#12: task_planner
  WORKING (hb newest 18:51:11Z, ~2min old at #12), 0 task_planner
  CONSENSUS_PROPOSE → STILL BLOCKED on the propose signal. plan.md unchanged at
  60cb40147. No escalation (task_planner live; would trip only if hb >15min
  stale ~19:06Z). #12: memory SURVIVED (no reseed; commit 8e06e193d held).
  Reseed recovery orphans: 176f07b26 (#6), 8639852ed (#8), 9c3280d95 (#9),
  b7f46a0c6 (#10).
- **#10 NEW SIGNAL (18:49Z)**: `3364-plan.md` now EXISTS on disk, committed by
  task_planner at **60cb40147** ("task_planner: canonical plan draft 3364-plan.md
  (PRs B/C/D)"), in current HEAD lineage (worktree reseeded to 4ebf98a3e), clean/
  tracked, 259 lines, header "# Issue #3364 — Implementation Plan". BUT
  task_planner has NOT sent CONSENSUS_PROPOSE and is still WORKING. DECISION:
  **still wait for the propose signal** — (a) artifact_spec dependency is "after
  task_planner PROPOSES", (b) the propose carries the version I must track/ACK,
  (c) task_planner may amend plan.md before proposing (rendering the 60cb40147
  draft now risks a faithfulness NACK if it changes). Did NOT render early.
  When the propose lands: read plan.md at the PROPOSAL commit_sha (not 60cb40147
  unless they match), render 3364-plan-human.md, commit+propose.

### 2026-07-06 18:47Z — event #9: ARCHITECT proposed (CONTEXT only) + cq-1 resolution captured

- **architect** CONSENSUS_PROPOSE v1, commit a7ea0133b (18:47:21Z), artifacts
  3364-architect-output.json + 3364-architect-slices.yaml. NOT my render source
  (task_planner's 3364-plan.md is) — captured as context for my future plan-human.
  Architect scaffold = 3 mutually-independent slices, no cross-block edges:
  - Slice 1 / PR B (low): --exclude-types/--quiet at wait-status json print
    sites; new SLICE_CLOSED EventType (events.py:35) via injected emitter callback
    in SliceScheduler.record_complete/record_failure (mirrors _hitl_escalator
    seam); add to _STATUS_WAIT_EVENT_TYPES.
  - Slice 2 / PR C (med-high): transient rate-limit classification off
    all-producers throttling-streak bypassing 30s fail-streak halt; hours-scale
    paced retry via restart_phase-equiv preserving landed slices; deterministic
    loop guard escalates on identical-failure repro; EX_AUTH_FATAL/abnormal
    unchanged.
  - Slice 3 / PR D (med, GATED, split D1 coverage-map → D2 SKILL.md deletion +
    D3 flag removal): coverage-map before delete; preserve AC-D3 render-on-alert;
    remove overseer_owns_host_detection entirely (grep-clean); HITL any block
    with no confirmed emitter.
- **cq-1 RESOLVED (human, 18:30:26Z) = opt-3** (verified via check_hitl_answers):
  "Retry until the cap lifts (NO hard ceiling) BUT emit an OVERSEER_ALERT once
  the wait crosses a threshold, so an attended operator is informed while
  auto-recovery continues." BINDING — render faithfully in plan-human (PR C).
  Refine phase-gate decision-2 = operator approved continue, carrying cq-1 fwd.
- Still blocked on task_planner; heartbeat WAITING_ON_ROLE(task_planner); exited.
- MEMORY SURVIVED the refine→plan transition (my push b949071af carried it).
- CLOBBER-WATCH now shifts to task_planner potentially overwriting my
  3364-plan-human.md; verify my file integrity once produced.

## Durability notes (carried from issue-3393 run)

- Only PUSHED commits survive worktree reseeds; local commits get orphaned.
  I can only push via `mcp__brc__propose` (not applicable while blocked), so
  this memory file may not survive the next reseed. Mitigation: put the
  current blocker in every HEARTBEAT body (orchestrator message store survives
  reseeds; readable via read_peer_artifact(HEARTBEAT, peer_role=simplifier)).
- Watch for refiner CLOBBERING my analysis-human file on their re-proposes
  (happened twice in #3393). Verify my file's integrity at every event once I
  have produced it.

## Next invocation checklist (PLAN phase)

1. Re-check BRC plan transcript for **task_planner** CONSENSUS_PROPOSE (note
   version for staleness/ACK).
2. If task_planner has proposed: pull their `.egg-state/drafts/3364-plan.md`
   (via `proposal_commit_sha`, or `git show <sha>:...`), read it + the full
   contract task_description + the refine analysis for context, then write
   `.egg-state/drafts/3364-plan-human.md` (faithful, jargon-free, nothing
   added/dropped — plain-English operator rendering of the plan: the slices,
   their order/dependencies, verification, what lands), commit, and
   `mcp__brc__propose` (push=true) with artifacts=[that path] + >=50-char
   summary. Load-bearing context to carry: 3 independent slices B/C/D; cq-1
   (PR C retry ceiling) operator decision; PR D coverage-map gate BEFORE any
   SKILL.md deletion; AC-D3 render-on-alert preservation.
3. If still no task_planner proposal: re-heartbeat WAITING_ON_ROLE(task_planner),
   update the "latest identical" line above, apply the >15-min escalation rule
   (plans run longer — be patient).
4. Watch also for risk_analyst / architect proposes as CONTEXT (not my source;
   task_planner's plan is my source of truth) — but their outputs may inform
   the plan I render.
5. Reviewer duty: this role also has a reviewer_phase — if an event asks me to
   review a peer proposal, read the CONSENSUS_PROPOSE version from the
   transcript and ACK/NACK with that exact version.
6. Verify my `3364-plan-human.md` integrity at every event once produced
   (task_planner clobber-watch).
