# simplifier — durable BRC memory — issue-3393 (refine phase)

## Role contract (verified against shared/egg_contracts/artifact_spec.py)

- Producer artifact (refine): `analysis-draft-human` at
  `.egg-state/drafts/issue-3393-analysis-human.md` — a faithful, jargon-free
  rendering of the refiner's `analysis-draft`
  (`.egg-state/drafts/issue-3393-analysis.md`). Gated by `reviewer_refine` on
  "faithful + jargon-free". Existence-only check at propose time.
- Dependency: I propose AFTER the refiner's CONSENSUS_PROPOSE
  (coder→tester dependency pattern per agent_roles.py comment on SIMPLIFIER).

## Event log

### 2026-07-01 ~23:2x UTC — event action=propose (producer=simplifier)

- BRC state: all agents WORKING, no CONSENSUS_PROPOSE from anyone yet
  (transcript live=true, only HEARTBEATs).
- Refiner draft `.egg-state/drafts/issue-3393-analysis.md`: ABSENT locally,
  ABSENT on all 3393 remote branches (`origin/egg/issue-3393/work`,
  `origin/egg/multi-repo-pipelines-3393`,
  `origin/egg/multi-repo-slice-pr-targeting-3393`).
- Verdict: **cannot propose yet — blocked on refiner's proposal.** Did not
  fabricate an analysis-human from the raw task description; the artifact
  contract requires faithfulness to the refiner's analysis, which is the
  source of truth I must render.
- Action taken: emitted HEARTBEAT state=WAITING_ON_ROLE waiting_on=refiner;
  exited without proposing.

### 2026-07-01 ~23:3x UTC — event #2, action=propose (producer=simplifier)

- Re-verified: refiner still WORKING; transcript filtered on
  CONSENSUS_PROPOSE returns 0 items with live=true (real absence, not #3076
  structural emptiness). Draft still absent locally and on all three 3393
  remote branches after fresh `git fetch`.
- Verdict: **unchanged — still blocked on refiner's proposal.** Second
  premature propose event; treating as pump noise, not anomaly (refiner
  heartbeats are recent). If a THIRD premature propose arrives with the
  refiner silent for a long interval, consider
  `mcp__progress__overseer_alert` (anomaly=agent-heartbeat-stall, re:
  refiner) instead of silently re-waiting.
- Action taken: re-emitted HEARTBEAT WAITING_ON_ROLE(refiner) + blocked
  progress event; exited without proposing.

### 2026-07-01 23:29 UTC — event #3, action=propose (producer=simplifier)

- Still zero CONSENSUS_PROPOSE (live=true); no 3393 draft locally.
- Escalation check per event-#2 rule: refiner is NOT stalled — latest
  heartbeat 23:27:58Z (WORKING, in-tool-loop liveness), ~1 min before this
  event. This is pump re-spawn cadence, not an anomaly. No overseer alert.
- Verdict: unchanged — blocked on refiner proposal. Re-heartbeated
  WAITING_ON_ROLE(refiner); exited.
- Revised escalation rule: alert only if a future premature propose finds
  the refiner's newest heartbeat >15 min old with no CONSENSUS_PROPOSE.

### 2026-07-01 23:29:50 UTC — event #4, action=propose

- 25 s after event #3. Zero CONSENSUS_PROPOSE (live=true), no local draft.
  Refiner heartbeat ~2 min old at last check — inside the 15-min threshold.
  Verdict unchanged; re-heartbeated WAITING_ON_ROLE(refiner); exited.
- The pump is firing propose events at sub-minute cadence while upstream is
  unready; this is orchestrator behavior, not something I can fix from this
  role. Keeping responses minimal to avoid transcript noise.

### 2026-07-01 23:30:32 UTC — event #5, action=propose

- ~24 s after #4. Same state (0 proposals, live=true, no draft). Verdict
  unchanged; heartbeat re-sent; exited. Escalation threshold still: refiner
  heartbeat >15 min stale (would trip at ~23:43Z if refiner goes silent
  after its last observed beat 23:27:58Z).

### 2026-07-01 23:31:15 UTC — event #6, action=propose

- Same state. Verdict unchanged; heartbeat re-sent; exited.
- NOTE for future events: pump cadence is ~25 s. To keep this file compact,
  do NOT add a new section per identical event — update this line instead:
  latest identical premature-propose event: **#6 at 23:31:15Z**.

### 2026-07-01 ~23:36 UTC — event #8: reviewer_refine NACK v1 → fixed, re-proposed v2

- NACK reason: "two spots" must be "three spots" — third repos[0] collapse
  at orchestrator/routes/pipelines.py:732 (overseer_repo). VERIFIED live by
  grep: all three sites real (_spawn.py:452,464;
  commit_authorship_store.py:932-933; pipelines.py:732).
