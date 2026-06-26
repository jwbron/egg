# refiner BRC memory — issue-2270-overhaul (refine)

## Pipeline identity (do NOT confuse with leftovers)
- THIS pipeline = **issue-2270** "Overseer overhaul (open season)". The sibling file
  `.egg-state/agent-outputs/refiner/brc-memory.md` is **STALE** (issue-3200) — ignore it.
- Worktree drafts referencing other issues are leftovers, not the task.

## Status
- v1 analysis written to `.egg-state/drafts/2270-analysis.md`, grounded against the tree 2026-06-26.
- HITL **cq-1** (architectural shape A/B/C; recommend C/B) and **cq-2** (scope: spine-first vs all-in-one; recommend spine-first) registered on the contract.
- Committed + proposed v1 via BRC (see decision log).

## Verdict / position
- **cq-1 (the central fork):** recommend **C** (hybrid: orchestrator-side deterministic
  detection + bounded corrective vocab; spawn a NORMAL on-demand agent only for adversarial
  escalation) leaning **B** (retire the standing pod) for the pod-shaped parts. Rationale:
  Tier-1 detectors ALREADY run orchestrator-side (`health_checks/tier1/`); #3064 killed the
  long-lived cohort; the respawn-every-90s pod + baked-in bootstrap are the root cause of
  §1's self-injection loop. Option A keeps the standing pod and bootstrap surface.
- **cq-2:** recommend **spine-first** (§1+§1.5+§2-calibration+§4-authority+§6-cleanup now;
  gate the broad §5 coverage-gap expansion on the calibrated corpus). §5 survey is where
  scope runs away.
- Calibration corpus (§2) is **deliverable #1** — tested known-normal/known-bad set.

## Grounded facts (verified 2026-06-26) — CONFIRMED unless noted
- §1: `models.py:726-728` default "sonnet"; bypasses `resolve_agent_model` via
  `classify_model()` at `kubernetes_spawner.py:2919`. CONFIRMED. Folds #2813.
- §1.5: `spawn_overseer_job` `kubernetes_spawner.py:2883-2960`; bespoke env
  `EGG_OVERSEER_MODE/_POLL_INTERVAL/_DECISION_MODEL` at `2922-2926`. `spawn_agent_job`
  at `1228`; `AgentRole.OVERSEER` already recognized (`672`). Baked-in
  `sandbox/overseer_monitor.py` (802L) invoked by prompt at `kubernetes_spawner.py:2931`.
- §2 reflection: `client.py:629+` → `midturn_messages.py:76` `_INJECT_FROM_ROLES` includes
  "overseer" — no distinction from operator HITL. CONFIRMED. (Saw it live this phase: a
  false [high] heartbeat-stall alert at 00:10:51 retracted at 00:12:56.)
- §2 branch-divergence: `routes/pipelines.py:15819` regex `\(#\d+\)`; detector `15822-15907`.
- §3: `_check_and_respawn_overseer` is at **`routes/pipelines.py:685-848`** (NOT the issue's
  `pipelines.py:433-596` — line drift).
- §4: **STALE** — `roles.py:can_modify` (`shared/egg_contracts/roles.py:147-170`) has NO
  overseer entry; overseer isn't a Role enum there. Real authority-denial point is elsewhere
  (gateway action-guards). Plan MUST locate it before designing §4.
- §5: Tier-1 detectors live orchestrator-side `health_checks/tier1/` (6 classes). No standalone
  Tier-2 `agent_inspector` (the agent IS Tier-2). `OverseerSelfMonitor` (`self_monitor.py`)
  is ALREADY recorded + health-checked (`monitor.py:111,1995`) — open nuance is whether
  `check_health()` emits alerts vs only logs.
- §6: **STALE** — `issue_filer.py` IS used (`overseer/__init__.py:27`, `monitor.py:36`, called
  `monitor.py:675`). Do NOT delete on the #1962 "unused" premise without re-confirming.
- Subsystem size: `orchestrator/overseer/` ~3,216L + `sandbox/overseer_monitor.py` 802L.

## If NACKed
- Edit `.egg-state/drafts/2270-analysis.md` in place, re-commit, re-propose (version bumps).
- Defend grounded file:line facts; the issue is heavily author-specified — don't invent scope.
- Keep the stale-claim flags (§4 roles.py, §6 issue_filer, §5 self_monitor) — they are
  verified and protect the plan phase from chasing ghosts.

## Decision log
- 2026-06-26: fresh slice; ignored stale 3200 memory; grounded #2270 claims (Explore sweep);
  wrote 2270-analysis.md; registered cq-1/cq-2; committed + proposed v1.
