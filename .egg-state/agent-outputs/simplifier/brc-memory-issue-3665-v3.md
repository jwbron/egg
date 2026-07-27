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
  summary (detection plane unwired, only 3/12 snapshot fields populated,
  `_run_overseer_detection_plane()` has zero call sites), 9 already-landed items
  verified, 4 areas of proposed work with ordering, ranked candidate list of 30 items
  across 5 tiers.
- Wrote `.egg-state/drafts/issue-3665-v3-analysis-human.md` (plain-English operator
  rendering, faithful, nothing added/dropped). Committed as 61b157459.
- Proposed via BRC with artifacts=[".egg-state/drafts/issue-3665-v3-analysis-human.md"].
- OBLIGATION next: (a) if reviewer_refine NACKs on faithfulness/jargon, fix & re-propose
  same version-bump; (b) if refiner re-proposes v2, re-check my render's faithfulness
  AND my file's integrity (clobber-watch from #3393 lesson); (c) my reviewer_phase edge
  (simplifier->refiner) — ACK/NACK refiner v1 37b8944d if a review event arrives, reading
  version from transcript.

## Durability notes

- Only PUSHED commits survive worktree reseeds; local commits get orphaned.
  I can only push via `mcp__brc__propose` (not applicable while blocked), so
  this memory file may not survive the next reseed. Mitigation: put the
  current blocker in every HEARTBEAT body.
- Watch for refiner CLOBBERING my analysis-human file on their re-proposes
  (happened in #3393). Verify my file's integrity at every event.
