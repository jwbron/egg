# Issue #3665 — Implementation Plan: Operator Summary (v3)

## ⚠️ Critical finding: work already exists on another branch

**Commit `68b185ca` (current tip of the `issue-3665-supervision-gaps` branch)
already implements three of the four supervision fixes** — 17 files, 1072
insertions. The plan integrates this commit, with corrections to the livelock
detector per operator feedback (cq-1, cq-3), and adds the fourth priority
(alert evidence bundling) that was missing from the fix commit.

The fix commit is NOT an ancestor of the current HEAD — it has not been
merged. The task is to review, verify, and integrate it with corrections.

### What the fix commit does (verified at 68b185ca)

1. **Agent livelock detection** — Creates
   `orchestrator/health_checks/tier1/loop_detection.py` (317 lines) with
   `detect_agent_livelock` function and `AgentLivelockCheck` class.
   **⚠️ Needs correction per cq-1:** The commit sources from `agent_log_store`
   (pod stdout) and truncates signatures to 80 chars (`args[:80]`). The issue
   explicitly says the pod log cannot support this signal. The detector must
   read the live session transcript at `$HOME/.claude/projects/<cwd>/<session>.jsonl`
   inside the running pod, and key on the FULL untruncated `(tool_name, input)`
   pair.
   **⚠️ Needs correction per cq-3:** The commit defaults to nudge recovery.
   The operator resolved cq-3 as respawn: a two-step process (1) post a
   terminating message to the bus, then (2) respawn with a fresh session.
   The detector must escalate to HITL with the looping input quoted verbatim.
   **⚠️ Needs correction per issue:** The commit uses a ratio metric
   (`unique_ratio = len(unique_signatures) / len(signatures)`, fires at
   `ratio < 0.1`). The issue specifies counting "inputs never issued before
   in the session" over a trailing window, firing at zero novelty.

2. **Two-hour timeout visibility** — Adds `agent_timeout_seconds` to
   `PipelineConfig` (default 7200). Passes `EGG_AGENT_TIMEOUT_SECONDS` env to
   the sandbox in `concurrent_executor.py`. Passes `active_deadline_seconds`
   to the K8s Job in `kubernetes_spawner/_spawn.py`. Classifies exit code 143
   (SIGTERM) as `JOB_OUTCOME_LEGITIMATE` (the existing constant at
   `event_loop/__init__.py:174`), NOT a new `JOB_OUTCOME_TIMEOUT` constant.
   This is simpler — reuses the existing outcome category. Verified:
   `record_legitimate_outcome` does NOT increment the failure streak
   (`_supervisor.py:127-139`).

3. **False convergence-stall suppression** — Adds
   `_has_recent_agent_activity()` to the event loop's convergence-stall check
   (`_loop.py:976`), which queries `HealthMonitor.get_agent_activity_ages()`.
   Enriches `snapshot_from_health_context` in `detection_plane.py` to
   populate `last_tool_call_age_s` and `last_heartbeat_age_s` on
   `RunningAgent` entries, enabling the existing `detect_heartbeat_stall`
   detector to fire in the live path.

4. **Tests** — Creates `test_loop_detection.py`, `test_timeout_sigterm.py`,
   `test_convergence_stall_suppression.py`, and `test_agent_timeout_config.py`.

## What this plan does

The plan proposes four priorities. Three are already implemented on the
`issue-3665-supervision-gaps` branch (commit `68b185ca`) and need integration
with corrections. The fourth (alert evidence bundling) is NOT in the fix
commit and must be built.

1. **Agent livelock/repetition-loop detection** — Already implemented but
   needs corrections per cq-1 (live session transcript, no truncation,
   novelty metric) and cq-3 (HITL escalation, not nudge).
2. **Two-hour timeout visibility** — Already implemented and correct.
   Integrates as-is.
3. **False convergence-stall suppression** — Already implemented and correct.
   Integrates as-is.
4. **Alert evidence bundling** — NOT in the fix commit. Must be built.
   Enriches OVERSEER_ALERT payloads with structured evidence.

## What's already implemented (verified)

All nine items in the issue's "already landed" list are present and verified:

