## Task Analysis

**Problem statement**: After the implement phase's post-consensus push + PR creation succeeds, the orchestrator's pipeline record does not reflect that fact: `pipeline.pr_number`, `pipeline.pr_head_sha`, every `completed_agents[*].commit`, and `pipeline.current_phase` all stay stale. The overseer's `post-consensus-push-stall` detector reads that stale state and fires false-positive `[high]` alerts, sending /sdlc operators on false rescue missions.

**Source context**: Reported from a /sdlc run on #1901 (PR #1910 opened successfully). The reporter documented three distinct stale fields co-occurring with a false-positive overseer alert and proposed acceptance criteria. They flagged the three-role implement phase (coder/tester/documenter) vs the legacy `implementer` role as one plausible wiring gap.

**Workarounds**: The /sdlc flow fell back to `gh pr list` / `git log` as ground truth. No workaround in the orchestrator itself.

**System context**: After implement-phase BRC consensus completes, `_run_pipeline` in `orchestrator/routes/pipelines.py`:
- Calls `_update_agents_complete()` (line 7886) which marks agents COMPLETE and copies commit SHAs from the BRC tracker into `agent.commit` (lines 7913–7916).
- Exits the consensus poll, marks the implement `phase_execution.status = COMPLETE`, and advances `pipeline.current_phase` to the next phase at line 11107.
- When `current_phase == "pr"`, the block at line 10122+ pushes the branch, then calls `_finalize_pr_phase_failed` (lines 10333 → 4985). On success it stores `phase_execution.artifacts["pr_url"]` only (line 5023) — nothing else on the pipeline record.
- Consumers of `get_status` get `pr_number` via `_get_pr_info` (line 2242), which parses it from that artifact on read. `pipeline.pr_number` and `pipeline.pr_head_sha` are written only at pipeline *creation* for babysit mode (`state_store.py:872–875`).
- The overseer detector `_check_post_consensus_stall` (`overseer/monitor.py:1065`) only checks `consensus.is_complete` and `pipeline_status_str == "running"`. It doesn't verify whether the PR phase actually ran or whether the PR was actually created, so it fires during any normal implement→pr→complete transition that takes more than `poll_interval * 3` seconds.

**Technical root causes** (three distinct bugs that share symptoms):

1. **No writeback of `pr_number` / `pr_head_sha` after auto-PR creation.** `_finalize_pr_phase_failed` at `orchestrator/routes/pipelines.py:5017–5024` persists only `phase_execution.artifacts["pr_url"]`. The model fields `pipeline.pr_number` and `pipeline.pr_head_sha` (`orchestrator/models.py:544, 549`) were designed for *babysit-mode* where a PR exists at pipeline creation — the auto-PR (issue-mode) codepath never populates them. Any downstream code that reads `pipeline.pr_number` directly (e.g., `get_pipeline_snapshot`, babysit-worker handoffs, the overseer) sees null.

2. **Overseer `post-consensus-push-stall` detector is underspecified.** `overseer/monitor.py:1065–1130` fires on `(consensus.is_complete AND pipeline.status == "running")` after a 3-poll grace period. It never consults `pipeline.current_phase`, `phases["pr"].artifacts["pr_url"]`, or `pipeline.pr_number`. The detector is designed to catch one specific failure — consensus complete but no phase transition — but its check is too loose, so any normal transition that takes >90s looks identical to a stall.

3. **`completed_agents[*].commit` can stay null on the three-role implement phase.** The population at lines 7913–7916 relies on `_brc.get_proposal_commit_sha(agent.role.value)` returning a real SHA. If the BRC tracker doesn't record a SHA for a role (the `RECONSTRUCTED_NO_SHA` sentinel or `None`), `agent.commit` stays null silently. The reporter flags the three-role split (coder/tester/documenter) as a plausible wiring gap vs. the legacy single-`implementer` role.

**Risks / edge cases**:
- `_fetch_pr_state` uses `gh pr view` and may fail if `gh` is unavailable or the PR was just created (propagation delay). Treat `head_sha` as best-effort: always populate `pr_number` (from parsed URL), populate `pr_head_sha` only when the gh lookup returned a valid hex SHA. Don't fail the PR phase if `_fetch_pr_state` returns empty.
- The overseer short-circuits must be strictly additive — if `pipeline.pr_number` is null AND no `pr_url` artifact exists AND `current_phase == "implement"`, the detector still fires. A real orchestrator stall won't populate any of those.
- The `agent.commit` logging change is diagnostic-only. Deliberately no auto-fallback — silencing the null with a guess would mask the real bug in the BRC tracker wiring.
- The push-failure fallback-to-remote-HEAD path (#1731) means the PR can be against a different SHA than we pushed. Reading `head_sha` from the created PR (not from our push intent) is correct for that case.
- `pipeline.pr_number` validator is `ge=1`; `pipeline.pr_head_sha` validator is `[0-9a-f]{7,40}`. Must guard against unusual URLs and short/empty SHAs.