- Also verified: gateway-client method is `create_worktrees` (PLURAL,
  _worktree.py:13) — refiner's correction #1 was inverted, as the reviewer
  said. My human draft never names the method ("machinery that checks out
  code already accepts a list") so it needs no change for that.
- Fix applied: "two spots" → "three spots" + added plain-language sweep
  clause ("a sweep at implementation time will catch any stragglers") per
  reviewer's "keep the sweep language".
- Refiner v2 NOT yet proposed at fix time (transcript shows only v1).
  OBLIGATION: when refiner v2 lands, re-check my draft's faithfulness
  against it — expected v2 delta (three sites, un-inverted naming) is
  already consistent with my v2, but verify anything else that changes.
- Re-proposed as my v2.

### 2026-07-01 ~23:46 UTC — event #9: iteration-0 gate — HITL cq-1 resolved → v3

- Iteration-0 outcome: reviewer_refine ACKed my v2; refiner v2 (4bb71004b)
  ACKed by all three of its reviewers; my reviewer edge simplifier->refiner
  left "pending" in the frozen matrix (did not ACK v2 — iteration ended at
  the operator gate; expect a fresh review event next iteration).
- HITL cq-1 RESOLVED (Other): plain merge ordering AUTOMATED — dependent
  slice developed in parallel, PR held as draft, orchestrator auto-marks
  ready when upstream merges. HITL only for beyond-merge-state conditions
  (release/publish waits, version pinning) and genuine development blocks.
- Faithfulness recheck vs refiner v2 DISCHARGED: (a) three collapse sites —
  my draft already correct; (b) two-layer naming — my draft never names the
  method, no change; (c) NEW per-repo-conventions point (v2 design rec #5 /
  AC-7) — ADDED to my draft ("that repo's own house rules" bullet).
- cq-1 resolution rendered into hard-bit #1 (replaces "open decision"
  framing) directly from the operator's authoritative resolution text —
  robust even if refiner's v3 wording differs.
- No new decisions induced (remaining mechanics are planner-owned).
- NEW OBLIGATION: when refiner v3 lands (their cq-1 update), re-check my
  draft's faithfulness against it before/while ACKing my pending
  simplifier->refiner review edge.

### 2026-07-01 ~23:59 UTC — event #10: iteration-1 gate — operator ratifies 4 design rulings → iteration-2 proposal

- Operator directive (iteration 1): the four design recommendations are now
  BINDING operator decisions (same standing as cq-1): (1) lazy-per-repo
  work branch + context PR RATIFIED; (2) worktree keying → option (a)
  re-key by full owner/repo, option (b) reject-same-name RULED OUT
  (contradicts arbitrary-N); prohibitive fan-out ⇒ new HITL, never silent
  (b); (3) test-gate/reviewer-diff single-repo scoping RATIFIED; (4)
  naming/status/per-repo conventions RATIFIED.
- INCIDENT NOTE: refiner's v3 commit (c2a3a8e80) CLOBBERED my
  3393-analysis-human.md; their 63c824cfe restored it. Verified my worktree
  file == my e88c16d61 content (empty diff) before editing. Watch for
  clobbers whenever the refiner re-proposes.
- Faithfulness vs refiner 63c824cfe DISCHARGED: their cq-1 fold-in matches
  my hard-bit-1 rendering (both derive from the operator resolution text);
  no new content requiring summary changes beyond this directive.
- My edits: hard-bit #3 rewritten (re-key by owner/repo decided; rejection
  ruled out; prohibitive ⇒ new operator decision); added "Where decisions
  stand" section marking all five rulings binding. cq-1 paragraph and
  grounding/good-news sections left untouched per "no other changes".
- simplifier->refiner review edge STILL pending — refiner must fold the
  ratifications into the analysis (v4); review when their propose event
  reaches me.

## Next invocation checklist

1. Read `.egg-state/drafts/issue-3393-analysis.md` (pull the refiner's
   proposal commit if pending_reviews carries `proposal_commit_sha`; the
   transcript CONSENSUS_PROPOSE from refiner carries the version number —
   note it for staleness checks).
2. Re-read the full contract `task_description` via `mcp__sdlc__show_contract`
   (it is truncated in the event prompt) before structural judgments. Key
   binding directives already known: arbitrary N repos (no 2-repo special
   case), slice↔repo strictly 1:1 (cross-repo = multiple slices + deps),
   pipeline-wide visibility uniformity (all-private or all-public, reject
   mixed).
3. Write `.egg-state/drafts/issue-3393-analysis-human.md`: faithful,
   jargon-free, no content added or dropped; plain-language for an operator.
4. Commit, then `mcp__brc__propose` (push=true) with artifacts
   `[".egg-state/drafts/issue-3393-analysis-human.md"]` and a >=50-char
   summary.
5. Note: gh CLI is DENIED for simplifier role ("Unknown agent role") — use
   the contract task_description, the refiner's analysis, and MCP tools, not
   `gh issue view`.
6. Reviewer duty: this role also has a reviewer_phase in the matrix — if a
   later event asks me to review a peer proposal, read the CONSENSUS_PROPOSE
   version from the transcript and ACK/NACK with that exact version.
