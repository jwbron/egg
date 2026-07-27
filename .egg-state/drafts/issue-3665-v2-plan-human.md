# Issue #3665 — Implementation Plan: Operator Summary

## ⚠️ Critical finding: work already exists on another branch

**Commit `6ffe97c8e` on the `issue-3665-supervision-gaps` branch already
implements exactly what this plan proposes** — 17 files, 1072 insertions.
The plan's three task groups map 1:1 to this commit. The plan-human should
describe **integration and review** of that existing work, not from-scratch
implementation.

The fix commit is NOT an ancestor of the current HEAD — it has not been
merged. The task is to review, verify, and integrate it.

### What the fix commit does (verified against the tree at 6ffe97c8e)

1. **Agent livelock detection** — Creates
   `orchestrator/health_checks/tier1/loop_detection.py` (317 lines) with
   `detect_agent_livelock` function and `AgentLivelockCheck` class. Parses
   Claude Code tool-call lines from `agent_log_store` transcripts. Fires when
   unique tool-input ratio drops below 10% with ≥10 total calls.
   `requires_adjudication=False` (deterministic). Registered in the detection
   plane and health check runner.

2. **Two-hour timeout visibility** — Adds `agent_timeout_seconds` to
   `PipelineConfig` (default 7200). Passes `EGG_AGENT_TIMEOUT_SECONDS` env to
   the sandbox in `concurrent_executor.py`. Passes `active_deadline_seconds`
   to the K8s Job in `kubernetes_spawner/_spawn.py`. **Key difference from
   the plan:** the fix commit classifies exit code 143 (SIGTERM) as
   `JOB_OUTCOME_LEGITIMATE` (the existing constant at `event_loop/__init__.py:174`),
   NOT a new `JOB_OUTCOME_TIMEOUT` constant. This is simpler — reuses the
   existing outcome category rather than adding a new one.

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

The plan proposes three supervision fixes. **All three are already implemented
on the `issue-3665-supervision-gaps` branch (commit `6ffe97c8e`).** The
operator-facing work is to review and integrate that commit:

1. **Agent livelock/repetition-loop detection** — Already implemented as
   `detect_agent_livelock` in `loop_detection.py` (317 lines).
2. **Two-hour timeout visibility** — Already implemented: `agent_timeout_seconds`
   config field, `EGG_AGENT_TIMEOUT_SECONDS` env, exit 143 →
   `JOB_OUTCOME_LEGITIMATE`.
3. **False convergence-stall suppression** — Already implemented:
   `_has_recent_agent_activity()` in the event loop, `get_agent_activity_ages()`
   on HealthMonitor, snapshot enrichment for tool-call/heartbeat ages.

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

## What the fix commit implements (verified at 6ffe97c8e)

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
  (line 147). **Note:** uses the existing `JOB_OUTCOME_LEGITIMATE` constant,
  NOT a new `JOB_OUTCOME_TIMEOUT` as the plan proposed.
- ✅ `AgentLivelockCheck` class — implemented in `loop_detection.py`
- ✅ `detect_agent_livelock` function — implemented in `loop_detection.py`

## The three task groups (as implemented in the fix commit)

### Task Group 1: Supervision Detectors and Snapshot Enrichment

**Already implemented in the fix commit.** The livelock detector is at
`orchestrator/health_checks/tier1/loop_detection.py` (317 lines). It is
registered in `DetectionPlane.default()` and the health check runner
(`cli.py`). The snapshot builder is enriched to populate
`last_tool_call_age_s` and `last_heartbeat_age_s` on `RunningAgent` entries
from the health monitor's per-agent activity data.

### Task Group 2: Two-Hour Timeout Visibility

**Already implemented in the fix commit.** `agent_timeout_seconds` added to
`PipelineConfig` (default 7200). `EGG_AGENT_TIMEOUT_SECONDS` passed to the
sandbox. Exit code 143 (SIGTERM) classified as `JOB_OUTCOME_LEGITIMATE`
(not a new `JOB_OUTCOME_TIMEOUT` constant — the fix commit reuses the
existing outcome category, which is simpler).

### Task Group 4: Tests

**Already implemented in the fix commit.** Four test files created:
`test_loop_detection.py`, `test_timeout_sigterm.py`,
`test_convergence_stall_suppression.py`, `test_agent_timeout_config.py`.

## Dependencies and ordering

The fix commit implements all three task groups in a single commit. The
task groups are independent (no cross-dependencies), and tests cover all
three. Integration review should verify:

1. The livelock detector's false-positive rate (10% unique-ratio threshold
   with ≥10 total calls).
2. The timeout classification correctly distinguishes SIGTERM (143) from
   other abnormal exits.
3. The convergence-stall suppression doesn't mask real stalls.

## Open questions (HITL — not yet resolved)

Three decisions are registered on the SDLC contract and will be resolved by
the operator before or during implementation:

- **cq-1**: Livelock detector tool-call signature parsing — Should we parse
  the existing Claude Code log format as-is, or add a structured tool-call
  event emitter in the sandbox for more reliable parsing? The fix commit
  defaults to parsing existing logs (agent_log_store captures full stdout).

- **cq-2**: Two-hour timeout — Should the `agent_timeout_seconds` config be
  pipeline-level only (uniform 7200s default), or support per-role overrides?
  The fix commit defaults to pipeline-level.

- **cq-3**: Livelock recovery action — When the detector fires, should the
  corrective vocabulary nudge the agent with a loop description, or respawn
  the agent? The fix commit defaults to nudge (less disruptive).

## What was left out (and why)

- **The overseer poll cycle** — The overseer is NOT deprecated (verified:
  `start()` has no deprecation marker, `overseer_poll_interval_seconds` is
  live). The overseer's phase-duration affordance (#3577) and crash-respawn
  backoff (#3212) are listed as candidates in the analysis draft, not as
  priorities here.
- **The `detect_loop` / `classify_activity_pattern` LLM classifiers** — These
  are Haiku-tier classifiers that run only after an alert is raised. They are
  not the right tool for proactive loop detection.
- **The 4-hour K8s `active_deadline_seconds`** — This is a K8s-level safety
  net, not an application-level timeout. It is correctly set higher than the
  2-hour agent timeout. The fix commit makes the agent timeout configurable
  but does not change the K8s deadline.

## Files in the fix commit (6ffe97c8e)

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
