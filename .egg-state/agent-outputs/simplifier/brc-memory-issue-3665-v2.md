# simplifier — durable BRC memory — issue-3665-v2 (refine phase)

## Role contract (verified against shared/egg_contracts/artifact_spec.py:141-147)

- Producer artifact (refine): `analysis-draft-human` at
  `.egg-state/drafts/issue-3665-v2-analysis-human.md` — a faithful, jargon-free
  rendering of the refiner's `analysis-draft`
  (`.egg-state/drafts/issue-3665-v2-analysis.md`). Gated by `reviewer_refine` on
  "faithful + jargon-free". Existence-only check at propose time.
- Identifier is `issue-3665-v2` (pipeline_id has a qualifier beyond bare
  `issue-<N>`, so `_pipeline_identifier` keys by pipeline_id, not issue number).
- Dependency: I propose AFTER the refiner's CONSENSUS_PROPOSE. Do NOT fabricate
  the analysis-human from the raw task_description — faithfulness to the refiner's
  analysis (the source of truth) is the gated property.
- gh CLI is DENIED for simplifier role — use contract task_description +
  refiner analysis + MCP tools, never `gh issue view`.

## Event log

### 2026-07-27 ~06:02 UTC — event #1, action=propose (producer=simplifier)

- BRC state: refiner PROPOSED v1 (commit bf91f0843, 06:01:57Z). All other agents
  WORKING. Zero CONSENSUS_PROPOSE from simplifier (live=true → real absence).
- Refiner analysis draft `.egg-state/drafts/issue-3665-v2-analysis.md`:
  PRESENT (committed by refiner at bf91f0843). Read in full.
- Refiner proposal `.egg-state/agent-outputs/refiner/refiner-proposal-issue-3665.md`:
  PRESENT. Read in full.
- BRC memory `.egg-state/agent-outputs/refiner/brc-memory-issue-3665-v2.md`:
  PRESENT. Read in full.
- Verdict: refiner has proposed → I can now render analysis-draft-human.

### Verification of refiner's claims (checked against live tree)

All key claims verified:

1. **`snapshot_from_health_context` does not populate `last_tool_call_age_s` /
   `last_heartbeat_age_s`** — CONFIRMED. `detection_plane.py:534-538` creates
   `RunningAgent` entries from `context.live_container_ids` (container ID strings),
   setting only `role`, `state="running"`, `lifecycle_owner`. The age fields
   default to `None` → `detect_heartbeat_stall` (line 242) always skips.

2. **`detect_heartbeat_stall` is not registered in the detection plane** —
   CONFIRMED. Defined at `consensus_stall.py:217` but NOT imported or registered
   in `detection_plane.py`. The `coverage_gap_detectors` tuple (line 467-493)
   does not include it.

3. **`_check_convergence_stall` does not consult `WAITING_ON_ROLE`** — CONFIRMED.
   `event_loop/_loop.py:836-957` only consults `bus_timestamp` (line 864) and
   `_live_keys` (line 905). No grep match for `WAITING_ON_ROLE` or
   `_is_brc_idle` or `_orchestrator_skip_tripwire` in `_loop.py`.

4. **Timeout exit code -1 maps to `JOB_OUTCOME_ABNORMAL`** — CONFIRMED.
   `kubernetes_spawner/_models.py:80` (`outcome_for`) returns `self._ABNORMAL`
   for any exit code that doesn't match `EX_AUTH_FATAL` or `EX_RATE_LIMITED`.
   No `JOB_OUTCOME_TIMEOUT` constant exists in `event_loop/__init__.py:172-177`.

5. **2-hour timeout is invisible to the agent** — CONFIRMED.
   `shared/egg_agent/__main__.py:47` (default=7200) and
   `shared/egg_agent/client.py:765` (`asyncio.timeout(7200)`) — no pre-warning
   heartbeat emitted.

### 2026-07-27 ~06:03 UTC — event #2, action=propose (producer=simplifier)

- Wrote `.egg-state/drafts/issue-3665-v2-analysis-human.md`: faithful,
  jargon-free, plain-English rendering of the refiner's analysis. Covers:
  - Problem statement (7 silent loops, false-positive alerts, invisible timeouts)
  - Five "is this role stuck?" states
  - Four problem areas (unconsulted signals, session boundaries, undetected loops,
    unactionable alerts) with verified file-and-symbol citations
  - Nine "already landed" items (verified present)
  - Four proposed priorities
  - What was left out (and why)
- Committed at 8e474c354 (push=true via mcp__brc__propose).
- Proposed as simplifier v1 (push=true). Reviewers: reviewer_refine.
- Status: PROPOSED. Waiting for reviewer_refine ACK/NACK.

## Next invocation checklist

1. If reviewer_refine NACKs on faithfulness/jargon: fix & re-propose (version bump).
2. If refiner re-proposes v2: re-check my render's faithfulness AND my file's
   integrity (clobber-watch from #3393).
3. My reviewer_phase edge (simplifier->refiner): ACK/NACK refiner v1
   bf91f0843 if a review event arrives, reading version from transcript.
4. Watch for phase transition to `plan` — at that point, wait for
   task_planner's CONSENSUS_PROPOSE before rendering plan-human.

## Durability notes

- Only PUSHED commits survive worktree reseeds; local commits get orphaned.
- I can only push via `mcp__brc__propose` — which I did (push=true, commit
  8e474c354 pushed to origin).
- Memory file committed in same invocation as the artifact.
