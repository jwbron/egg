# simplifier — durable BRC memory — issue-3665-v3 (refine phase)

## Role contract (verified against shared/egg_contracts/artifact_spec.py:142-147)

- Producer artifact (refine): `analysis-draft-human` at
  `.egg-state/drafts/issue-3665-v3-analysis-human.md` — a faithful, jargon-free
  rendering of the refiner's `analysis-draft`
  (`.egg-state/drafts/issue-3665-v3-analysis.md`). Gated by `reviewer_refine` on
  "faithful + jargon-free". Existence-only check at propose time.
- Identifier is `issue-3665-v3` (== pipeline_id); refiner draft path confirmed
  present under `issue-3665-v3-analysis.md`.
- Dependency: I propose AFTER the refiner's CONSENSUS_PROPOSE. Do NOT
  fabricate the analysis-human from the raw task_description — faithfulness to
  the refiner's analysis (the source of truth) is the gated property.
- gh CLI is DENIED for simplifier role — use contract task_description +
  refiner analysis + MCP tools, never `gh issue view`.

## Event log

### 2026-07-27 ~21:13 UTC — event #1, action=propose (producer=simplifier)

- BRC state: refiner WORKING (heartbeats fresh from 20:13:40Z onward). Zero
  CONSENSUS_PROPOSE from anyone initially (transcript live=true, only HEARTBEATs).
- Refiner draft `.egg-state/drafts/issue-3665-v3-analysis.md`: ABSENT locally
  at first check, but present on disk after refiner commit (17,164 bytes).
- Verdict: **cannot propose yet — blocked on refiner's CONSENSUS_PROPOSE.**
  Did not fabricate analysis-human from raw task_description.
- Action: HEARTBEAT WAITING_ON_ROLE(refiner); exited without proposing.
- Escalation rule: raise `mcp__progress__overseer_alert`
  (anomaly=agent-heartbeat-stall, re: refiner) ONLY if a future premature
  propose finds the refiner's newest heartbeat >15 min stale with no
  CONSENSUS_PROPOSE.

### 2026-07-27 21:13 UTC — event #2: REFINER PROPOSED v1 → I rendered analysis-human & proposed

- Refiner CONSENSUS_PROPOSE **version=1, commit 37b8944d** (21:12:58Z).
- Refiner analysis draft read at proposal commit 37b8944d. Content: executive
  summary (detection plane unwired, 5/13 snapshot fields populated,
  `_run_overseer_detection_plane()` has zero call sites), 9 already-landed items
  verified, 4 areas of proposed work with ordering, ranked candidate list of 30 items
  across 5 tiers.
- Wrote `.egg-state/drafts/issue-3665-v3-analysis-human.md` (plain-English operator
  rendering, faithful, nothing added/dropped). Committed as 61b157459.
- Proposed via BRC v1 with artifacts=[".egg-state/drafts/issue-3665-v3-analysis-human.md"].
- ACKed refiner v1 (37b8944d) on the simplifier→refiner review edge.
- OBLIGATION next: (a) if reviewer_refine NACKs on faithfulness/jargon, fix & re-propose
  same version-bump; (b) if refiner re-proposes v2, re-check my render's faithfulness
  AND my file's integrity (clobber-watch from #3393 lesson); (c) my reviewer_phase edge
  (simplifier->refiner) — ACK/NACK refiner v1 37b8944d if a review event arrives, reading
  version from transcript.

### 2026-07-27 21:52 UTC — event #3: iteration-0 feedback → corrected & re-proposed v2

- Operator feedback (iteration_n=0) delivered 4 corrections, all ACKed by 5 reviewers
  with zero NACKs — treated as verification failure, not writing failure.
- **Correction 1 (field count):** Changed "3 of 12 fields" → "5 of 13 fields" everywhere.
  Verified against `EventStreamSnapshot` class in `orchestrator/health_checks/detection_plane.py:106`
  (13 fields) and `snapshot_from_health_context()` (lines 511-549) which populates 5:
  snapshot_id, pipeline_id, phase, running_agents, phase_state.
- **Correction 2 (candidate #24):** Line 633 is 429 retry-after backoff, NOT heartbeat cadence.
  Re-anchored to `cmd_message_heartbeat` at line 588 (actual heartbeat handler).
- **Correction 3 (line anchors):** `noop_park_report()` at line 610 (not 584);
  `_classify_exit()` at line 1148 (consistent in both candidates #9 and #14).
- **Correction 4 (verification method):** Changed "verified via git log" to
  "verified via file-and-symbol citations" — per-item citations are the real evidence.
- Committed as fab4bd795. Re-proposed via BRC.
- BRC state: reviewer_refine now in REVIEWING (was WORKING); first_principles_reviewer
  also in REVIEWING. Waiting on reviewer_refine to ACK my corrected proposal.

## Durability notes

- Only PUSHED commits survive worktree reseeds; local commits get orphaned.
  I can only push via `mcp__brc__propose` (not applicable while blocked), so
  this memory file may not survive the next reseed. Mitigation: put the
  current blocker in every HEARTBEAT body.
- Watch for refiner CLOBBERING my analysis-human file on their re-proposes
  (happened in #3393). Verify my file's integrity at every event.