1. Terminating-Job adoption (#3613) — `kubernetes_spawner/_events.py:110`
2. Worktree preservation (#3644+) — `kubernetes_spawner/_worktree.py`
3. Cancel stops driver (#3645+) — `event_loop/_loop.py:1011`
4. Phase-gate approvals (#3648) — `overseer/monitor/_anomaly_checks.py:126`
5. Never-heartbeated roles anchor at Job start (#3612) — `health_monitor.py:248-275`
6. Simplifier's first propose gated on upstream (#3607) — `event_loop/_loop.py:683`
7. Green gate defaults to on (#3609) — `models/_config.py:191`
8. Decoding config recorded (#3611, #3625) — `consensus_wrapper.py`
9. Re-reviews blocking-only (#3661) — `peer_consensus`

Additional items verified present in the tree:

- `JOB_OUTCOME_LEGITIMATE` constant exists at `event_loop/__init__.py:174` —
  the event loop already handles it in `_loop.py:92-95` via
  `supervisor.record_legitimate_outcome()`.
- `detect_heartbeat_stall` detector exists at
  `health_checks/tier1/consensus_stall.py:217` — but is NOT registered in the
  detection plane and its inputs are never populated (the fix commit addresses
  both).
- `agent_log_store` module exists (`orchestrator/agent_log_store.py`) —
  captures pod logs before reaping, with Redis-backed storage and 24h TTL.

## What the fix commit implements (verified at 68b185ca)

All items the plan proposed as "needs to be built" are ALREADY implemented on
the `issue-3665-supervision-gaps` branch:

- ✅ `get_agent_activity_ages()` method on HealthMonitor — implemented
- ✅ `_has_recent_agent_activity()` method on the event loop — implemented
  at `_loop.py:976`
- ✅ `agent_timeout_seconds` config field on PipelineConfig — implemented
- ✅ `EGG_AGENT_TIMEOUT_SECONDS` env var passed to the sandbox — implemented
  in `concurrent_executor.py:506-508`
- ✅ `active_deadline_seconds` made configurable — implemented in
  `kubernetes_spawner/_spawn.py`
- ✅ Exit code 143 (SIGTERM) classified as `JOB_OUTCOME_LEGITIMATE` —
  implemented in `_models.py:81-88` via `_failed_with_timeout_sigterm`
  (line 147). Uses the existing `JOB_OUTCOME_LEGITIMATE` constant, NOT a new
  `JOB_OUTCOME_TIMEOUT`.
- ✅ `AgentLivelockCheck` class — implemented in `loop_detection.py`
- ✅ `detect_agent_livelock` function — implemented in `loop_detection.py`
- ✅ `record_legitimate_outcome` does NOT increment failure streak — verified
  at `_supervisor.py:127-139`

## Corrections needed to the livelock detector (per cq-1, cq-3, and the issue)

The fix commit's livelock detector at `loop_detection.py` has three defects
that must be corrected during integration:

1. **Sources from `agent_log_store` (pod stdout)** — cq-1 says: "Do NOT parse
   the pod log. Read the live session transcript at
   `$HOME/.claude/projects/<cwd>/<session>.jsonl` inside the running pod."
   The fix commit reads from `agent_log_store` (line 132-134), which captures
   pod stdout at reap time — too late to intervene.

2. **Truncates signatures to 80 chars** — cq-1 says: "key on the FULL
   untruncated `(tool_name, input)` pair with no character limit." The fix
   commit uses `sig = f"{tool_name}:{args[:80]}"` (line 108), which stacks two
   lossy steps under the ~100-char pod log truncation.

3. **Uses ratio metric, not novelty** — The fix commit computes
   `unique_ratio = len(unique_signatures) / len(signatures)` and fires at
   `ratio < 0.1` (lines 205-211). The issue specifies counting "inputs never
   issued before in the session" over a trailing window, firing at zero
   novelty. An 8-cycle over a 30-call window scores novelty 0 and should fire
   immediately, but its distinctness ratio is 8/30 = 0.27 and does not fire
   until ~80 accumulated calls.

4. **Defaults to nudge recovery** — cq-3 says: "Respawn. Nudge alone is
   empirically falsified, twice." The fix commit's docstring says
   "can nudge the agent without an LLM call" (line 18-19) and
   `requires_adjudication=False` (line 231). The operator resolved cq-3 as a
   two-step process: (1) post a terminating message to the bus, then (2)
   respawn with a fresh session. The detector must escalate to HITL with the
   looping input quoted verbatim.

## The four task groups

### Task Group 1: Integrate Livelock Detector with Corrections (TASK-1-1)

**Source:** Commit `68b185ca` on `issue-3665-supervision-gaps` branch

Integrate the livelock detector from the fix commit, with corrections per
cq-1, cq-3, and the issue:

- Read the live session transcript at `$HOME/.claude/projects/<cwd>/<session>.jsonl`
  inside the running pod, NOT `agent_log_store` (pod stdout).
- Key on the FULL untruncated `(tool_name, input)` pair — no character limit.
- Implement novelty metric: count inputs never issued before IN THE SESSION
  over a trailing window, fire at zero. NOT a ratio.
- Recovery: escalate to HITL with the looping input quoted verbatim, then
  respawn with a fresh session. NOT nudge.
- `requires_adjudication=False` (deterministic detection), but the corrective
  action escalates to HITL.
- Register in `DetectionPlane.default()` and `cli.py`.

**Files:**
- `orchestrator/health_checks/tier1/loop_detection.py` (new, with corrections)
- `orchestrator/health_checks/detection_plane.py` (register detector)
- `orchestrator/health_checks/tier1/__init__.py` (export)
- `orchestrator/cli.py` (register check)

### Task Group 2: Integrate Two-Hour Timeout Visibility (TASK-1-2)

**Source:** Commit `68b185ca` — integrates as-is (no corrections needed)

- `agent_timeout_seconds` config field (default 7200, ge=60)
- `EGG_AGENT_TIMEOUT_SECONDS` env passed to sandbox
- `active_deadline_seconds` passed to K8s Job
- Exit 143 (SIGTERM) classified as `JOB_OUTCOME_LEGITIMATE` (not crash)
- `record_legitimate_outcome` does NOT increment failure streak (verified)

**Files:**
- `orchestrator/models/_config.py`
- `orchestrator/concurrent_executor.py`
- `orchestrator/kubernetes_spawner/_spawn.py`
- `orchestrator/kubernetes_spawner/_models.py`
- `orchestrator/kubernetes_monitor.py`
- `sandbox/llm/claude/config.py`

### Task Group 3: Integrate False Convergence-Stall Suppression (TASK-1-3)

**Source:** Commit `68b185ca` — integrates as-is (no corrections needed)

- `get_agent_activity_ages()` on HealthMonitor
- `_has_recent_agent_activity()` on the event loop
- `snapshot_from_health_context` enriched with `last_tool_call_age_s` /
  `last_heartbeat_age_s`
- `detect_heartbeat_stall` registered in `DetectionPlane.default()`

**Files:**
- `orchestrator/health_monitor.py`
- `orchestrator/event_loop/_loop.py`
- `orchestrator/health_checks/detection_plane.py`

### Task Group 4: Alert Evidence Bundling (TASK-1-4) — NOT in fix commit

**Priority 4 from the issue's "What to propose" section.** This was NOT
implemented in the fix commit and must be built.

Enrich OVERSEER_ALERT payloads with structured evidence so operators can act
without hand-investigation:
- `latest_heartbeat_age_s` — seconds since last heartbeat
- `latest_tool_call_age_s` — seconds since last tool call
- `last_progress_event` — the most recent progress event data
- `blocking_agents` — the BRC consensus blocking set
- `consensus_state` — the current BRC consensus matrix state

The overseer already fetches container logs separately at `_poll.py:78-85`,
so most of the data is in hand. This is what makes the other three fixes
usable: a livelock alert that does not carry the repeated input and the ages
is an alert an operator has to investigate by hand.

**Files:**
- `orchestrator/health_monitor.py` (enrich escalation dicts)
- `orchestrator/event_loop/_loop.py` (enrich convergence-stall anomaly payloads)
- `orchestrator/overseer/monitor/_alerting.py` (enrich OVERSEER_ALERT payloads)

### Task Group 5: Tests (TASK-1-5)

Verify test coverage from the fix commit and update tests for the corrected
livelock detector:
- `test_loop_detection.py` — update to test novelty metric (not ratio), live
  session transcript parsing (not agent_log_store), and HITL escalation
  (not nudge).
- `test_agent_timeout_config.py` — verify `agent_timeout_seconds` config.
- `test_convergence_stall_suppression.py` — verify activity-based suppression.
- `test_timeout_sigterm.py` — verify exit 143 classification.

**Files:**
- `orchestrator/tests/test_loop_detection.py`
- `orchestrator/tests/test_agent_timeout_config.py`
- `orchestrator/tests/test_convergence_stall_suppression.py`
- `orchestrator/tests/test_timeout_sigterm.py`

## Dependencies and ordering

- Task Groups 1, 2, 3 are independent — can be built in parallel.
- Task Group 4 (Alert Evidence) depends on Task Group 1 (needs livelock
  alert evidence to include the looping input).
- Task Group 5 (Tests) depends on Task Groups 1 and 2.
- Serialized chain order: Group 1 → Group 2 → Group 4 (Groups 1 and 2 can
  parallelize, Group 4 follows Group 1, Group 5 follows Groups 1+2).

## Open questions (HITL — resolved)

Three decisions are registered on the SDLC contract and have been resolved:

- **cq-1** (resolved): The livelock detector must NOT source from
  `agent_log_store` and must NOT truncate the signature. Read the live session
  transcript at `$HOME/.claude/projects/<cwd>/<session>.jsonl` inside the
  running pod, and key on the FULL untruncated `(tool_name, input)` pair.

- **cq-2** (resolved): Pipeline-level only. Ship the uniform 7200s default and
  make it configurable; do not build per-role overrides. The agent must be able
  to SEE the deadline. Keep the 4h K8s `active_deadline_seconds` as the outer
  safety net.

- **cq-3** (resolved): Recovery is a two-step process: (1) post a terminating
  message to the bus, then (2) respawn with a fresh session. The detector
  escalates to HITL with the looping input quoted verbatim, since it cannot
  know the answer to the agent's question. Nudge alone is falsified.

## What was left out (and why)

- **Per-agent timeout configuration** (candidate #2): The operator resolved
  cq-2 as pipeline-level only. Per-role overrides are a real follow-up but
  not in scope for this work.
- **Timeout warning emission** (candidate #3): The operator's cq-2 resolution
  notes the agent must be able to SEE the deadline. `EGG_AGENT_TIMEOUT_SECONDS`
  reaching the sandbox is necessary but not sufficient; the remaining budget
  must reach the agent's prompt or a tool it can call. This is a follow-up —
  the current work makes the timeout visible and non-fatal, which is the core
  ask.
- **Agent log retention policy** (candidate #7): The livelock detector now
  reads from the live session transcript, not `agent_log_store`, so the 24h
  TTL is no longer a concern for detection.
- **Convergence-stall suppression for reviewers** (candidate #9): The
  `_has_recent_agent_activity` check applies to all roles. Reviewers
  legitimately wait on producers; their activity pattern differs. This is a
  follow-up — the current work suppresses false alerts against busy agents,
  which is the core ask.
- **Two-hour timeout config validation** (candidate #10): The K8s
  `active_deadline_seconds` default (14400) remains as the outer safety net
  per cq-2 resolution. No validation needed.

## Files in the fix commit (68b185ca)

**New files:**
- `orchestrator/health_checks/tier1/loop_detection.py` (livelock detector, 317 lines)
- `orchestrator/tests/test_loop_detection.py`
- `orchestrator/tests/test_timeout_sigterm.py`
- `orchestrator/tests/test_convergence_stall_suppression.py`
- `orchestrator/tests/test_agent_timeout_config.py`

**Modified files:**
- `orchestrator/cli.py` (register health check)
- `orchestrator/concurrent_executor.py` (pass `EGG_AGENT_TIMEOUT_SECONDS` env)
- `orchestrator/event_loop/__init__.py` (no new constant — reuses `JOB_OUTCOME_LEGITIMATE`)
- `orchestrator/event_loop/_loop.py` (add `_has_recent_agent_activity` to convergence-stall)
- `orchestrator/health_checks/detection_plane.py` (register detector, enrich snapshot)
- `orchestrator/health_checks/tier1/__init__.py` (export new detector)
- `orchestrator/health_monitor.py` (add `get_agent_activity_ages`)
- `orchestrator/kubernetes_monitor.py` (update `_classify_exit` for exit 143)
- `orchestrator/kubernetes_spawner/_models.py` (add `_failed_with_timeout_sigterm`, classify 143 as LEGITIMATE)
- `orchestrator/kubernetes_spawner/_spawn.py` (pass `active_deadline_seconds`)
- `orchestrator/models/_config.py` (add `agent_timeout_seconds` field)
- `sandbox/llm/claude/config.py` (read `EGG_AGENT_TIMEOUT_SECONDS` env)
- `orchestrator/event_loop/_supervisor.py` (only 15 lines trimmed from docstring — no functional change)
