# BRC Consensus History — implement phase

Generated: 2026-04-24T04:37:26Z
Pipeline: issue-1932

### [2026-04-24T03:49:32Z] overseer (HEARTBEAT): heartbeat: WORKING

Cycle 9: Pipeline transitioned to IMPLEMENT phase. 5 agents now WORKING: coder, documenter, tester, reviewer_code, reviewer_contract. 0 HITL decisions. 0 alerts. Monitoring implement phase BRC.

````yaml
id: dd32db55-0599-46
phase: implement
metadata:
  state: WORKING
````

### [2026-04-24T03:50:03Z] overseer (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Cycle 2: 0 alerts. Implement phase active since ~03:47 UTC. All 5 agents (coder, documenter, tester, reviewer_code, reviewer_contract) WORKING — no proposals yet. Normal for early implement phase. Awaiting coder/tester first proposals.

````yaml
id: ae4318f2-de8c-45
phase: implement
metadata:
  state: WAITING_ON_ROLE
  waiting_on: coder
````

### [2026-04-24T03:51:11Z] reviewer_contract (HEARTBEAT): heartbeat: WAITING_ON_ROLE

reviewer_contract prepared; contract has 0 tasks/0 AC in JSON (still current_phase=refine in contract blob); plan draft defines TASK-1-1..4-7; waiting for coder CONSENSUS_PROPOSE to verify against plan-defined tasks.

````yaml
id: 687385a9-8edc-47
phase: implement
metadata:
  state: WAITING_ON_ROLE
  waiting_on: coder
````

### [2026-04-24T03:52:36Z] documenter (HEARTBEAT): heartbeat: WORKING

Updating SKILL.md and adding new docs for wait_for_status_change MCP tool

````yaml
id: 741722d1-5c8d-44
phase: implement
metadata:
  state: WORKING
````

### [2026-04-24T03:54:48Z] tester (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Tester ready with scaffolding in host-worktree. Waiting for coder proposal.

````yaml
id: cccc8481-2043-46
phase: implement
metadata:
  state: WAITING_ON_ROLE
  waiting_on: coder
````

### [2026-04-24T04:02:42Z] reviewer_code (HEARTBEAT): heartbeat: WAITING_ON_ROLE

reviewer_code ready; prep complete (read contract, analysis, plan, existing events.py/mcp_server.py/env_config.py/mcp_tools.py/messages.py). Blocking on CONSENSUS_PROPOSE from coder/tester/documenter.

````yaml
id: bf634b1b-e8e0-4c
phase: implement
metadata:
  state: WAITING_ON_ROLE
  waiting_on: coder
````

### [2026-04-24T04:03:19Z] documenter (CONSENSUS_PROPOSE): Proposal from documenter

Documenter scope complete for #1932. Updated docs and SDLC skill prompt to cover the new wait_for_status_change MCP tool — the event-triggered host-side poll vehicle that replaces the time-triggered get_status(wait=25) loop. Four files touched in commit 9c517f3af on origin/egg/issue-1932: (1) skills/sdlc/SKILL.md — Phase 3 and Phase S5 monitor loops switched from get_status(wait=25) to wait_for_status_change(wait=25, since=<cursor>) for subsequent polls, with side-by-side Path A (changed: true) / Path B (no_change: true) envelope shapes, opaque cursor-handling protocol, structural branching guidance (branch on the no_change key — distinct from !changed), cached-snapshot reuse rules for Path B, refreshed Consensus Monitoring/Fallback/Long-Running Phase Detection/Stuck Pipeline Rescue/Phase 4 HITL/Troubleshooting/Critical Rules sections, and updated Important notes pointing operators away from sleep loops; (2) docs/reference/agent-wait-patterns.md — new §7 Host-Side Waits — wait_for_status_change covering the two response envelopes, the explicit event-trigger allowlist plus DECISION_RESOLVED-exclusion reasoning, the opaque msg:<id>|evt:<seq> cursor protocol, the queue + daemon-thread concurrency model with the accepted lame-duck window, error responses (400 malformed cursor, 404 unknown pipeline), the aspirational-liveness reasoning, and a worked example. Existing §7 (EGG_ORCH_WAITRESS_THREADS) renumbered to §8 with the new 16 → 24 default and 2-threads-per-host-wait sizing rule; existing §8 renumbered to §9 with cross-links to the new release note + SDLC skill; (3) docs/releases/wait-for-status-change.md — new release note following the agent-mcp-tools.md template covering issue link, six-item what-changed list, rationale (token savings, sub-second latency, race window closed by cursor, liveness preservation), trigger allowlist, envelope shapes, cursor protocol, rollback path (skill-first revert, daemon-thread bound), and Future Work covering R7 (literal liveness watchdog), R11 (Python SDK MCP surface parity), R14 (message_store cancellation signal); (4) docs/architecture/orchestrator.md — MCP tool inventory now includes wait_for_status_change with a one-paragraph explainer cross-linking the new §7. Closes documenter-scope tasks TASK-3-1, TASK-3-2, TASK-3-3, TASK-3-4, TASK-4-6, TASK-4-7. Tests run: none — documentation-only commit; the coder/tester own the underlying server-side route, MCP tool surface, EventBus.sequence field, prometheus metric, and Waitress-default tests. Anchors verified to follow existing repo convention (em-dash → double-dash, e.g. #6-egg_message_poll_max_wait--long-poll-cap-coupling). All cross-links between SKILL.md and the new §7 use the matching #7-host-side-waits--wait_for_status_change anchor.

````yaml
id: 9f49035a-9481-4f
phase: implement
metadata:
  payload:
    summary: "Documenter scope complete for #1932. Updated docs and SDLC skill prompt\
      \ to cover the new wait_for_status_change MCP tool \u2014 the event-triggered\
      \ host-side poll vehicle that replaces the time-triggered get_status(wait=25)\
      \ loop. Four files touched in commit 9c517f3af on origin/egg/issue-1932: (1)\
      \ skills/sdlc/SKILL.md \u2014 Phase 3 and Phase S5 monitor loops switched from\
      \ get_status(wait=25) to wait_for_status_change(wait=25, since=<cursor>) for\
      \ subsequent polls, with side-by-side Path A (changed: true) / Path B (no_change:\
      \ true) envelope shapes, opaque cursor-handling protocol, structural branching\
      \ guidance (branch on the no_change key \u2014 distinct from !changed), cached-snapshot\
      \ reuse rules for Path B, refreshed Consensus Monitoring/Fallback/Long-Running\
      \ Phase Detection/Stuck Pipeline Rescue/Phase 4 HITL/Troubleshooting/Critical\
      \ Rules sections, and updated Important notes pointing operators away from sleep\
      \ loops; (2) docs/reference/agent-wait-patterns.md \u2014 new \xA77 Host-Side\
      \ Waits \u2014 wait_for_status_change covering the two response envelopes, the\
      \ explicit event-trigger allowlist plus DECISION_RESOLVED-exclusion reasoning,\
      \ the opaque msg:<id>|evt:<seq> cursor protocol, the queue + daemon-thread concurrency\
      \ model with the accepted lame-duck window, error responses (400 malformed cursor,\
      \ 404 unknown pipeline), the aspirational-liveness reasoning, and a worked example.\
      \ Existing \xA77 (EGG_ORCH_WAITRESS_THREADS) renumbered to \xA78 with the new\
      \ 16 \u2192 24 default and 2-threads-per-host-wait sizing rule; existing \xA7\
      8 renumbered to \xA79 with cross-links to the new release note + SDLC skill;\
      \ (3) docs/releases/wait-for-status-change.md \u2014 new release note following\
      \ the agent-mcp-tools.md template covering issue link, six-item what-changed\
      \ list, rationale (token savings, sub-second latency, race window closed by\
      \ cursor, liveness preservation), trigger allowlist, envelope shapes, cursor\
      \ protocol, rollback path (skill-first revert, daemon-thread bound), and Future\
      \ Work covering R7 (literal liveness watchdog), R11 (Python SDK MCP surface\
      \ parity), R14 (message_store cancellation signal); (4) docs/architecture/orchestrator.md\
      \ \u2014 MCP tool inventory now includes wait_for_status_change with a one-paragraph\
      \ explainer cross-linking the new \xA77. Closes documenter-scope tasks TASK-3-1,\
      \ TASK-3-2, TASK-3-3, TASK-3-4, TASK-4-6, TASK-4-7. Tests run: none \u2014 documentation-only\
      \ commit; the coder/tester own the underlying server-side route, MCP tool surface,\
      \ EventBus.sequence field, prometheus metric, and Waitress-default tests. Anchors\
      \ verified to follow existing repo convention (em-dash \u2192 double-dash, e.g.\
      \ #6-egg_message_poll_max_wait--long-poll-cap-coupling). All cross-links between\
      \ SKILL.md and the new \xA77 use the matching #7-host-side-waits--wait_for_status_change\
      \ anchor."
    attestation: {}
    artifacts:
    - skills/sdlc/SKILL.md
    - docs/reference/agent-wait-patterns.md
    - docs/releases/wait-for-status-change.md
    - docs/architecture/orchestrator.md
    risk_considered: "R6 (SKILL.md branching drift) \u2014 mitigated by emphasizing\
      \ structural branch on no_change key (not on !changed) and providing side-by-side\
      \ envelope shapes in both Phase 3 and Phase S5. R7 (aspirational liveness floor)\
      \ \u2014 documented in SKILL.md Important notes, \xA77.6 of agent-wait-patterns,\
      \ and Future Work in the release note. R11 (Python SDK parity) \u2014 release\
      \ note Future Work names #1920 follow-up. R14 (daemon-thread lame-duck) \u2014\
      \ documented as accepted in \xA77.4 with bounds and follow-up cancellation-signal\
      \ idea."
    commit_sha: 9c517f3af
    files_changed:
    - skills/sdlc/SKILL.md
    - docs/reference/agent-wait-patterns.md
    - docs/releases/wait-for-status-change.md
    - docs/architecture/orchestrator.md
    tests_run: []
    tasks_satisfied:
    - task-3-1
    - task-3-2
    - task-3-3
    - task-3-4
    - task-4-6
    - task-4-7
  version: 1
  commit_sha: 9c517f3af
````

### [2026-04-24T04:03:24Z] documenter (HEARTBEAT): heartbeat: PROPOSED

Proposed v1: 4 doc files updated covering wait_for_status_change MCP tool — commit 9c517f3af. Awaiting reviewer_code.

````yaml
id: f5c5f63e-fd37-4a
phase: implement
metadata:
  state: PROPOSED
````

### [2026-04-24T04:11:31Z] coder → tester (HANDOFF): issue-1932 Phase 1+2 ready; test handoff at .egg-state/agent-outputs/1932-coder-tests/

Phase 1 + Phase 2 production code is merged on egg/issue-1932:

- Phase 1 (c57d14649): Event.sequence + EventBus._sequence + current_sequence(), GET /api/v1/pipelines/<id>/status/wait route with EventBus + message_store composition + queue.Queue(16) + daemon thread pattern, egg_inflight_host_waits gauge, DEFAULT_WAITRESS_THREADS 16->24.

- Phase 2 (1258ff399): wait_for_status_change MCP tool in PIPELINE_TOOLS with full schema, _build_status_snapshot extracted from _handle_get_status (pure refactor), _handle_wait_for_status_change method wired into the dispatcher.

I authored the Phase 4 test files while implementing — they pass on 1258ff399. Coder role cannot push orchestrator/tests/** so they're staged at .egg-state/agent-outputs/1932-coder-tests/:

  - test_pipelines_status_wait_route.py (16 cases, TASK-4-1) — drop in as orchestrator/tests/test_pipelines_status_wait_route.py
  - test_events_event_sequence.py (7 cases, TASK-4-3) — drop in as orchestrator/tests/test_events_event_sequence.py
  - test_mcp_tools_additions.py (8 cases, TASK-4-2 + TASK-4-4) — append classes to orchestrator/tests/test_mcp_tools.py

HEADS UP: orchestrator/tests/test_mcp_tools.py TestToolRouting.test_all_tools_registered expects a fixed set of tool names and will fail now that PIPELINE_TOOLS has wait_for_status_change. Add 'wait_for_status_change' to the 'expected' set to unblock that test.

TASK-4-5 (integration test integration_tests/test_host_wait_end_to_end.py) is yours — I didn't pre-author that one since it needs a real orchestrator fixture.

See .egg-state/agent-outputs/1932-coder-tests/HANDOFF.md for the full handoff note.

````yaml
id: bf8a6848-ae53-4e
phase: implement
````

### [2026-04-24T04:12:10Z] coder (CONSENSUS_PROPOSE): Proposal from coder

Phase 1 + Phase 2 implementation for issue #1932 (event-driven host-side wait). Phase 1 (c57d14649) adds Event.sequence + EventBus._sequence (issue#1932 TASK-1-1), the new GET /api/v1/pipelines/<id>/status/wait route composing EventBus with message_store long-poll via queue.Queue(16) + daemon thread + wildcard handler (TASK-1-2), egg_inflight_host_waits gauge best-effort (TASK-1-3), and DEFAULT_WAITRESS_THREADS 16->24 (TASK-1-4). Phase 2 (1258ff399) registers wait_for_status_change in PIPELINE_TOOLS with full schema documenting both envelope shapes and the opaque cursor contract (TASK-2-1), extracts _build_status_snapshot from _handle_get_status as a pure refactor (TASK-2-2), and adds _handle_wait_for_status_change wired into the dispatcher (TASK-2-3). Opaque compound cursor msg:<id>|evt:<seq> parses both halves independently; missing halves degrade to snap-to-tip. Event allowlist is explicit (phase.*, decision.created, pipeline.{completed,failed,cancelled}); DECISION_RESOLVED deliberately excluded to prevent self-wake. Message allowlist covers OVERSEER_ALERT + CONSENSUS_{CONFIRMED,NACK,RE_REVIEW}. Daemon-thread lame-duck accepted per plan R14. Tests authored alongside (16+7+8=31 passing cases) but handed off to tester via .egg-state/agent-outputs/1932-coder-tests/ since coder cannot push orchestrator/tests/**. Existing 163 test_mcp_tools cases still pass after refactor. Skill/docs edits are documenter scope; integration test (TASK-4-5) is tester scope.

````yaml
id: 8ec79fe5-35a5-4f
phase: implement
metadata:
  payload:
    summary: 'Phase 1 + Phase 2 implementation for issue #1932 (event-driven host-side
      wait). Phase 1 (c57d14649) adds Event.sequence + EventBus._sequence (issue#1932
      TASK-1-1), the new GET /api/v1/pipelines/<id>/status/wait route composing EventBus
      with message_store long-poll via queue.Queue(16) + daemon thread + wildcard
      handler (TASK-1-2), egg_inflight_host_waits gauge best-effort (TASK-1-3), and
      DEFAULT_WAITRESS_THREADS 16->24 (TASK-1-4). Phase 2 (1258ff399) registers wait_for_status_change
      in PIPELINE_TOOLS with full schema documenting both envelope shapes and the
      opaque cursor contract (TASK-2-1), extracts _build_status_snapshot from _handle_get_status
      as a pure refactor (TASK-2-2), and adds _handle_wait_for_status_change wired
      into the dispatcher (TASK-2-3). Opaque compound cursor msg:<id>|evt:<seq> parses
      both halves independently; missing halves degrade to snap-to-tip. Event allowlist
      is explicit (phase.*, decision.created, pipeline.{completed,failed,cancelled});
      DECISION_RESOLVED deliberately excluded to prevent self-wake. Message allowlist
      covers OVERSEER_ALERT + CONSENSUS_{CONFIRMED,NACK,RE_REVIEW}. Daemon-thread
      lame-duck accepted per plan R14. Tests authored alongside (16+7+8=31 passing
      cases) but handed off to tester via .egg-state/agent-outputs/1932-coder-tests/
      since coder cannot push orchestrator/tests/**. Existing 163 test_mcp_tools cases
      still pass after refactor. Skill/docs edits are documenter scope; integration
      test (TASK-4-5) is tester scope.'
    attestation: {}
    artifacts:
    - orchestrator/events.py
    - orchestrator/env_config.py
    - orchestrator/routes/pipelines.py
    - orchestrator/mcp_tools.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    - .egg-state/agent-outputs/1932-coder-tests/HANDOFF.md
    risk_considered: "R1 self-wake via DECISION_RESOLVED \u2014 mitigated via explicit\
      \ allowlist at route. R2 snapshot->wait transition race \u2014 mitigated via\
      \ since cursor + from_tip fallback. R3 EventBus cursor \u2014 Event.sequence\
      \ + EventBus._sequence populated under existing _lock, exposed via current_sequence()\
      \ for tip queries. R4 Waitress thread starvation \u2014 default raised 16->24,\
      \ egg_inflight_host_waits gauge parallels egg_inflight_long_polls. R5 invisible\
      \ consensus drift \u2014 concurrent.consensus shipped in minimal envelope. R6\
      \ SKILL.md branching drift \u2014 distinct no_change: true key (structural branch).\
      \ R13 Delphi filter \u2014 applied on message-wake path. R14 daemon-thread lame-duck\
      \ \u2014 accepted, bounded at wait seconds, non-blocking on shutdown. R15 wildcard\
      \ handler delivery-thread blocking \u2014 put_nowait + try/except queue.Full\
      \ + WARNING log. R16 double-sleep regression \u2014 _apply_get_status_wait short-circuit\
      \ preserved (tool_name == 'get_status' only); regression test in coder-authored\
      \ handoff test file. R17 malformed cursor / unknown pipeline \u2014 400/404\
      \ with descriptive error bodies."
    commit_sha: 20e1d083c
    files_changed:
    - orchestrator/events.py
    - orchestrator/env_config.py
    - orchestrator/routes/pipelines.py
    - orchestrator/mcp_tools.py
    tests_run:
    - orchestrator/tests/test_mcp_tools.py (163 passed)
    - orchestrator/tests/test_concurrent_status.py (19 passed)
    - orchestrator/tests/test_messages.py (full suite)
    - orchestrator/tests/test_concurrent_wait.py (70 passed)
    - Coder-authored tests at .egg-state/agent-outputs/1932-coder-tests/ (31 passed,
      pending tester drop-in)
    tasks_satisfied:
    - TASK-1-1
    - TASK-1-2
    - TASK-1-3
    - TASK-1-4
    - TASK-2-1
    - TASK-2-2
    - TASK-2-3
  version: 1
  commit_sha: 20e1d083c
````

### [2026-04-24T04:12:20Z] reviewer_code → documenter (CONSENSUS_NACK): NACK from reviewer_code for documenter


Reviewed the four documenter-scope files against the actual code landed by the coder (commits c57d14649 + 1258ff399). The docs are thorough and mostly accurate — worked examples are clear, tables are complete, §7.4 concurrency diagram is genuinely helpful — but two documentation claims materially contradict the shipped code. The first is especially concerning because SKILL.md is an LLM prompt — incorrect instructions there directly steer runtime behavior.

### Blocking

1. **SKILL.md repeatedly claims `get_status` returns a `cursor` field. It does not.** The code `_build_status_snapshot` at `orchestrator/mcp_tools.py:1614-1728` builds the status dict with `pipeline`, `current_phase`, `status`, `running_agents`, `completed_agents`, `phase_started_at`, `phase_elapsed_seconds`, `pending_decisions`, `recent_messages` — there is no `cursor` field. `_handle_get_status` is just a thin wrapper over the same helper. Yet SKILL.md instructs the LLM, four times, to capture this non-existent field:
   - `skills/sdlc/SKILL.md:318` — "The response includes a `cursor` field (opaque string of shape `msg:<id>|evt:<seq>`) that seeds the next call."
   - `skills/sdlc/SKILL.md:321` — "The first `get_status` call returns a starter cursor..."
   - `skills/sdlc/SKILL.md:1220` — "Capture the `cursor` field from the response."
   - `skills/sdlc/SKILL.md:1223` — "The first `get_status` call returns a starter cursor..."

   Why this matters: the LLM running the SDLC skill will read the prompt literally, try to pull `response.cursor` from a get_status return value that lacks it, then either (a) crash on an undefined reference, (b) pass literal `undefined`/`None` as `since` — the route's regex rejects that with 400, or (c) hallucinate a cursor by synthesizing from adjacent fields (e.g. `recent_messages[-1].id`) — this produces a malformed compound cursor that skips or drops events unpredictably. This undermines the entire event-driven wake contract the PR is supposed to deliver.

   Fix: rewrite these four claim sites so they describe what actually happens. The route already handles a missing `since` gracefully (`_parse_status_wait_cursor(None) → (True, None, None) → snap to tip`), so the simplest fix is doc-only:
   ```
   First poll: `get_status(task_id)` — returns the full snapshot.
   First `wait_for_status_change(task_id, wait=25)` call: omit `since` (or pass `""`);
     the route snaps to the tip of both event sources.
   Every subsequent call: `wait_for_status_change(task_id, wait=25, since=<response.cursor>)`
     using the cursor returned by the prior `wait_for_status_change` response.
   ```
   Remove every "the first `get_status` call returns a starter cursor" sentence. Update the "Cursor handling" blocks in both Phase 3 (~line 321) and Phase S5 (~line 1223) accordingly. The same misstatement in the Critical Rules bullet at line 932 is fine as-is (it says "thread the response `cursor` from one call into the next call's `since`" — this is accurate if "the response" means a `wait_for_status_change` response; add a clarifying parenthetical).

2. **docs/reference/agent-wait-patterns.md §7.5 describes error response bodies that do not match the route.** Lines 630-631 claim:
   | **400** | ... | `{"error": "invalid_cursor", "detail": "..."}` |
   | **404** | ... | `{"error": "unknown_pipeline", "pipeline_id": "..."}` |

   The route at `orchestrator/routes/pipelines.py:2470-2491` returns `make_error_response(...)`. That helper at `orchestrator/routes/pipelines.py:787-794` produces `{"success": false, "message": "...", "details": ...?}` — no `error` key, no `detail` key, no `pipeline_id` key. A client consuming the documented shape will `KeyError` on `error` and never see the actual explanation carried in `message`.

   Fix: update the table to describe the real shape, e.g.
   ```
   400 → `{"success": false, "message": "Invalid 'since' cursor — expected 'msg:<id>|evt:<seq>' (either half may be empty)."}`
   404 → `{"success": false, "message": "Pipeline <id> not found"}`
   400 (bad wait) → `{"success": false, "message": "Invalid 'wait' query parameter: must be an integer"}`
   ```
   Or, if you prefer to document the helper shape once and reference it, link to the shape used by the rest of the orchestrator REST surface.

### Non-blocking

- **docs/reference/agent-wait-patterns.md §7.1 envelope table says `phase_elapsed_seconds` is always present on both paths.** `_build_minimal_status_envelope` at `orchestrator/routes/pipelines.py:335-364` only sets `phase_elapsed_seconds` when `phase_data.started_at` is truthy (and parseable). At phase boundaries or when the phase hasn't recorded `started_at` yet, the field will be absent. Consider softening to "when phase has started" or noting the conditional.
- **SKILL.md Phase 3 vs Phase S5 inconsistency.** Phase 3 (line 355 area) tells the skill to reuse `running_agents` / `completed_agents` (top-level fields from `_build_status_snapshot`). Phase S5 (line 1252) tells it to reuse `concurrent.agents` (a nested field from `_get_concurrent_status`). Both cached-snapshot sources exist, but the two sections use different names for "the list of agents" — harmonize to reduce LLM confusion. Preferred: "`running_agents`, `completed_agents`, and `concurrent.agents` where present".
- **§7.4 ASCII diagram** shows the wildcard handler pushing via `put_nowait` and the daemon pushing via a plain `put`. The daemon actually also uses `put_nowait` in a `try/except queue.Full` block (route implementation, search for `_on_message_store_wake`). The diagram is slightly misleading — either swap the second `put` for `put_nowait` or drop the method name from both arrows.
- **docs/releases/wait-for-status-change.md is well-structured.** No blocking issues there. One nit: the "70% reduction in tokens-per-quiet-minute on real pipelines" claim in the Rationale section should either cite data or be softened to "expected"/"targeted", since there is no tester measurement in this PR to back it.
- **docs/architecture/orchestrator.md** — the one-paragraph addition is accurate and cross-links correctly. No issues.

### Observations on the under-review coder code (not documenter scope — flagged here because the SKILL.md issue above depends on fixing SKILL.md's claims rather than the code)

While reviewing the docs I read the server route and MCP handler end-to-end. I will raise these against the coder when they propose; listing here for visibility and so the docs don't need to be re-revised if we decide to fix the code instead:
- Race window between `event_bus.current_sequence()` read and `event_bus.subscribe(None, _on_event)` in the new route: events published in that microsecond window are not delivered to the handler AND are skipped by the cursor on the next call (because the returned cursor's `evt:` half is the post-event tip). Narrow but real — and the plan explicitly frames closing race windows as the point of the cursor (R2). A simple fix is to subscribe first with a handler that tolerates `event_since_seq = None` by accepting any event, then set `event_since_seq = current_sequence()` and allow duplicates-on-next-call (harmless, cursor filters them).
- `_message_store_tip_id` has dead code (the `limit=10_000` fallback is unreachable — `limit=1` already returns the newest message when any exist in the in-memory and Redis backends).

No action needed from the documenter on these two — they're coder-scope items I'll carry into the coder's review.


````yaml
id: 2140f0bc-b441-43
phase: implement
metadata:
  payload:
    reason: "\nReviewed the four documenter-scope files against the actual code landed\
      \ by the coder (commits c57d14649 + 1258ff399). The docs are thorough and mostly\
      \ accurate \u2014 worked examples are clear, tables are complete, \xA77.4 concurrency\
      \ diagram is genuinely helpful \u2014 but two documentation claims materially\
      \ contradict the shipped code. The first is especially concerning because SKILL.md\
      \ is an LLM prompt \u2014 incorrect instructions there directly steer runtime\
      \ behavior.\n\n### Blocking\n\n1. **SKILL.md repeatedly claims `get_status`\
      \ returns a `cursor` field. It does not.** The code `_build_status_snapshot`\
      \ at `orchestrator/mcp_tools.py:1614-1728` builds the status dict with `pipeline`,\
      \ `current_phase`, `status`, `running_agents`, `completed_agents`, `phase_started_at`,\
      \ `phase_elapsed_seconds`, `pending_decisions`, `recent_messages` \u2014 there\
      \ is no `cursor` field. `_handle_get_status` is just a thin wrapper over the\
      \ same helper. Yet SKILL.md instructs the LLM, four times, to capture this non-existent\
      \ field:\n   - `skills/sdlc/SKILL.md:318` \u2014 \"The response includes a `cursor`\
      \ field (opaque string of shape `msg:<id>|evt:<seq>`) that seeds the next call.\"\
      \n   - `skills/sdlc/SKILL.md:321` \u2014 \"The first `get_status` call returns\
      \ a starter cursor...\"\n   - `skills/sdlc/SKILL.md:1220` \u2014 \"Capture the\
      \ `cursor` field from the response.\"\n   - `skills/sdlc/SKILL.md:1223` \u2014\
      \ \"The first `get_status` call returns a starter cursor...\"\n\n   Why this\
      \ matters: the LLM running the SDLC skill will read the prompt literally, try\
      \ to pull `response.cursor` from a get_status return value that lacks it, then\
      \ either (a) crash on an undefined reference, (b) pass literal `undefined`/`None`\
      \ as `since` \u2014 the route's regex rejects that with 400, or (c) hallucinate\
      \ a cursor by synthesizing from adjacent fields (e.g. `recent_messages[-1].id`)\
      \ \u2014 this produces a malformed compound cursor that skips or drops events\
      \ unpredictably. This undermines the entire event-driven wake contract the PR\
      \ is supposed to deliver.\n\n   Fix: rewrite these four claim sites so they\
      \ describe what actually happens. The route already handles a missing `since`\
      \ gracefully (`_parse_status_wait_cursor(None) \u2192 (True, None, None) \u2192\
      \ snap to tip`), so the simplest fix is doc-only:\n   ```\n   First poll: `get_status(task_id)`\
      \ \u2014 returns the full snapshot.\n   First `wait_for_status_change(task_id,\
      \ wait=25)` call: omit `since` (or pass `\"\"`);\n     the route snaps to the\
      \ tip of both event sources.\n   Every subsequent call: `wait_for_status_change(task_id,\
      \ wait=25, since=<response.cursor>)`\n     using the cursor returned by the\
      \ prior `wait_for_status_change` response.\n   ```\n   Remove every \"the first\
      \ `get_status` call returns a starter cursor\" sentence. Update the \"Cursor\
      \ handling\" blocks in both Phase 3 (~line 321) and Phase S5 (~line 1223) accordingly.\
      \ The same misstatement in the Critical Rules bullet at line 932 is fine as-is\
      \ (it says \"thread the response `cursor` from one call into the next call's\
      \ `since`\" \u2014 this is accurate if \"the response\" means a `wait_for_status_change`\
      \ response; add a clarifying parenthetical).\n\n2. **docs/reference/agent-wait-patterns.md\
      \ \xA77.5 describes error response bodies that do not match the route.** Lines\
      \ 630-631 claim:\n   | **400** | ... | `{\"error\": \"invalid_cursor\", \"detail\"\
      : \"...\"}` |\n   | **404** | ... | `{\"error\": \"unknown_pipeline\", \"pipeline_id\"\
      : \"...\"}` |\n\n   The route at `orchestrator/routes/pipelines.py:2470-2491`\
      \ returns `make_error_response(...)`. That helper at `orchestrator/routes/pipelines.py:787-794`\
      \ produces `{\"success\": false, \"message\": \"...\", \"details\": ...?}` \u2014\
      \ no `error` key, no `detail` key, no `pipeline_id` key. A client consuming\
      \ the documented shape will `KeyError` on `error` and never see the actual explanation\
      \ carried in `message`.\n\n   Fix: update the table to describe the real shape,\
      \ e.g.\n   ```\n   400 \u2192 `{\"success\": false, \"message\": \"Invalid 'since'\
      \ cursor \u2014 expected 'msg:<id>|evt:<seq>' (either half may be empty).\"\
      }`\n   404 \u2192 `{\"success\": false, \"message\": \"Pipeline <id> not found\"\
      }`\n   400 (bad wait) \u2192 `{\"success\": false, \"message\": \"Invalid 'wait'\
      \ query parameter: must be an integer\"}`\n   ```\n   Or, if you prefer to document\
      \ the helper shape once and reference it, link to the shape used by the rest\
      \ of the orchestrator REST surface.\n\n### Non-blocking\n\n- **docs/reference/agent-wait-patterns.md\
      \ \xA77.1 envelope table says `phase_elapsed_seconds` is always present on both\
      \ paths.** `_build_minimal_status_envelope` at `orchestrator/routes/pipelines.py:335-364`\
      \ only sets `phase_elapsed_seconds` when `phase_data.started_at` is truthy (and\
      \ parseable). At phase boundaries or when the phase hasn't recorded `started_at`\
      \ yet, the field will be absent. Consider softening to \"when phase has started\"\
      \ or noting the conditional.\n- **SKILL.md Phase 3 vs Phase S5 inconsistency.**\
      \ Phase 3 (line 355 area) tells the skill to reuse `running_agents` / `completed_agents`\
      \ (top-level fields from `_build_status_snapshot`). Phase S5 (line 1252) tells\
      \ it to reuse `concurrent.agents` (a nested field from `_get_concurrent_status`).\
      \ Both cached-snapshot sources exist, but the two sections use different names\
      \ for \"the list of agents\" \u2014 harmonize to reduce LLM confusion. Preferred:\
      \ \"`running_agents`, `completed_agents`, and `concurrent.agents` where present\"\
      .\n- **\xA77.4 ASCII diagram** shows the wildcard handler pushing via `put_nowait`\
      \ and the daemon pushing via a plain `put`. The daemon actually also uses `put_nowait`\
      \ in a `try/except queue.Full` block (route implementation, search for `_on_message_store_wake`).\
      \ The diagram is slightly misleading \u2014 either swap the second `put` for\
      \ `put_nowait` or drop the method name from both arrows.\n- **docs/releases/wait-for-status-change.md\
      \ is well-structured.** No blocking issues there. One nit: the \"70% reduction\
      \ in tokens-per-quiet-minute on real pipelines\" claim in the Rationale section\
      \ should either cite data or be softened to \"expected\"/\"targeted\", since\
      \ there is no tester measurement in this PR to back it.\n- **docs/architecture/orchestrator.md**\
      \ \u2014 the one-paragraph addition is accurate and cross-links correctly. No\
      \ issues.\n\n### Observations on the under-review coder code (not documenter\
      \ scope \u2014 flagged here because the SKILL.md issue above depends on fixing\
      \ SKILL.md's claims rather than the code)\n\nWhile reviewing the docs I read\
      \ the server route and MCP handler end-to-end. I will raise these against the\
      \ coder when they propose; listing here for visibility and so the docs don't\
      \ need to be re-revised if we decide to fix the code instead:\n- Race window\
      \ between `event_bus.current_sequence()` read and `event_bus.subscribe(None,\
      \ _on_event)` in the new route: events published in that microsecond window\
      \ are not delivered to the handler AND are skipped by the cursor on the next\
      \ call (because the returned cursor's `evt:` half is the post-event tip). Narrow\
      \ but real \u2014 and the plan explicitly frames closing race windows as the\
      \ point of the cursor (R2). A simple fix is to subscribe first with a handler\
      \ that tolerates `event_since_seq = None` by accepting any event, then set `event_since_seq\
      \ = current_sequence()` and allow duplicates-on-next-call (harmless, cursor\
      \ filters them).\n- `_message_store_tip_id` has dead code (the `limit=10_000`\
      \ fallback is unreachable \u2014 `limit=1` already returns the newest message\
      \ when any exist in the in-memory and Redis backends).\n\nNo action needed from\
      \ the documenter on these two \u2014 they're coder-scope items I'll carry into\
      \ the coder's review.\n"
    artifact_references:
    - skills/sdlc/SKILL.md
    - docs/reference/agent-wait-patterns.md
    - docs/releases/wait-for-status-change.md
    - docs/architecture/orchestrator.md
  reason: "\nReviewed the four documenter-scope files against the actual code landed\
    \ by the coder (commits c57d14649 + 1258ff399). The docs are thorough and mostly\
    \ accurate \u2014 worked examples are clear, tables are complete, \xA77.4 concurrency\
    \ diagram is genuinely helpful \u2014 but two documentation claims materially\
    \ contradict the shipped code. The first is especially concerning because SKILL.md\
    \ is an LLM prompt \u2014 incorrect instructions there directly steer runtime\
    \ behavior.\n\n### Blocking\n\n1. **SKILL.md repeatedly claims `get_status` returns\
    \ a `cursor` field. It does not.** The code `_build_status_snapshot` at `orchestrator/mcp_tools.py:1614-1728`\
    \ builds the status dict with `pipeline`, `current_phase`, `status`, `running_agents`,\
    \ `completed_agents`, `phase_started_at`, `phase_elapsed_seconds`, `pending_decisions`,\
    \ `recent_messages` \u2014 there is no `cursor` field. `_handle_get_status` is\
    \ just a thin wrapper over the same helper. Yet SKILL.md instructs the LLM, four\
    \ times, to capture this non-existent field:\n   - `skills/sdlc/SKILL.md:318`\
    \ \u2014 \"The response includes a `cursor` field (opaque string of shape `msg:<id>|evt:<seq>`)\
    \ that seeds the next call.\"\n   - `skills/sdlc/SKILL.md:321` \u2014 \"The first\
    \ `get_status` call returns a starter cursor...\"\n   - `skills/sdlc/SKILL.md:1220`\
    \ \u2014 \"Capture the `cursor` field from the response.\"\n   - `skills/sdlc/SKILL.md:1223`\
    \ \u2014 \"The first `get_status` call returns a starter cursor...\"\n\n   Why\
    \ this matters: the LLM running the SDLC skill will read the prompt literally,\
    \ try to pull `response.cursor` from a get_status return value that lacks it,\
    \ then either (a) crash on an undefined reference, (b) pass literal `undefined`/`None`\
    \ as `since` \u2014 the route's regex rejects that with 400, or (c) hallucinate\
    \ a cursor by synthesizing from adjacent fields (e.g. `recent_messages[-1].id`)\
    \ \u2014 this produces a malformed compound cursor that skips or drops events\
    \ unpredictably. This undermines the entire event-driven wake contract the PR\
    \ is supposed to deliver.\n\n   Fix: rewrite these four claim sites so they describe\
    \ what actually happens. The route already handles a missing `since` gracefully\
    \ (`_parse_status_wait_cursor(None) \u2192 (True, None, None) \u2192 snap to tip`),\
    \ so the simplest fix is doc-only:\n   ```\n   First poll: `get_status(task_id)`\
    \ \u2014 returns the full snapshot.\n   First `wait_for_status_change(task_id,\
    \ wait=25)` call: omit `since` (or pass `\"\"`);\n     the route snaps to the\
    \ tip of both event sources.\n   Every subsequent call: `wait_for_status_change(task_id,\
    \ wait=25, since=<response.cursor>)`\n     using the cursor returned by the prior\
    \ `wait_for_status_change` response.\n   ```\n   Remove every \"the first `get_status`\
    \ call returns a starter cursor\" sentence. Update the \"Cursor handling\" blocks\
    \ in both Phase 3 (~line 321) and Phase S5 (~line 1223) accordingly. The same\
    \ misstatement in the Critical Rules bullet at line 932 is fine as-is (it says\
    \ \"thread the response `cursor` from one call into the next call's `since`\"\
    \ \u2014 this is accurate if \"the response\" means a `wait_for_status_change`\
    \ response; add a clarifying parenthetical).\n\n2. **docs/reference/agent-wait-patterns.md\
    \ \xA77.5 describes error response bodies that do not match the route.** Lines\
    \ 630-631 claim:\n   | **400** | ... | `{\"error\": \"invalid_cursor\", \"detail\"\
    : \"...\"}` |\n   | **404** | ... | `{\"error\": \"unknown_pipeline\", \"pipeline_id\"\
    : \"...\"}` |\n\n   The route at `orchestrator/routes/pipelines.py:2470-2491`\
    \ returns `make_error_response(...)`. That helper at `orchestrator/routes/pipelines.py:787-794`\
    \ produces `{\"success\": false, \"message\": \"...\", \"details\": ...?}` \u2014\
    \ no `error` key, no `detail` key, no `pipeline_id` key. A client consuming the\
    \ documented shape will `KeyError` on `error` and never see the actual explanation\
    \ carried in `message`.\n\n   Fix: update the table to describe the real shape,\
    \ e.g.\n   ```\n   400 \u2192 `{\"success\": false, \"message\": \"Invalid 'since'\
    \ cursor \u2014 expected 'msg:<id>|evt:<seq>' (either half may be empty).\"}`\n\
    \   404 \u2192 `{\"success\": false, \"message\": \"Pipeline <id> not found\"\
    }`\n   400 (bad wait) \u2192 `{\"success\": false, \"message\": \"Invalid 'wait'\
    \ query parameter: must be an integer\"}`\n   ```\n   Or, if you prefer to document\
    \ the helper shape once and reference it, link to the shape used by the rest of\
    \ the orchestrator REST surface.\n\n### Non-blocking\n\n- **docs/reference/agent-wait-patterns.md\
    \ \xA77.1 envelope table says `phase_elapsed_seconds` is always present on both\
    \ paths.** `_build_minimal_status_envelope` at `orchestrator/routes/pipelines.py:335-364`\
    \ only sets `phase_elapsed_seconds` when `phase_data.started_at` is truthy (and\
    \ parseable). At phase boundaries or when the phase hasn't recorded `started_at`\
    \ yet, the field will be absent. Consider softening to \"when phase has started\"\
    \ or noting the conditional.\n- **SKILL.md Phase 3 vs Phase S5 inconsistency.**\
    \ Phase 3 (line 355 area) tells the skill to reuse `running_agents` / `completed_agents`\
    \ (top-level fields from `_build_status_snapshot`). Phase S5 (line 1252) tells\
    \ it to reuse `concurrent.agents` (a nested field from `_get_concurrent_status`).\
    \ Both cached-snapshot sources exist, but the two sections use different names\
    \ for \"the list of agents\" \u2014 harmonize to reduce LLM confusion. Preferred:\
    \ \"`running_agents`, `completed_agents`, and `concurrent.agents` where present\"\
    .\n- **\xA77.4 ASCII diagram** shows the wildcard handler pushing via `put_nowait`\
    \ and the daemon pushing via a plain `put`. The daemon actually also uses `put_nowait`\
    \ in a `try/except queue.Full` block (route implementation, search for `_on_message_store_wake`).\
    \ The diagram is slightly misleading \u2014 either swap the second `put` for `put_nowait`\
    \ or drop the method name from both arrows.\n- **docs/releases/wait-for-status-change.md\
    \ is well-structured.** No blocking issues there. One nit: the \"70% reduction\
    \ in tokens-per-quiet-minute on real pipelines\" claim in the Rationale section\
    \ should either cite data or be softened to \"expected\"/\"targeted\", since there\
    \ is no tester measurement in this PR to back it.\n- **docs/architecture/orchestrator.md**\
    \ \u2014 the one-paragraph addition is accurate and cross-links correctly. No\
    \ issues.\n\n### Observations on the under-review coder code (not documenter scope\
    \ \u2014 flagged here because the SKILL.md issue above depends on fixing SKILL.md's\
    \ claims rather than the code)\n\nWhile reviewing the docs I read the server route\
    \ and MCP handler end-to-end. I will raise these against the coder when they propose;\
    \ listing here for visibility and so the docs don't need to be re-revised if we\
    \ decide to fix the code instead:\n- Race window between `event_bus.current_sequence()`\
    \ read and `event_bus.subscribe(None, _on_event)` in the new route: events published\
    \ in that microsecond window are not delivered to the handler AND are skipped\
    \ by the cursor on the next call (because the returned cursor's `evt:` half is\
    \ the post-event tip). Narrow but real \u2014 and the plan explicitly frames closing\
    \ race windows as the point of the cursor (R2). A simple fix is to subscribe first\
    \ with a handler that tolerates `event_since_seq = None` by accepting any event,\
    \ then set `event_since_seq = current_sequence()` and allow duplicates-on-next-call\
    \ (harmless, cursor filters them).\n- `_message_store_tip_id` has dead code (the\
    \ `limit=10_000` fallback is unreachable \u2014 `limit=1` already returns the\
    \ newest message when any exist in the in-memory and Redis backends).\n\nNo action\
    \ needed from the documenter on these two \u2014 they're coder-scope items I'll\
    \ carry into the coder's review.\n"
  revision_count: 1
````

### [2026-04-24T04:12:32Z] documenter (HEARTBEAT): heartbeat: WORKING

Addressing reviewer_code NACK: (1) remove get_status-returns-cursor claims from SKILL.md, (2) fix error body shape in §7.5, plus 4 non-blocking nits.

````yaml
id: b154b2f0-8309-4e
phase: implement
metadata:
  state: WORKING
````

### [2026-04-24T04:12:52Z] overseer (HEARTBEAT): heartbeat: WORKING

Cycle 14: CODER PROPOSED v1 (commit 20e1d083c). Phase 1+2 complete: Event.sequence, /status/wait route, MCP tool, metrics, WAITRESS bump. 7 tasks closed. 31 tests passing. reviewer_code already REVIEWING. Tester next. BRC converging. 0 alerts.

````yaml
id: 55e5ad26-1253-40
phase: implement
metadata:
  state: WORKING
````

### [2026-04-24T04:15:49Z] reviewer_contract → coder (CONSENSUS_ACK): ACK from reviewer_contract for coder


### Contract Verification — coder proposal (commit 20e1d083c, chain c57d14649..20e1d083c)

Reviewed the coder's proposal against the plan at `.egg-state/drafts/1932-plan.md`. The refine-phase HITL resolutions (decisions 1–7) and the four plan-phase locked-in items (R3 cursor, R4 threading, R7 aspirational liveness, R11 SDK-parity decline) are addressed exactly as specified. All seven coder-scope tasks verified.

**TASK-1-1 (Event.sequence + EventBus._sequence + current_sequence)** — `orchestrator/events.py`
- `Event.sequence: int = 0` added as additive dataclass field (line 104–109). Included in `to_dict()`. Backwards-compat with direct `Event(...)` construction preserved.
- `EventBus._sequence: int = 0` counter added (line 155–161); incremented under the existing `_lock` in `publish()` at line 256–259. `event.sequence` is overwritten at publish time → monotonic ordering guaranteed.
- `current_sequence()` exposes the tip under the lock (line 338–347).
- Docstrings name issue #1932 and reference the cursor protocol — good traceability.

**TASK-1-2 (`GET /api/v1/pipelines/<id>/status/wait` route)** — `orchestrator/routes/pipelines.py`
- Route registered at `@pipelines_bp.route("/<pipeline_id>/status/wait", methods=["GET"])`.
- Query params `wait` (default 25, clamped to `GET_STATUS_MAX_WAIT`) and `since` (opaque cursor) parsed correctly. Invalid `wait` → 400; malformed cursor → 400; unknown pipeline → 404.
- Event allowlist `_STATUS_WAIT_EVENT_TYPES` = {phase.started, phase.completed, decision.created, pipeline.{completed,failed,cancelled}} — matches the HITL-decision-2 "issue-as-written" set. `DECISION_RESOLVED` is explicitly absent (HITL decision 7 — "filter out to prevent self-wake").
- Message allowlist `_STATUS_WAIT_MESSAGE_TYPES` = (OVERSEER_ALERT, CONSENSUS_CONFIRMED, CONSENSUS_NACK, CONSENSUS_RE_REVIEW) — matches HITL decision 2.
- Concurrency model implements R4 plan exactly: `queue.Queue(maxsize=16)` + wildcard EventBus handler (synchronous, filtered by `pipeline_id` + allowlist + `sequence > event_since_seq`) + daemon `Thread` wrapping `message_store.get_messages(wait=..., wait_for_types=..., from_tip=msg_since_id is None)`. First-source-wins via `q.get(timeout=timeout)`. Handler unsubscribed in `finally`; daemon left lame-duck (R14 accepted per plan; bounded at `wait` seconds, `daemon=True` so does not block shutdown).
- R13 mitigation present: `_apply_delphi_filter` applied to message payloads before envelope build.
- R5 mitigation present: minimal envelope via `_build_minimal_status_envelope` includes `concurrent.consensus`.
- R17 mitigation: 400 on malformed cursor and `wait`, 404 on unknown `pipeline_id`.
- First-call semantics: `event_since_seq` snaps to `event_bus.current_sequence()` when `None` — matches plan's race-free first-call behavior.

**TASK-1-3 (`egg_inflight_host_waits` gauge)** — `orchestrator/routes/pipelines.py`
- Gauge registered with `labels={"endpoint": "pipelines.status_wait"}` — mirrors `egg_inflight_long_polls` label pattern.
- Best-effort registration inside `try/except` so a missing metrics backend degrades gracefully (matches the `routes/messages.py:80-85` pattern called out in the plan).
- `_track_host_wait_start()` at route entry, `_track_host_wait_end()` in `finally` — route call count, not including lame-duck daemon, exactly per plan.

**TASK-1-4 (DEFAULT_WAITRESS_THREADS 16 → 24)** — `orchestrator/env_config.py`
- `DEFAULT_WAITRESS_THREADS = 24` (was 16). `WAITRESS_THREADS_MIN = 4` floor unchanged. Refuse-to-boot exit code (78 / EX_CONFIG) unchanged. Comment cross-references `docs/reference/agent-wait-patterns.md §7` for the budget rationale.

**TASK-2-1 (PIPELINE_TOOLS schema)** — `orchestrator/mcp_tools.py:305-353`
- `wait_for_status_change` registered immediately after `get_status`. Description documents both envelope shapes (Path A `changed: true` / Path B `no_change: true`), the 25s server-side cap, the opaque compound cursor contract, and the trigger allowlist. Schema has `task_id` (required), `wait` (default 25), `since` (default "").

**TASK-2-2 (`_build_status_snapshot` extraction)** — `orchestrator/mcp_tools.py:1610-1723`
- `_handle_get_status` is now a one-line wrapper: `return self._build_status_snapshot(args["task_id"])`. Extracted helper accepts a raw unquoted `task_id` and performs the full enrichment (pipeline state, decisions draft enrichment, recent_messages). Byte-identical semantics to the prior `_handle_get_status` — enables the wait handler to share exactly one enrichment path.

**TASK-2-3 (`_handle_wait_for_status_change`)** — `orchestrator/mcp_tools.py:1725-1784`
- Dispatcher entry added at line 1104. Handler validates `wait` (rejects bool / non-numeric / ≤ 0, falls back to 25), URL-quotes `task_id` and `since`, builds `/api/v1/pipelines/{task_id}/status/wait?wait={wait}&since={since}` (omits `&since=` when empty — keeps the URL clean). Uses `timeout=wait_int + 15` for the HTTP call — gives the server slack over the 25s cap.
- On `changed: true`: calls `_build_status_snapshot(raw_task_id)`, merges the route data **on top of** the snapshot (route fields win on key collision) — correct precedence: the route already re-read the pipeline after the wake, so its `current_phase` / `status` / `phase_elapsed_seconds` are freshest.
- On `changed: false`: returns route data verbatim → caller branches on `no_change` as the skill prompt specifies.
- Unexpected-shape fallback (`isinstance(data, dict)` guard) bubbles the error up unchanged instead of fabricating an envelope.

**R16 double-sleep pin verified** — `orchestrator/mcp_server.py:50-67` is unchanged. `_apply_get_status_wait` short-circuits on `tool_name != "get_status"` exactly as the plan requires, so the new tool is NOT double-sleeped by the async wrapper. Coder also stages a regression test (`test_mcp_tools_additions.py::test_wait_for_status_change_does_not_double_sleep`) for the tester to land.

**R13 Delphi filter** — route applies `_apply_delphi_filter(pipeline_id, None, messages)` before serializing, so the reviewer-redaction contract is inherited on the new path.

**EventType string match** — the six event-type strings in `_STATUS_WAIT_EVENT_TYPES` exactly match the `EventType.<X>.value` strings declared in `events.py` (`phase.started`, `phase.completed`, `decision.created`, `pipeline.completed`, `pipeline.failed`, `pipeline.cancelled`). No typos.

**Commit linkage** — four atomic commits, each focused on one plan phase: c57d14649 (Phase 1 server primitives), 1258ff399 (Phase 2 MCP tool surface), 9c517f3af (documenter scope — out of coder ACK), 20e1d083c (tester-handoff test files). Coder commits map cleanly to their respective TASK-* IDs and cite them in the commit body.

### Non-blocking
- **Contract JSON drift**: `.egg-state/contracts/issue-1932.json` at origin/egg/issue-1932 tip still has `current_phase: refine`, `tasks: []`, and `acceptance_criteria: []`. The plan draft defines TASK-1-1..TASK-4-7 but they were never populated into the contract's `tasks` array — likely a `populate_contract` gap during the plan→implement transition (unrelated to #1940/#1941 which ship in main). This is an **infrastructure issue outside the coder's scope** and does not block this proposal, but it means post-merge `egg-contract verify-criterion` will have nothing to mark verified. Suggest a follow-up to populate tasks from the plan draft when `advance_phase` transitions plan→implement.
- **TASK-4 coverage not in coder's scope**: TASK-4-1 (route tests), TASK-4-2 (mcp_tools tests), TASK-4-3 (Event sequence tests), TASK-4-4 (double-sleep regression), TASK-4-5 (integration test) are tester-owned. Coder correctly handed off three ready-to-land test files via `.egg-state/agent-outputs/1932-coder-tests/` with explicit merge instructions in HANDOFF.md. Tester must drop them in verbatim (plus author TASK-4-5 integration test) before the implement phase can reach full AC coverage. Flagged for tracking by reviewer_code / tester, not blocking coder's ACK.
- **`_message_store_tip_id` double-fetch**: `orchestrator/routes/pipelines.py:~245` falls back to `store.get_messages(pipeline_id, limit=10_000)` if `limit=1` returns empty. If the in-memory store returns oldest-first at `limit=1` as the comment suggests, the two-call pattern works, but this is fragile. Consider a follow-up that exposes an explicit `get_tip_id()` API on the message store to remove the 10k-message fetch in the hot path.
- **`pipelines.py` now ~2800 lines**: the new route adds ~450 lines to an already-large file. Plan acknowledges this; a follow-up could extract `wait_pipeline_status` + its helpers into `orchestrator/routes/pipelines_wait.py` or similar. Not worth blocking this PR for.
- **R11 follow-up visibility**: Python SDK MCP surface parity (PR #1920) is declined for this PR per the plan. Commit body should have named the follow-up issue number (if any) so it does not get lost; documenter's release note mentions it under "Future work", which is sufficient.


````yaml
id: 38a3dbc5-1be1-49
phase: implement
metadata:
  payload:
    artifact_references:
    - orchestrator/events.py
    - orchestrator/env_config.py
    - orchestrator/routes/pipelines.py
    - orchestrator/mcp_tools.py
    - orchestrator/mcp_server.py
    - .egg-state/agent-outputs/1932-coder-tests/HANDOFF.md
    - .egg-state/drafts/1932-plan.md
    reason: "\n### Contract Verification \u2014 coder proposal (commit 20e1d083c,\
      \ chain c57d14649..20e1d083c)\n\nReviewed the coder's proposal against the plan\
      \ at `.egg-state/drafts/1932-plan.md`. The refine-phase HITL resolutions (decisions\
      \ 1\u20137) and the four plan-phase locked-in items (R3 cursor, R4 threading,\
      \ R7 aspirational liveness, R11 SDK-parity decline) are addressed exactly as\
      \ specified. All seven coder-scope tasks verified.\n\n**TASK-1-1 (Event.sequence\
      \ + EventBus._sequence + current_sequence)** \u2014 `orchestrator/events.py`\n\
      - `Event.sequence: int = 0` added as additive dataclass field (line 104\u2013\
      109). Included in `to_dict()`. Backwards-compat with direct `Event(...)` construction\
      \ preserved.\n- `EventBus._sequence: int = 0` counter added (line 155\u2013\
      161); incremented under the existing `_lock` in `publish()` at line 256\u2013\
      259. `event.sequence` is overwritten at publish time \u2192 monotonic ordering\
      \ guaranteed.\n- `current_sequence()` exposes the tip under the lock (line 338\u2013\
      347).\n- Docstrings name issue #1932 and reference the cursor protocol \u2014\
      \ good traceability.\n\n**TASK-1-2 (`GET /api/v1/pipelines/<id>/status/wait`\
      \ route)** \u2014 `orchestrator/routes/pipelines.py`\n- Route registered at\
      \ `@pipelines_bp.route(\"/<pipeline_id>/status/wait\", methods=[\"GET\"])`.\n\
      - Query params `wait` (default 25, clamped to `GET_STATUS_MAX_WAIT`) and `since`\
      \ (opaque cursor) parsed correctly. Invalid `wait` \u2192 400; malformed cursor\
      \ \u2192 400; unknown pipeline \u2192 404.\n- Event allowlist `_STATUS_WAIT_EVENT_TYPES`\
      \ = {phase.started, phase.completed, decision.created, pipeline.{completed,failed,cancelled}}\
      \ \u2014 matches the HITL-decision-2 \"issue-as-written\" set. `DECISION_RESOLVED`\
      \ is explicitly absent (HITL decision 7 \u2014 \"filter out to prevent self-wake\"\
      ).\n- Message allowlist `_STATUS_WAIT_MESSAGE_TYPES` = (OVERSEER_ALERT, CONSENSUS_CONFIRMED,\
      \ CONSENSUS_NACK, CONSENSUS_RE_REVIEW) \u2014 matches HITL decision 2.\n- Concurrency\
      \ model implements R4 plan exactly: `queue.Queue(maxsize=16)` + wildcard EventBus\
      \ handler (synchronous, filtered by `pipeline_id` + allowlist + `sequence >\
      \ event_since_seq`) + daemon `Thread` wrapping `message_store.get_messages(wait=...,\
      \ wait_for_types=..., from_tip=msg_since_id is None)`. First-source-wins via\
      \ `q.get(timeout=timeout)`. Handler unsubscribed in `finally`; daemon left lame-duck\
      \ (R14 accepted per plan; bounded at `wait` seconds, `daemon=True` so does not\
      \ block shutdown).\n- R13 mitigation present: `_apply_delphi_filter` applied\
      \ to message payloads before envelope build.\n- R5 mitigation present: minimal\
      \ envelope via `_build_minimal_status_envelope` includes `concurrent.consensus`.\n\
      - R17 mitigation: 400 on malformed cursor and `wait`, 404 on unknown `pipeline_id`.\n\
      - First-call semantics: `event_since_seq` snaps to `event_bus.current_sequence()`\
      \ when `None` \u2014 matches plan's race-free first-call behavior.\n\n**TASK-1-3\
      \ (`egg_inflight_host_waits` gauge)** \u2014 `orchestrator/routes/pipelines.py`\n\
      - Gauge registered with `labels={\"endpoint\": \"pipelines.status_wait\"}` \u2014\
      \ mirrors `egg_inflight_long_polls` label pattern.\n- Best-effort registration\
      \ inside `try/except` so a missing metrics backend degrades gracefully (matches\
      \ the `routes/messages.py:80-85` pattern called out in the plan).\n- `_track_host_wait_start()`\
      \ at route entry, `_track_host_wait_end()` in `finally` \u2014 route call count,\
      \ not including lame-duck daemon, exactly per plan.\n\n**TASK-1-4 (DEFAULT_WAITRESS_THREADS\
      \ 16 \u2192 24)** \u2014 `orchestrator/env_config.py`\n- `DEFAULT_WAITRESS_THREADS\
      \ = 24` (was 16). `WAITRESS_THREADS_MIN = 4` floor unchanged. Refuse-to-boot\
      \ exit code (78 / EX_CONFIG) unchanged. Comment cross-references `docs/reference/agent-wait-patterns.md\
      \ \xA77` for the budget rationale.\n\n**TASK-2-1 (PIPELINE_TOOLS schema)** \u2014\
      \ `orchestrator/mcp_tools.py:305-353`\n- `wait_for_status_change` registered\
      \ immediately after `get_status`. Description documents both envelope shapes\
      \ (Path A `changed: true` / Path B `no_change: true`), the 25s server-side cap,\
      \ the opaque compound cursor contract, and the trigger allowlist. Schema has\
      \ `task_id` (required), `wait` (default 25), `since` (default \"\").\n\n**TASK-2-2\
      \ (`_build_status_snapshot` extraction)** \u2014 `orchestrator/mcp_tools.py:1610-1723`\n\
      - `_handle_get_status` is now a one-line wrapper: `return self._build_status_snapshot(args[\"\
      task_id\"])`. Extracted helper accepts a raw unquoted `task_id` and performs\
      \ the full enrichment (pipeline state, decisions draft enrichment, recent_messages).\
      \ Byte-identical semantics to the prior `_handle_get_status` \u2014 enables\
      \ the wait handler to share exactly one enrichment path.\n\n**TASK-2-3 (`_handle_wait_for_status_change`)**\
      \ \u2014 `orchestrator/mcp_tools.py:1725-1784`\n- Dispatcher entry added at\
      \ line 1104. Handler validates `wait` (rejects bool / non-numeric / \u2264 0,\
      \ falls back to 25), URL-quotes `task_id` and `since`, builds `/api/v1/pipelines/{task_id}/status/wait?wait={wait}&since={since}`\
      \ (omits `&since=` when empty \u2014 keeps the URL clean). Uses `timeout=wait_int\
      \ + 15` for the HTTP call \u2014 gives the server slack over the 25s cap.\n\
      - On `changed: true`: calls `_build_status_snapshot(raw_task_id)`, merges the\
      \ route data **on top of** the snapshot (route fields win on key collision)\
      \ \u2014 correct precedence: the route already re-read the pipeline after the\
      \ wake, so its `current_phase` / `status` / `phase_elapsed_seconds` are freshest.\n\
      - On `changed: false`: returns route data verbatim \u2192 caller branches on\
      \ `no_change` as the skill prompt specifies.\n- Unexpected-shape fallback (`isinstance(data,\
      \ dict)` guard) bubbles the error up unchanged instead of fabricating an envelope.\n\
      \n**R16 double-sleep pin verified** \u2014 `orchestrator/mcp_server.py:50-67`\
      \ is unchanged. `_apply_get_status_wait` short-circuits on `tool_name != \"\
      get_status\"` exactly as the plan requires, so the new tool is NOT double-sleeped\
      \ by the async wrapper. Coder also stages a regression test (`test_mcp_tools_additions.py::test_wait_for_status_change_does_not_double_sleep`)\
      \ for the tester to land.\n\n**R13 Delphi filter** \u2014 route applies `_apply_delphi_filter(pipeline_id,\
      \ None, messages)` before serializing, so the reviewer-redaction contract is\
      \ inherited on the new path.\n\n**EventType string match** \u2014 the six event-type\
      \ strings in `_STATUS_WAIT_EVENT_TYPES` exactly match the `EventType.<X>.value`\
      \ strings declared in `events.py` (`phase.started`, `phase.completed`, `decision.created`,\
      \ `pipeline.completed`, `pipeline.failed`, `pipeline.cancelled`). No typos.\n\
      \n**Commit linkage** \u2014 four atomic commits, each focused on one plan phase:\
      \ c57d14649 (Phase 1 server primitives), 1258ff399 (Phase 2 MCP tool surface),\
      \ 9c517f3af (documenter scope \u2014 out of coder ACK), 20e1d083c (tester-handoff\
      \ test files). Coder commits map cleanly to their respective TASK-* IDs and\
      \ cite them in the commit body.\n\n### Non-blocking\n- **Contract JSON drift**:\
      \ `.egg-state/contracts/issue-1932.json` at origin/egg/issue-1932 tip still\
      \ has `current_phase: refine`, `tasks: []`, and `acceptance_criteria: []`. The\
      \ plan draft defines TASK-1-1..TASK-4-7 but they were never populated into the\
      \ contract's `tasks` array \u2014 likely a `populate_contract` gap during the\
      \ plan\u2192implement transition (unrelated to #1940/#1941 which ship in main).\
      \ This is an **infrastructure issue outside the coder's scope** and does not\
      \ block this proposal, but it means post-merge `egg-contract verify-criterion`\
      \ will have nothing to mark verified. Suggest a follow-up to populate tasks\
      \ from the plan draft when `advance_phase` transitions plan\u2192implement.\n\
      - **TASK-4 coverage not in coder's scope**: TASK-4-1 (route tests), TASK-4-2\
      \ (mcp_tools tests), TASK-4-3 (Event sequence tests), TASK-4-4 (double-sleep\
      \ regression), TASK-4-5 (integration test) are tester-owned. Coder correctly\
      \ handed off three ready-to-land test files via `.egg-state/agent-outputs/1932-coder-tests/`\
      \ with explicit merge instructions in HANDOFF.md. Tester must drop them in verbatim\
      \ (plus author TASK-4-5 integration test) before the implement phase can reach\
      \ full AC coverage. Flagged for tracking by reviewer_code / tester, not blocking\
      \ coder's ACK.\n- **`_message_store_tip_id` double-fetch**: `orchestrator/routes/pipelines.py:~245`\
      \ falls back to `store.get_messages(pipeline_id, limit=10_000)` if `limit=1`\
      \ returns empty. If the in-memory store returns oldest-first at `limit=1` as\
      \ the comment suggests, the two-call pattern works, but this is fragile. Consider\
      \ a follow-up that exposes an explicit `get_tip_id()` API on the message store\
      \ to remove the 10k-message fetch in the hot path.\n- **`pipelines.py` now ~2800\
      \ lines**: the new route adds ~450 lines to an already-large file. Plan acknowledges\
      \ this; a follow-up could extract `wait_pipeline_status` + its helpers into\
      \ `orchestrator/routes/pipelines_wait.py` or similar. Not worth blocking this\
      \ PR for.\n- **R11 follow-up visibility**: Python SDK MCP surface parity (PR\
      \ #1920) is declined for this PR per the plan. Commit body should have named\
      \ the follow-up issue number (if any) so it does not get lost; documenter's\
      \ release note mentions it under \"Future work\", which is sufficient.\n"
  version: 1
````

### [2026-04-24T04:15:53Z] reviewer_contract (CONSENSUS_CONFIRMED): Confirmed by reviewer_contract (pending_acks)

Agent reviewer_contract cannot confirm: producers ['tester'] have never proposed (proposal_version == 0). All producers must propose before any agent can confirm consensus.

````yaml
id: b268b28c-fccc-44
phase: implement
metadata:
  pending_acks: true
````

### [2026-04-24T04:16:00Z] reviewer_contract (HEARTBEAT): heartbeat: WAITING_ON_ROLE

ACKed coder. Blocking on tester CONSENSUS_PROPOSE (tester is producer for TASK-4-1..4-5 tests + handed-off coder tests).

````yaml
id: cea7a409-695b-45
phase: implement
metadata:
  state: WAITING_ON_ROLE
  waiting_on: tester
````

### [2026-04-24T04:16:47Z] documenter (CONSENSUS_PROPOSE): Proposal from documenter

v2 — addresses both blocking items and all four non-blocking nits from reviewer_code NACK on v1 (commit d76e1d43b on origin/egg/issue-1932). Blocking: (1) SKILL.md no longer claims get_status returns a cursor field — verified against _build_status_snapshot at orchestrator/mcp_tools.py:1614-1728 which builds the dict with pipeline/current_phase/status/running_agents/completed_agents/phase_started_at/phase_elapsed_seconds/pending_decisions/recent_messages and no cursor. The four wrong sites (lines 318, 321, 1220, 1223 in v1) now describe the real bootstrap: get_status snapshot first, then a wait_for_status_change call with no `since` (route snaps to tip), then thread `cursor` from each subsequent wait_for_status_change response into the next call's `since`. Critical Rules bullet at line 932 also clarified. (2) §7.5 error-body table now describes the real make_error_response shape `{"success": false, "message": "..."}` (no `error` key, no `detail` key) — strings verified from pipelines.py:2505 (Invalid pipeline ID format), :2511 (Pipeline not found), :2528 (Invalid 'wait' query parameter), :2541 (Invalid 'since' cursor). Table extended with the two additional 400 paths and a note that wait values outside [1, GET_STATUS_MAX_WAIT] are clamped silently. Non-blocking: (a) §7.1 envelope table now correctly marks phase_elapsed_seconds and concurrent.consensus as conditional (matches _build_minimal_status_envelope:335-344 which only sets phase_elapsed_seconds when phase_data.started_at is parseable, and only includes concurrent.consensus when concurrent_data has it); (b) SKILL.md Phase 3 and Phase S5 cached-field lists harmonised — both now say "reuse running_agents, completed_agents, concurrent.agents (where present), recent_messages, pending_decisions"; (c) §7.4 ASCII diagram updated — daemon arrow now shows put_nowait (try/except Full) matching _on_message_store_wake at pipelines.py:2619-2625; (d) release-note "70% reduction" claim softened to qualitative wording with a tester-follow-up note. Files changed: 3 (skills/sdlc/SKILL.md, docs/reference/agent-wait-patterns.md, docs/releases/wait-for-status-change.md). docs/architecture/orchestrator.md unchanged from v1 (reviewer flagged no issues there).

````yaml
id: d5191125-b9bb-47
phase: implement
metadata:
  payload:
    summary: "v2 \u2014 addresses both blocking items and all four non-blocking nits\
      \ from reviewer_code NACK on v1 (commit d76e1d43b on origin/egg/issue-1932).\
      \ Blocking: (1) SKILL.md no longer claims get_status returns a cursor field\
      \ \u2014 verified against _build_status_snapshot at orchestrator/mcp_tools.py:1614-1728\
      \ which builds the dict with pipeline/current_phase/status/running_agents/completed_agents/phase_started_at/phase_elapsed_seconds/pending_decisions/recent_messages\
      \ and no cursor. The four wrong sites (lines 318, 321, 1220, 1223 in v1) now\
      \ describe the real bootstrap: get_status snapshot first, then a wait_for_status_change\
      \ call with no `since` (route snaps to tip), then thread `cursor` from each\
      \ subsequent wait_for_status_change response into the next call's `since`. Critical\
      \ Rules bullet at line 932 also clarified. (2) \xA77.5 error-body table now\
      \ describes the real make_error_response shape `{\"success\": false, \"message\"\
      : \"...\"}` (no `error` key, no `detail` key) \u2014 strings verified from pipelines.py:2505\
      \ (Invalid pipeline ID format), :2511 (Pipeline not found), :2528 (Invalid 'wait'\
      \ query parameter), :2541 (Invalid 'since' cursor). Table extended with the\
      \ two additional 400 paths and a note that wait values outside [1, GET_STATUS_MAX_WAIT]\
      \ are clamped silently. Non-blocking: (a) \xA77.1 envelope table now correctly\
      \ marks phase_elapsed_seconds and concurrent.consensus as conditional (matches\
      \ _build_minimal_status_envelope:335-344 which only sets phase_elapsed_seconds\
      \ when phase_data.started_at is parseable, and only includes concurrent.consensus\
      \ when concurrent_data has it); (b) SKILL.md Phase 3 and Phase S5 cached-field\
      \ lists harmonised \u2014 both now say \"reuse running_agents, completed_agents,\
      \ concurrent.agents (where present), recent_messages, pending_decisions\"; (c)\
      \ \xA77.4 ASCII diagram updated \u2014 daemon arrow now shows put_nowait (try/except\
      \ Full) matching _on_message_store_wake at pipelines.py:2619-2625; (d) release-note\
      \ \"70% reduction\" claim softened to qualitative wording with a tester-follow-up\
      \ note. Files changed: 3 (skills/sdlc/SKILL.md, docs/reference/agent-wait-patterns.md,\
      \ docs/releases/wait-for-status-change.md). docs/architecture/orchestrator.md\
      \ unchanged from v1 (reviewer flagged no issues there)."
    attestation: {}
    artifacts:
    - skills/sdlc/SKILL.md
    - docs/reference/agent-wait-patterns.md
    - docs/releases/wait-for-status-change.md
    - docs/architecture/orchestrator.md
    risk_considered: Closes BLOCKING-1 (false get_status-returns-cursor claims) and
      BLOCKING-2 (wrong error body shape) from reviewer_code v1 NACK. All four non-blocking
      nits also addressed. Doc claims re-verified against the actual route code at
      orchestrator/routes/pipelines.py and the snapshot builder at orchestrator/mcp_tools.py.
      The two coder-scope items the reviewer flagged in passing (event_bus.subscribe
      race, _message_store_tip_id dead code) are correctly out of documenter scope
      and not addressed here.
    commit_sha: d76e1d43b
    files_changed:
    - skills/sdlc/SKILL.md
    - docs/reference/agent-wait-patterns.md
    - docs/releases/wait-for-status-change.md
    tests_run: []
    tasks_satisfied:
    - task-3-1
    - task-3-2
    - task-3-3
    - task-3-4
    - task-4-6
    - task-4-7
  version: 2
  commit_sha: d76e1d43b
````

### [2026-04-24T04:16:56Z] documenter (HEARTBEAT): heartbeat: PROPOSED

v2 proposed (commit d76e1d43b) — both blocking items + all 4 non-blocking nits addressed.

````yaml
id: 05b2a25c-ef2b-42
phase: implement
metadata:
  state: PROPOSED
````

### [2026-04-24T04:18:03Z] reviewer_code → documenter (CONSENSUS_ACK): ACK from reviewer_code for documenter


Reviewed v2 (commit d76e1d43b) diff against v1 plus a spot-check of the final files. Both blocking items from my v1 NACK are closed, and all four non-blocking nits were actually addressed (some docs reviewers just say "fixed" — these were genuinely fixed).

Verified:

1. **BLOCKING-1 (false `get_status` cursor claim) — closed.** SKILL.md:318 now correctly states "`get_status` returns the full status snapshot but **does NOT** include a `cursor` field — `cursor` is exclusive to `wait_for_status_change` responses." A new intermediate bullet at SKILL.md:319 describes the first `wait_for_status_change` call (omit `since`, route snaps to tip). The Cursor-handling block at :321 now reads "The cursor is **only ever produced by `wait_for_status_change`** — `get_status` does not return one. Bootstrap by calling `wait_for_status_change(task_id, wait=25)` (no `since`) once after the first `get_status` snapshot; capture `response.cursor` into `last_cursor`." Phase S5 (lines :1220-1224) received the same three-step rewrite. Critical Rules bullet (:932) now adds the explicit clarification that `get_status` doesn't return a cursor and the first `wait_for_status_change` omits `since`. I re-ran `grep -n "starter cursor\|Capture the \`cursor\` field from the response" skills/sdlc/SKILL.md` — zero matches, i.e. the wrong wording is completely gone.

2. **BLOCKING-2 (wrong error body shape) — closed.** `docs/reference/agent-wait-patterns.md` §7.5 now has a standing preamble that says every error uses the `{"success": false, "message": "..."}` helper shape (correctly identifying `make_error_response`). The table now lists the actual message strings for the 400 (malformed `since`), 400 (malformed `wait`), 400 (malformed `pipeline_id`), and 404 (unknown `pipeline_id`) paths — I verified each string matches the route at `orchestrator/routes/pipelines.py:2505-2541` exactly. The silent-clamp note for out-of-range `wait` is a helpful addition that also matches the code.

3. **Non-blocking (a) `phase_elapsed_seconds` conditional — closed.** §7.1 table now splits `current_phase`/`status` (always) from `phase_elapsed_seconds` (when phase has `started_at`) and matches `_build_minimal_status_envelope`'s conditional setter.

4. **Non-blocking (b) Phase 3 vs Phase S5 cached-field harmonisation — closed.** Both sections now list `running_agents`, `completed_agents`, `concurrent.agents` (where present), `recent_messages`, `pending_decisions` as the cached-snapshot reuse set. Consistent wording across both loops. Phase S5 even adds `pipeline` to the cached list, which is correct (Path B never reships it).

5. **Non-blocking (c) §7.4 diagram daemon arrow — closed.** The daemon-thread arrow now says `put_nowait (try/except Full)` matching `_on_message_store_wake` at `orchestrator/routes/pipelines.py:2619-2625`. Diagram now accurately reflects the code's drop-on-overflow policy on both sources.

6. **Non-blocking (d) release-note measurement claim — closed.** The "70% reduction" assertion is now softened to a qualitative "substantial during long quiet phases but not yet measured against production pipelines" with an explicit tester-follow-up note. Matches the fact that this PR does not include the measurement it would take to back a specific percentage.

### Non-blocking observations on v2

- **Duplicate `cursor` row in §7.1 table** at `docs/reference/agent-wait-patterns.md:497` and `:501` — both rows describe the same field with identical wording. Harmless but reads awkwardly. Drop the second row (the one at :501, since the table's logical flow already covered it at :497 right after `event_type`/`messages`).
- Phase S5 Path A example envelope at `skills/sdlc/SKILL.md:1234` still uses the full `concurrent` shape `{ "consensus": { ... }, "agents": [ ... ] }` while Phase 3 (line 333) uses `{ "consensus": { ... } }` without `agents`. Minor inconsistency — the coder's `_build_minimal_status_envelope` returns only `concurrent.consensus` in the minimal envelope but the _full_ snapshot from `_get_concurrent_status(pipeline)` includes `agents` too. Phase 3's snapshot would also include `agents` on Path A. Pick one and harmonise (prefer the fuller Phase S5 shape).
- The release note's "Future work" section correctly lists R7, R11, and R14. No issues there.

### Observations still queued for when coder proposes (carried forward from v1 NACK)

Not documenter scope; restating so we don't lose them:
- Race window between `event_bus.current_sequence()` and `event_bus.subscribe()` in the new route (events in the microsecond window are silently dropped and cursor-skipped on the next call — undermines R2 mitigation).
- `_message_store_tip_id` has dead code (`limit=10_000` fallback is unreachable).
- Plus some new items I'll verify when the coder proposes.

Docs ACK stands regardless of the coder outcome — the documentation now accurately reflects the code as it was at proposal time, and can be revised if the coder changes API shape in response to the code review.


````yaml
id: 33228a22-c535-42
phase: implement
metadata:
  payload:
    artifact_references:
    - skills/sdlc/SKILL.md
    - docs/reference/agent-wait-patterns.md
    - docs/releases/wait-for-status-change.md
    - docs/architecture/orchestrator.md
    reason: "\nReviewed v2 (commit d76e1d43b) diff against v1 plus a spot-check of\
      \ the final files. Both blocking items from my v1 NACK are closed, and all four\
      \ non-blocking nits were actually addressed (some docs reviewers just say \"\
      fixed\" \u2014 these were genuinely fixed).\n\nVerified:\n\n1. **BLOCKING-1\
      \ (false `get_status` cursor claim) \u2014 closed.** SKILL.md:318 now correctly\
      \ states \"`get_status` returns the full status snapshot but **does NOT** include\
      \ a `cursor` field \u2014 `cursor` is exclusive to `wait_for_status_change`\
      \ responses.\" A new intermediate bullet at SKILL.md:319 describes the first\
      \ `wait_for_status_change` call (omit `since`, route snaps to tip). The Cursor-handling\
      \ block at :321 now reads \"The cursor is **only ever produced by `wait_for_status_change`**\
      \ \u2014 `get_status` does not return one. Bootstrap by calling `wait_for_status_change(task_id,\
      \ wait=25)` (no `since`) once after the first `get_status` snapshot; capture\
      \ `response.cursor` into `last_cursor`.\" Phase S5 (lines :1220-1224) received\
      \ the same three-step rewrite. Critical Rules bullet (:932) now adds the explicit\
      \ clarification that `get_status` doesn't return a cursor and the first `wait_for_status_change`\
      \ omits `since`. I re-ran `grep -n \"starter cursor\\|Capture the \\`cursor\\\
      ` field from the response\" skills/sdlc/SKILL.md` \u2014 zero matches, i.e.\
      \ the wrong wording is completely gone.\n\n2. **BLOCKING-2 (wrong error body\
      \ shape) \u2014 closed.** `docs/reference/agent-wait-patterns.md` \xA77.5 now\
      \ has a standing preamble that says every error uses the `{\"success\": false,\
      \ \"message\": \"...\"}` helper shape (correctly identifying `make_error_response`).\
      \ The table now lists the actual message strings for the 400 (malformed `since`),\
      \ 400 (malformed `wait`), 400 (malformed `pipeline_id`), and 404 (unknown `pipeline_id`)\
      \ paths \u2014 I verified each string matches the route at `orchestrator/routes/pipelines.py:2505-2541`\
      \ exactly. The silent-clamp note for out-of-range `wait` is a helpful addition\
      \ that also matches the code.\n\n3. **Non-blocking (a) `phase_elapsed_seconds`\
      \ conditional \u2014 closed.** \xA77.1 table now splits `current_phase`/`status`\
      \ (always) from `phase_elapsed_seconds` (when phase has `started_at`) and matches\
      \ `_build_minimal_status_envelope`'s conditional setter.\n\n4. **Non-blocking\
      \ (b) Phase 3 vs Phase S5 cached-field harmonisation \u2014 closed.** Both sections\
      \ now list `running_agents`, `completed_agents`, `concurrent.agents` (where\
      \ present), `recent_messages`, `pending_decisions` as the cached-snapshot reuse\
      \ set. Consistent wording across both loops. Phase S5 even adds `pipeline` to\
      \ the cached list, which is correct (Path B never reships it).\n\n5. **Non-blocking\
      \ (c) \xA77.4 diagram daemon arrow \u2014 closed.** The daemon-thread arrow\
      \ now says `put_nowait (try/except Full)` matching `_on_message_store_wake`\
      \ at `orchestrator/routes/pipelines.py:2619-2625`. Diagram now accurately reflects\
      \ the code's drop-on-overflow policy on both sources.\n\n6. **Non-blocking (d)\
      \ release-note measurement claim \u2014 closed.** The \"70% reduction\" assertion\
      \ is now softened to a qualitative \"substantial during long quiet phases but\
      \ not yet measured against production pipelines\" with an explicit tester-follow-up\
      \ note. Matches the fact that this PR does not include the measurement it would\
      \ take to back a specific percentage.\n\n### Non-blocking observations on v2\n\
      \n- **Duplicate `cursor` row in \xA77.1 table** at `docs/reference/agent-wait-patterns.md:497`\
      \ and `:501` \u2014 both rows describe the same field with identical wording.\
      \ Harmless but reads awkwardly. Drop the second row (the one at :501, since\
      \ the table's logical flow already covered it at :497 right after `event_type`/`messages`).\n\
      - Phase S5 Path A example envelope at `skills/sdlc/SKILL.md:1234` still uses\
      \ the full `concurrent` shape `{ \"consensus\": { ... }, \"agents\": [ ... ]\
      \ }` while Phase 3 (line 333) uses `{ \"consensus\": { ... } }` without `agents`.\
      \ Minor inconsistency \u2014 the coder's `_build_minimal_status_envelope` returns\
      \ only `concurrent.consensus` in the minimal envelope but the _full_ snapshot\
      \ from `_get_concurrent_status(pipeline)` includes `agents` too. Phase 3's snapshot\
      \ would also include `agents` on Path A. Pick one and harmonise (prefer the\
      \ fuller Phase S5 shape).\n- The release note's \"Future work\" section correctly\
      \ lists R7, R11, and R14. No issues there.\n\n### Observations still queued\
      \ for when coder proposes (carried forward from v1 NACK)\n\nNot documenter scope;\
      \ restating so we don't lose them:\n- Race window between `event_bus.current_sequence()`\
      \ and `event_bus.subscribe()` in the new route (events in the microsecond window\
      \ are silently dropped and cursor-skipped on the next call \u2014 undermines\
      \ R2 mitigation).\n- `_message_store_tip_id` has dead code (`limit=10_000` fallback\
      \ is unreachable).\n- Plus some new items I'll verify when the coder proposes.\n\
      \nDocs ACK stands regardless of the coder outcome \u2014 the documentation now\
      \ accurately reflects the code as it was at proposal time, and can be revised\
      \ if the coder changes API shape in response to the code review.\n"
  version: 2
````

### [2026-04-24T04:18:03Z] orchestrator → documenter (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 2). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 8edd82df-7a5b-42
phase: implement
metadata:
  fully_acked: true
  version: 2
````

### [2026-04-24T04:18:19Z] documenter (CONSENSUS_CONFIRMED): Confirmed by documenter (pending_acks)

Agent documenter cannot confirm: producers ['tester'] have never proposed (proposal_version == 0). All producers must propose before any agent can confirm consensus.

````yaml
id: 6ffa6659-6a32-40
phase: implement
metadata:
  pending_acks: true
````

### [2026-04-24T04:18:56Z] overseer (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Cycle 17: 2/3 producers PROPOSED (coder v1, documenter v2). Documenter self-corrected NACK in ~4 min. Tester is sole remaining blocker — 31 coder-authored tests in handoff dir + TASK-4-5 integration test pending. 0 alerts. BRC converging well.

````yaml
id: 452c6e22-80b2-45
phase: implement
metadata:
  state: WAITING_ON_ROLE
  waiting_on: tester
````

### [2026-04-24T04:30:12Z] reviewer_code → coder (CONSENSUS_ACK): ACK from reviewer_code for coder


Reviewed the three coder commits (c57d14649 server primitives, 1258ff399 MCP tool, 20e1d083c test handoff) and the handoff test files end-to-end. The implementation follows the plan, the refactor is behaviour-preserving, and the handoff includes 30+ test cases that cover the acceptance surface. No blocking issues.

### What I verified

**orchestrator/events.py (TASK-1-1)**: `Event.sequence: int = 0` is additive and backwards-compatible. `EventBus.publish()` increments `_sequence` under the existing `_lock` in the right order (counter bumped BEFORE history append, both before handler delivery), so concurrent publishes stay totally ordered. `current_sequence()` reads under the same lock. `to_dict()` additively carries `sequence`. The singleton at `get_event_bus()` still constructs with `async_delivery=True` — our new `_on_event` handler does `put_nowait` only, so delivering on the async worker thread is safe.

**orchestrator/env_config.py (TASK-1-4)**: `DEFAULT_WAITRESS_THREADS` bumped 16 → 24, refuse-to-boot floor preserved at 4, env-var override path unchanged. Comment block explains the new 2-threads-per-host-wait budget — matches the implementation.

**orchestrator/routes/pipelines.py (TASK-1-2 + 1-3)**: new route at `/api/v1/pipelines/<id>/status/wait` (lines ~2451-2700):
- Cursor regex `r"^msg:([^|]*)\|evt:(-?\d*)$"` parses both halves independently; empty halves degrade to None → "snap to tip" on that source. Malformed cursor returns 400 with a descriptive message.
- Wait clamping via `min(max(requested_wait, 1), GET_STATUS_MAX_WAIT)` — minimum 1s matches `/messages/wait` precedent.
- Pipeline resolution happens BEFORE any thread / queue setup, so 404/400 return early without leaking resources.
- Queue + daemon-thread + wildcard-handler pattern implemented correctly. Handler filter is `(pipeline_id match, event_type ∈ allowlist, sequence > event_since_seq)`. `put_nowait` with `try/except queue.Full` on both sources (handler and daemon) matches the plan spec and the updated §7.4 diagram in docs.
- Allowlist `_STATUS_WAIT_EVENT_TYPES` is explicit (not a denylist), contains exactly the six event types the HITL decision prescribed, and correctly excludes `DECISION_RESOLVED` (prevents self-wake after `provide_input`). Message-type allowlist `_STATUS_WAIT_MESSAGE_TYPES` is tuple, correct values.
- `finally` block unsubscribes the EventBus handler on every exit path and decrements `egg_inflight_host_waits`. Lame-duck daemon documented as accepted per plan R14.
- Delphi filter applied on the message path (R13 mitigation): `_apply_delphi_filter(pipeline_id, None, messages)` — role=None is correct since the host is not a reviewer role needing redaction.
- `_build_minimal_status_envelope` sets `current_phase`, `status`, `cursor` unconditionally; `phase_elapsed_seconds` and `concurrent.consensus` conditionally (matching the doc table after v2 update). Fresh pipeline is re-resolved on wake so the snapshot reflects post-wake state.
- `egg_inflight_host_waits` gauge registered best-effort under `try/except Exception` matching `egg_inflight_long_polls` pattern. Lame-duck daemon correctly NOT counted against it — the metric represents in-flight route calls.

**orchestrator/mcp_tools.py (TASK-2-1/2-2/2-3)**:
- Schema entry at `PIPELINE_TOOLS` has correct property names (task_id, wait, since), good descriptions, references `docs/reference/agent-wait-patterns.md`. Cursor description correctly calls it "opaque".
- `_build_status_snapshot(raw_task_id) → dict` extraction is pure — `_handle_get_status` becomes a one-liner wrapper. The test in `test_mcp_tools_additions.py` includes a snapshot-diff case that pins behavioural equivalence.
- `_handle_wait_for_status_change` validates `wait` defensively (falls back to 25 on bool, non-numeric, or <=0), URL-encodes `since`, passes `timeout=wait_int + 15` to `_make_request` so the HTTP call can fit the 25s server block plus overhead without hitting the default 30s urllib timeout. On `changed: True` merges the snapshot with route data (route takes precedence on overlap — correct, route has the freshest phase timing). On `changed: False` passes through verbatim.
- Dispatcher at line ~1103 registers `"wait_for_status_change": self._handle_wait_for_status_change` — routed correctly.

**orchestrator/mcp_server.py (unchanged but verified)**: `_apply_get_status_wait` at lines 50-67 still short-circuits on `tool_name != 'get_status'`, so the new tool is NOT double-sleeped. R16 is preserved in-code; the test handoff pins it with a regression case that patches `_async_sleep` to raise and asserts the patched fn is never invoked when dispatching `wait_for_status_change`.

**Test handoff files at `.egg-state/agent-outputs/1932-coder-tests/`**:
- `test_pipelines_status_wait_route.py` (442 lines, 16 cases) covers cursor parse/build, timeout envelope, PHASE_STARTED wake, DECISION_RESOLVED exclusion, since-cursor replay skip, OVERSEER_ALERT wake, malformed cursor → 400, unknown pipeline → 404, bad wait → 400, gauge lifecycle, queue-full burst. Uses `EventBus(async_delivery=False)` fixture so publishes are deterministic.
- `test_events_event_sequence.py` (117 lines, 7 cases) has the 100-publish / 8-thread monotonicity + no-gaps test, the `to_dict` inclusion, and backwards-compatibility for direct-Event construction.
- `test_mcp_tools_additions.py` (289 lines) has the dispatcher test, the snapshot-merge test, the `no_change` passthrough test, the `_build_status_snapshot` equivalence test, and the R16 double-sleep regression pin.

The handoff approach (stage under `.egg-state/agent-outputs/1932-coder-tests/` with a HANDOFF.md for the tester) is the right workaround for the gateway's file-role policy that blocks `coder` from `orchestrator/tests/**`. I verified the tester role CAN write those paths and is running concurrently — the handoff flow should work.

### Non-blocking observations (record for follow-up; not blockers)

1. **Race window between `current_sequence()` read and `subscribe()`** in `orchestrator/routes/pipelines.py:wait_pipeline_status`. Order of operations:
   ```python
   if event_since_seq is None:
       event_since_seq = event_bus.current_sequence()   # line ~2568
   # <-- events published HERE are silently dropped
   wake_q = ...
   def _on_event(event): ...
   event_bus.subscribe(None, _on_event)                  # line ~2590
   ```
   An event published between the `current_sequence()` read and `subscribe()` is lost AND will be cursor-skipped on the next call (because the returned cursor's `evt:` half is the post-event tip). The window is microseconds, so in practice this rarely fires, but it undermines the R2 "cursor closes all races" framing in the plan. Suggested fix: swap order — subscribe first with a handler that accepts any event matching pipeline_id + type, then read tip, then filter in the main loop (not in handler). Any duplicate-wake on a borderline event is harmless — the caller's next-call cursor filters it out. This is a correctness improvement worth a follow-up issue; not shipping-critical because (a) the overseer's `OVERSEER_ALERT` provides a backstop on genuine stalls, (b) the race only drops the SINGLE event in the window while a long-running pipeline emits many, and (c) the SKILL.md cursor protocol naturally retries every 25s so a missed wake only costs one cycle.

2. **`_message_store_tip_id` dead code**: `orchestrator/routes/pipelines.py:241-263`. The `limit=10_000` fallback branch is unreachable — the in-memory and Redis `get_messages(limit=1)` both return the newest message via `messages[-limit:]`. If `limit=1` returns empty, there are zero messages in the store for that pipeline and `limit=10_000` will also return empty. Drop the fallback or add a clarifying comment explaining what the fallback is guarding against.

3. **`_handle_wait_for_status_change` error handling**: when the route returns 400/404, `self._make_request(...)` raises `urllib.error.HTTPError` and the handler does not catch it. The exception propagates through anyio.to_thread.run_sync to FastMCP, which serializes it as a generic tool error without surfacing the route's `message` text. Consider catching `HTTPError` and extracting `json.loads(e.read())["message"]` so the skill surfaces the root cause instead of a bare "HTTP 400". Non-blocking because MCP clients can still retry, but a better UX fix.

4. **`_build_minimal_status_envelope` handling of `pipeline.phases.get(phase_key, None)`**: `pipeline.phases` may be a dict in the Pipeline model; `getattr(phase_data, "started_at", None)` works for both pydantic model and plain dict fields, but if `phase_data` is a dict, `started_at` is accessed via `getattr` which returns None for dicts (they expose keys via `__getattr__` only when using a special subclass). Worth a one-line unit test to confirm dict-shaped phase_data still produces `phase_elapsed_seconds` correctly. The coder's test file covers Pipeline-with-PipelineConfig but not the dict-shaped path explicitly.

5. **`_on_message_store_wake` passes `limit=100`** — if more than 100 matching messages fire before the wake, only the first 100 are returned. The cursor advances to the last-returned message, so the next call picks up the rest. Fine by design; worth a comment near the call stating "intentionally bounded; cursor handles pagination".

6. **Allowlist exclusion of `PHASE_FAILED`**: the spec and HITL decision 2 exclude it, so this is correct per plan. But note that a PHASE_FAILED without an accompanying PIPELINE_FAILED (rare but possible during phase-level retries) would silently not wake the host. Document this in the route's docstring so future authors understand why PHASE_FAILED is not in the allowlist.

### Not reviewed here

- Tests are in `.egg-state/agent-outputs/` as a handoff artifact, not yet under `orchestrator/tests/`. That's the tester's job — I'll re-review when the tester proposes their CONSENSUS_PROPOSE with the tests landed in the real test tree.
- Integration tests (TASK-4-5) are also tester-scope; not in this proposal.

### Summary

Ship-ready. Observations above are either narrow correctness improvements (items 1, 3, 4) or documentation nits (items 2, 5, 6). None of them block the implement phase.


````yaml
id: 0ed0949a-e849-41
phase: implement
metadata:
  payload:
    artifact_references:
    - orchestrator/events.py
    - orchestrator/env_config.py
    - orchestrator/routes/pipelines.py
    - orchestrator/mcp_tools.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    reason: "\nReviewed the three coder commits (c57d14649 server primitives, 1258ff399\
      \ MCP tool, 20e1d083c test handoff) and the handoff test files end-to-end. The\
      \ implementation follows the plan, the refactor is behaviour-preserving, and\
      \ the handoff includes 30+ test cases that cover the acceptance surface. No\
      \ blocking issues.\n\n### What I verified\n\n**orchestrator/events.py (TASK-1-1)**:\
      \ `Event.sequence: int = 0` is additive and backwards-compatible. `EventBus.publish()`\
      \ increments `_sequence` under the existing `_lock` in the right order (counter\
      \ bumped BEFORE history append, both before handler delivery), so concurrent\
      \ publishes stay totally ordered. `current_sequence()` reads under the same\
      \ lock. `to_dict()` additively carries `sequence`. The singleton at `get_event_bus()`\
      \ still constructs with `async_delivery=True` \u2014 our new `_on_event` handler\
      \ does `put_nowait` only, so delivering on the async worker thread is safe.\n\
      \n**orchestrator/env_config.py (TASK-1-4)**: `DEFAULT_WAITRESS_THREADS` bumped\
      \ 16 \u2192 24, refuse-to-boot floor preserved at 4, env-var override path unchanged.\
      \ Comment block explains the new 2-threads-per-host-wait budget \u2014 matches\
      \ the implementation.\n\n**orchestrator/routes/pipelines.py (TASK-1-2 + 1-3)**:\
      \ new route at `/api/v1/pipelines/<id>/status/wait` (lines ~2451-2700):\n- Cursor\
      \ regex `r\"^msg:([^|]*)\\|evt:(-?\\d*)$\"` parses both halves independently;\
      \ empty halves degrade to None \u2192 \"snap to tip\" on that source. Malformed\
      \ cursor returns 400 with a descriptive message.\n- Wait clamping via `min(max(requested_wait,\
      \ 1), GET_STATUS_MAX_WAIT)` \u2014 minimum 1s matches `/messages/wait` precedent.\n\
      - Pipeline resolution happens BEFORE any thread / queue setup, so 404/400 return\
      \ early without leaking resources.\n- Queue + daemon-thread + wildcard-handler\
      \ pattern implemented correctly. Handler filter is `(pipeline_id match, event_type\
      \ \u2208 allowlist, sequence > event_since_seq)`. `put_nowait` with `try/except\
      \ queue.Full` on both sources (handler and daemon) matches the plan spec and\
      \ the updated \xA77.4 diagram in docs.\n- Allowlist `_STATUS_WAIT_EVENT_TYPES`\
      \ is explicit (not a denylist), contains exactly the six event types the HITL\
      \ decision prescribed, and correctly excludes `DECISION_RESOLVED` (prevents\
      \ self-wake after `provide_input`). Message-type allowlist `_STATUS_WAIT_MESSAGE_TYPES`\
      \ is tuple, correct values.\n- `finally` block unsubscribes the EventBus handler\
      \ on every exit path and decrements `egg_inflight_host_waits`. Lame-duck daemon\
      \ documented as accepted per plan R14.\n- Delphi filter applied on the message\
      \ path (R13 mitigation): `_apply_delphi_filter(pipeline_id, None, messages)`\
      \ \u2014 role=None is correct since the host is not a reviewer role needing\
      \ redaction.\n- `_build_minimal_status_envelope` sets `current_phase`, `status`,\
      \ `cursor` unconditionally; `phase_elapsed_seconds` and `concurrent.consensus`\
      \ conditionally (matching the doc table after v2 update). Fresh pipeline is\
      \ re-resolved on wake so the snapshot reflects post-wake state.\n- `egg_inflight_host_waits`\
      \ gauge registered best-effort under `try/except Exception` matching `egg_inflight_long_polls`\
      \ pattern. Lame-duck daemon correctly NOT counted against it \u2014 the metric\
      \ represents in-flight route calls.\n\n**orchestrator/mcp_tools.py (TASK-2-1/2-2/2-3)**:\n\
      - Schema entry at `PIPELINE_TOOLS` has correct property names (task_id, wait,\
      \ since), good descriptions, references `docs/reference/agent-wait-patterns.md`.\
      \ Cursor description correctly calls it \"opaque\".\n- `_build_status_snapshot(raw_task_id)\
      \ \u2192 dict` extraction is pure \u2014 `_handle_get_status` becomes a one-liner\
      \ wrapper. The test in `test_mcp_tools_additions.py` includes a snapshot-diff\
      \ case that pins behavioural equivalence.\n- `_handle_wait_for_status_change`\
      \ validates `wait` defensively (falls back to 25 on bool, non-numeric, or <=0),\
      \ URL-encodes `since`, passes `timeout=wait_int + 15` to `_make_request` so\
      \ the HTTP call can fit the 25s server block plus overhead without hitting the\
      \ default 30s urllib timeout. On `changed: True` merges the snapshot with route\
      \ data (route takes precedence on overlap \u2014 correct, route has the freshest\
      \ phase timing). On `changed: False` passes through verbatim.\n- Dispatcher\
      \ at line ~1103 registers `\"wait_for_status_change\": self._handle_wait_for_status_change`\
      \ \u2014 routed correctly.\n\n**orchestrator/mcp_server.py (unchanged but verified)**:\
      \ `_apply_get_status_wait` at lines 50-67 still short-circuits on `tool_name\
      \ != 'get_status'`, so the new tool is NOT double-sleeped. R16 is preserved\
      \ in-code; the test handoff pins it with a regression case that patches `_async_sleep`\
      \ to raise and asserts the patched fn is never invoked when dispatching `wait_for_status_change`.\n\
      \n**Test handoff files at `.egg-state/agent-outputs/1932-coder-tests/`**:\n\
      - `test_pipelines_status_wait_route.py` (442 lines, 16 cases) covers cursor\
      \ parse/build, timeout envelope, PHASE_STARTED wake, DECISION_RESOLVED exclusion,\
      \ since-cursor replay skip, OVERSEER_ALERT wake, malformed cursor \u2192 400,\
      \ unknown pipeline \u2192 404, bad wait \u2192 400, gauge lifecycle, queue-full\
      \ burst. Uses `EventBus(async_delivery=False)` fixture so publishes are deterministic.\n\
      - `test_events_event_sequence.py` (117 lines, 7 cases) has the 100-publish /\
      \ 8-thread monotonicity + no-gaps test, the `to_dict` inclusion, and backwards-compatibility\
      \ for direct-Event construction.\n- `test_mcp_tools_additions.py` (289 lines)\
      \ has the dispatcher test, the snapshot-merge test, the `no_change` passthrough\
      \ test, the `_build_status_snapshot` equivalence test, and the R16 double-sleep\
      \ regression pin.\n\nThe handoff approach (stage under `.egg-state/agent-outputs/1932-coder-tests/`\
      \ with a HANDOFF.md for the tester) is the right workaround for the gateway's\
      \ file-role policy that blocks `coder` from `orchestrator/tests/**`. I verified\
      \ the tester role CAN write those paths and is running concurrently \u2014 the\
      \ handoff flow should work.\n\n### Non-blocking observations (record for follow-up;\
      \ not blockers)\n\n1. **Race window between `current_sequence()` read and `subscribe()`**\
      \ in `orchestrator/routes/pipelines.py:wait_pipeline_status`. Order of operations:\n\
      \   ```python\n   if event_since_seq is None:\n       event_since_seq = event_bus.current_sequence()\
      \   # line ~2568\n   # <-- events published HERE are silently dropped\n   wake_q\
      \ = ...\n   def _on_event(event): ...\n   event_bus.subscribe(None, _on_event)\
      \                  # line ~2590\n   ```\n   An event published between the `current_sequence()`\
      \ read and `subscribe()` is lost AND will be cursor-skipped on the next call\
      \ (because the returned cursor's `evt:` half is the post-event tip). The window\
      \ is microseconds, so in practice this rarely fires, but it undermines the R2\
      \ \"cursor closes all races\" framing in the plan. Suggested fix: swap order\
      \ \u2014 subscribe first with a handler that accepts any event matching pipeline_id\
      \ + type, then read tip, then filter in the main loop (not in handler). Any\
      \ duplicate-wake on a borderline event is harmless \u2014 the caller's next-call\
      \ cursor filters it out. This is a correctness improvement worth a follow-up\
      \ issue; not shipping-critical because (a) the overseer's `OVERSEER_ALERT` provides\
      \ a backstop on genuine stalls, (b) the race only drops the SINGLE event in\
      \ the window while a long-running pipeline emits many, and (c) the SKILL.md\
      \ cursor protocol naturally retries every 25s so a missed wake only costs one\
      \ cycle.\n\n2. **`_message_store_tip_id` dead code**: `orchestrator/routes/pipelines.py:241-263`.\
      \ The `limit=10_000` fallback branch is unreachable \u2014 the in-memory and\
      \ Redis `get_messages(limit=1)` both return the newest message via `messages[-limit:]`.\
      \ If `limit=1` returns empty, there are zero messages in the store for that\
      \ pipeline and `limit=10_000` will also return empty. Drop the fallback or add\
      \ a clarifying comment explaining what the fallback is guarding against.\n\n\
      3. **`_handle_wait_for_status_change` error handling**: when the route returns\
      \ 400/404, `self._make_request(...)` raises `urllib.error.HTTPError` and the\
      \ handler does not catch it. The exception propagates through anyio.to_thread.run_sync\
      \ to FastMCP, which serializes it as a generic tool error without surfacing\
      \ the route's `message` text. Consider catching `HTTPError` and extracting `json.loads(e.read())[\"\
      message\"]` so the skill surfaces the root cause instead of a bare \"HTTP 400\"\
      . Non-blocking because MCP clients can still retry, but a better UX fix.\n\n\
      4. **`_build_minimal_status_envelope` handling of `pipeline.phases.get(phase_key,\
      \ None)`**: `pipeline.phases` may be a dict in the Pipeline model; `getattr(phase_data,\
      \ \"started_at\", None)` works for both pydantic model and plain dict fields,\
      \ but if `phase_data` is a dict, `started_at` is accessed via `getattr` which\
      \ returns None for dicts (they expose keys via `__getattr__` only when using\
      \ a special subclass). Worth a one-line unit test to confirm dict-shaped phase_data\
      \ still produces `phase_elapsed_seconds` correctly. The coder's test file covers\
      \ Pipeline-with-PipelineConfig but not the dict-shaped path explicitly.\n\n\
      5. **`_on_message_store_wake` passes `limit=100`** \u2014 if more than 100 matching\
      \ messages fire before the wake, only the first 100 are returned. The cursor\
      \ advances to the last-returned message, so the next call picks up the rest.\
      \ Fine by design; worth a comment near the call stating \"intentionally bounded;\
      \ cursor handles pagination\".\n\n6. **Allowlist exclusion of `PHASE_FAILED`**:\
      \ the spec and HITL decision 2 exclude it, so this is correct per plan. But\
      \ note that a PHASE_FAILED without an accompanying PIPELINE_FAILED (rare but\
      \ possible during phase-level retries) would silently not wake the host. Document\
      \ this in the route's docstring so future authors understand why PHASE_FAILED\
      \ is not in the allowlist.\n\n### Not reviewed here\n\n- Tests are in `.egg-state/agent-outputs/`\
      \ as a handoff artifact, not yet under `orchestrator/tests/`. That's the tester's\
      \ job \u2014 I'll re-review when the tester proposes their CONSENSUS_PROPOSE\
      \ with the tests landed in the real test tree.\n- Integration tests (TASK-4-5)\
      \ are also tester-scope; not in this proposal.\n\n### Summary\n\nShip-ready.\
      \ Observations above are either narrow correctness improvements (items 1, 3,\
      \ 4) or documentation nits (items 2, 5, 6). None of them block the implement\
      \ phase.\n"
  version: 1
````

### [2026-04-24T04:30:12Z] orchestrator → coder (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 1). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 58b66956-b48b-47
phase: implement
metadata:
  fully_acked: true
  version: 1
````

### [2026-04-24T04:31:11Z] tester → coder (CONSENSUS_NACK): NACK from tester for coder


### Blocking

1. **orchestrator/routes/pipelines.py:2553 — F401 `_EventType` imported but unused.** The route only uses `event.event_type.value` (string compare against `_STATUS_WAIT_EVENT_TYPES`), never the `EventType` class. Both the primary import at line 2549 (`from events import EventType as _EventType`) and the fallback at line 2553 are dead. Fix: remove both `EventType as _EventType` imports from the try/except block so it reads `from events import get_event_bus` only. `ruff check .` fails on this; CI (`make lint-python`) will fail.

2. **orchestrator/routes/pipelines.py:2579 — UP037 quoted type annotation.** `wake_q: "_queue.Queue[tuple[str, Any]]" = _queue.Queue(maxsize=16)` — the string quotes are unnecessary on py313 (no forward-reference need here). Fix: remove the quotes so the annotation reads `wake_q: _queue.Queue[tuple[str, Any]] = _queue.Queue(maxsize=16)`. `ruff check --fix` will auto-apply this.

3. **orchestrator/routes/pipelines.py — `ruff format --check` fails.** Three spots inside the new `wait_pipeline_status` route have line-length / line-break layout that ruff format wants to reformat (e.g. `_parse_status_wait_cursor(request.args.get("since"))` would be collapsed onto a single line at line 2536; the 500 error return at line 2561 likewise). Fix: run `ruff format orchestrator/routes/pipelines.py` and commit the diff. CI (`make lint-python`) fails on this.

### Non-blocking

- **orchestrator/routes/pipelines.py:2571-2577** — The `event_since_seq` snap-to-tip logic closes the "cursor said None" case but the `_on_event` handler filter `event.sequence <= event_since_seq` is purely additive — events that fire **between the prior call's `finally` unsubscribe and the current call's `subscribe`** are lost because EventBus history is never inspected. This is the R2 race window the plan claims `since` closes; in reality the current implementation only closes the **re-wake-on-already-seen-event** direction (plan TASK-4-1 case (e), passing). My integration test in `orchestrator/tests/test_host_wait_integration.py::test_cursor_round_trip_suppresses_already_seen_event` pins the suppression direction that actually works. The "already-fired-events-during-the-gap are replayed" direction is a real gap — a follow-up issue to inspect `event_bus._history` at route entry would close it properly. Not blocking this PR — the aspirational liveness-floor + overseer-as-primary-deadlock-detector in the plan covers the gap, and the test count (16 route + 6 integration + 8 mcp_tools) comfortably covers the claimed behaviour.
- **Coder-handoff artifacts under `.egg-state/agent-outputs/1932-coder-tests/`** fail `ruff format --check .` ("Would reformat" on all three). These are not part of the test suite and do not block CI in practice (they are not imported by pytest collection), but operators running `ruff format --check .` from the repo root will see them flagged. Consider either (a) stripping them in a post-handoff commit or (b) adding `.egg-state/` to the project's ruff exclude list as a separate cleanup.

### What I verified against the proposed commits (20e1d083c head)

- Applied the staged handoff tests verbatim into `orchestrator/tests/` and all 16 route cases + 7 event-sequence cases + 8 mcp-tools cases pass.
- Added TASK-4-4 (`test_wait_for_status_change_does_not_double_sleep`) into `TestGetStatusWait` — passes and correctly asserts the `tool_name == 'get_status'` short-circuit.
- Added TASK-4-5 integration (`orchestrator/tests/test_host_wait_integration.py`) — 6 cases covering the full MCP handler → Flask route → EventBus/message-store chain; all pass.
- Updated pre-existing `orchestrator/tests/test_cli.py::TestWaitressSizing` to reflect the 16 → 24 default bump per TASK-1-4; the two previously-passing tests that asserted `threads == 16` now assert `threads == 24` and pass. The new `DEFAULT_WAITRESS_THREADS = 24` constant is compatible with the existing `test_refuse_to_boot_when_threads_lt_4` / `test_thread_count_honors_env_var` tests (boundaries unchanged).
- `PYTHONPATH=shared:gateway:orchestrator pytest orchestrator/tests/` on HEAD-plus-my-test-drops: 4397 passed, 1 skipped. 31 failed / 27 errors are all pre-existing on `origin/egg/issue-1932` prior to any of my or the coder's changes (kubernetes.client.VersionApi attribute errors, gateway_client fixture errors, deployment_routes mocking errors) — verified by stashing my diff and re-running.

Once the three ruff items above are fixed I will re-review and ACK.


````yaml
id: f9af22a4-a9df-4a
phase: implement
metadata:
  payload:
    reason: "\n### Blocking\n\n1. **orchestrator/routes/pipelines.py:2553 \u2014 F401\
      \ `_EventType` imported but unused.** The route only uses `event.event_type.value`\
      \ (string compare against `_STATUS_WAIT_EVENT_TYPES`), never the `EventType`\
      \ class. Both the primary import at line 2549 (`from events import EventType\
      \ as _EventType`) and the fallback at line 2553 are dead. Fix: remove both `EventType\
      \ as _EventType` imports from the try/except block so it reads `from events\
      \ import get_event_bus` only. `ruff check .` fails on this; CI (`make lint-python`)\
      \ will fail.\n\n2. **orchestrator/routes/pipelines.py:2579 \u2014 UP037 quoted\
      \ type annotation.** `wake_q: \"_queue.Queue[tuple[str, Any]]\" = _queue.Queue(maxsize=16)`\
      \ \u2014 the string quotes are unnecessary on py313 (no forward-reference need\
      \ here). Fix: remove the quotes so the annotation reads `wake_q: _queue.Queue[tuple[str,\
      \ Any]] = _queue.Queue(maxsize=16)`. `ruff check --fix` will auto-apply this.\n\
      \n3. **orchestrator/routes/pipelines.py \u2014 `ruff format --check` fails.**\
      \ Three spots inside the new `wait_pipeline_status` route have line-length /\
      \ line-break layout that ruff format wants to reformat (e.g. `_parse_status_wait_cursor(request.args.get(\"\
      since\"))` would be collapsed onto a single line at line 2536; the 500 error\
      \ return at line 2561 likewise). Fix: run `ruff format orchestrator/routes/pipelines.py`\
      \ and commit the diff. CI (`make lint-python`) fails on this.\n\n### Non-blocking\n\
      \n- **orchestrator/routes/pipelines.py:2571-2577** \u2014 The `event_since_seq`\
      \ snap-to-tip logic closes the \"cursor said None\" case but the `_on_event`\
      \ handler filter `event.sequence <= event_since_seq` is purely additive \u2014\
      \ events that fire **between the prior call's `finally` unsubscribe and the\
      \ current call's `subscribe`** are lost because EventBus history is never inspected.\
      \ This is the R2 race window the plan claims `since` closes; in reality the\
      \ current implementation only closes the **re-wake-on-already-seen-event** direction\
      \ (plan TASK-4-1 case (e), passing). My integration test in `orchestrator/tests/test_host_wait_integration.py::test_cursor_round_trip_suppresses_already_seen_event`\
      \ pins the suppression direction that actually works. The \"already-fired-events-during-the-gap\
      \ are replayed\" direction is a real gap \u2014 a follow-up issue to inspect\
      \ `event_bus._history` at route entry would close it properly. Not blocking\
      \ this PR \u2014 the aspirational liveness-floor + overseer-as-primary-deadlock-detector\
      \ in the plan covers the gap, and the test count (16 route + 6 integration +\
      \ 8 mcp_tools) comfortably covers the claimed behaviour.\n- **Coder-handoff\
      \ artifacts under `.egg-state/agent-outputs/1932-coder-tests/`** fail `ruff\
      \ format --check .` (\"Would reformat\" on all three). These are not part of\
      \ the test suite and do not block CI in practice (they are not imported by pytest\
      \ collection), but operators running `ruff format --check .` from the repo root\
      \ will see them flagged. Consider either (a) stripping them in a post-handoff\
      \ commit or (b) adding `.egg-state/` to the project's ruff exclude list as a\
      \ separate cleanup.\n\n### What I verified against the proposed commits (20e1d083c\
      \ head)\n\n- Applied the staged handoff tests verbatim into `orchestrator/tests/`\
      \ and all 16 route cases + 7 event-sequence cases + 8 mcp-tools cases pass.\n\
      - Added TASK-4-4 (`test_wait_for_status_change_does_not_double_sleep`) into\
      \ `TestGetStatusWait` \u2014 passes and correctly asserts the `tool_name ==\
      \ 'get_status'` short-circuit.\n- Added TASK-4-5 integration (`orchestrator/tests/test_host_wait_integration.py`)\
      \ \u2014 6 cases covering the full MCP handler \u2192 Flask route \u2192 EventBus/message-store\
      \ chain; all pass.\n- Updated pre-existing `orchestrator/tests/test_cli.py::TestWaitressSizing`\
      \ to reflect the 16 \u2192 24 default bump per TASK-1-4; the two previously-passing\
      \ tests that asserted `threads == 16` now assert `threads == 24` and pass. The\
      \ new `DEFAULT_WAITRESS_THREADS = 24` constant is compatible with the existing\
      \ `test_refuse_to_boot_when_threads_lt_4` / `test_thread_count_honors_env_var`\
      \ tests (boundaries unchanged).\n- `PYTHONPATH=shared:gateway:orchestrator pytest\
      \ orchestrator/tests/` on HEAD-plus-my-test-drops: 4397 passed, 1 skipped. 31\
      \ failed / 27 errors are all pre-existing on `origin/egg/issue-1932` prior to\
      \ any of my or the coder's changes (kubernetes.client.VersionApi attribute errors,\
      \ gateway_client fixture errors, deployment_routes mocking errors) \u2014 verified\
      \ by stashing my diff and re-running.\n\nOnce the three ruff items above are\
      \ fixed I will re-review and ACK.\n"
    artifact_references:
    - orchestrator/events.py
    - orchestrator/routes/pipelines.py
    - orchestrator/mcp_tools.py
    - orchestrator/env_config.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
  reason: "\n### Blocking\n\n1. **orchestrator/routes/pipelines.py:2553 \u2014 F401\
    \ `_EventType` imported but unused.** The route only uses `event.event_type.value`\
    \ (string compare against `_STATUS_WAIT_EVENT_TYPES`), never the `EventType` class.\
    \ Both the primary import at line 2549 (`from events import EventType as _EventType`)\
    \ and the fallback at line 2553 are dead. Fix: remove both `EventType as _EventType`\
    \ imports from the try/except block so it reads `from events import get_event_bus`\
    \ only. `ruff check .` fails on this; CI (`make lint-python`) will fail.\n\n2.\
    \ **orchestrator/routes/pipelines.py:2579 \u2014 UP037 quoted type annotation.**\
    \ `wake_q: \"_queue.Queue[tuple[str, Any]]\" = _queue.Queue(maxsize=16)` \u2014\
    \ the string quotes are unnecessary on py313 (no forward-reference need here).\
    \ Fix: remove the quotes so the annotation reads `wake_q: _queue.Queue[tuple[str,\
    \ Any]] = _queue.Queue(maxsize=16)`. `ruff check --fix` will auto-apply this.\n\
    \n3. **orchestrator/routes/pipelines.py \u2014 `ruff format --check` fails.**\
    \ Three spots inside the new `wait_pipeline_status` route have line-length / line-break\
    \ layout that ruff format wants to reformat (e.g. `_parse_status_wait_cursor(request.args.get(\"\
    since\"))` would be collapsed onto a single line at line 2536; the 500 error return\
    \ at line 2561 likewise). Fix: run `ruff format orchestrator/routes/pipelines.py`\
    \ and commit the diff. CI (`make lint-python`) fails on this.\n\n### Non-blocking\n\
    \n- **orchestrator/routes/pipelines.py:2571-2577** \u2014 The `event_since_seq`\
    \ snap-to-tip logic closes the \"cursor said None\" case but the `_on_event` handler\
    \ filter `event.sequence <= event_since_seq` is purely additive \u2014 events\
    \ that fire **between the prior call's `finally` unsubscribe and the current call's\
    \ `subscribe`** are lost because EventBus history is never inspected. This is\
    \ the R2 race window the plan claims `since` closes; in reality the current implementation\
    \ only closes the **re-wake-on-already-seen-event** direction (plan TASK-4-1 case\
    \ (e), passing). My integration test in `orchestrator/tests/test_host_wait_integration.py::test_cursor_round_trip_suppresses_already_seen_event`\
    \ pins the suppression direction that actually works. The \"already-fired-events-during-the-gap\
    \ are replayed\" direction is a real gap \u2014 a follow-up issue to inspect `event_bus._history`\
    \ at route entry would close it properly. Not blocking this PR \u2014 the aspirational\
    \ liveness-floor + overseer-as-primary-deadlock-detector in the plan covers the\
    \ gap, and the test count (16 route + 6 integration + 8 mcp_tools) comfortably\
    \ covers the claimed behaviour.\n- **Coder-handoff artifacts under `.egg-state/agent-outputs/1932-coder-tests/`**\
    \ fail `ruff format --check .` (\"Would reformat\" on all three). These are not\
    \ part of the test suite and do not block CI in practice (they are not imported\
    \ by pytest collection), but operators running `ruff format --check .` from the\
    \ repo root will see them flagged. Consider either (a) stripping them in a post-handoff\
    \ commit or (b) adding `.egg-state/` to the project's ruff exclude list as a separate\
    \ cleanup.\n\n### What I verified against the proposed commits (20e1d083c head)\n\
    \n- Applied the staged handoff tests verbatim into `orchestrator/tests/` and all\
    \ 16 route cases + 7 event-sequence cases + 8 mcp-tools cases pass.\n- Added TASK-4-4\
    \ (`test_wait_for_status_change_does_not_double_sleep`) into `TestGetStatusWait`\
    \ \u2014 passes and correctly asserts the `tool_name == 'get_status'` short-circuit.\n\
    - Added TASK-4-5 integration (`orchestrator/tests/test_host_wait_integration.py`)\
    \ \u2014 6 cases covering the full MCP handler \u2192 Flask route \u2192 EventBus/message-store\
    \ chain; all pass.\n- Updated pre-existing `orchestrator/tests/test_cli.py::TestWaitressSizing`\
    \ to reflect the 16 \u2192 24 default bump per TASK-1-4; the two previously-passing\
    \ tests that asserted `threads == 16` now assert `threads == 24` and pass. The\
    \ new `DEFAULT_WAITRESS_THREADS = 24` constant is compatible with the existing\
    \ `test_refuse_to_boot_when_threads_lt_4` / `test_thread_count_honors_env_var`\
    \ tests (boundaries unchanged).\n- `PYTHONPATH=shared:gateway:orchestrator pytest\
    \ orchestrator/tests/` on HEAD-plus-my-test-drops: 4397 passed, 1 skipped. 31\
    \ failed / 27 errors are all pre-existing on `origin/egg/issue-1932` prior to\
    \ any of my or the coder's changes (kubernetes.client.VersionApi attribute errors,\
    \ gateway_client fixture errors, deployment_routes mocking errors) \u2014 verified\
    \ by stashing my diff and re-running.\n\nOnce the three ruff items above are fixed\
    \ I will re-review and ACK.\n"
  revision_count: 1
````

### [2026-04-24T04:33:05Z] coder (CONSENSUS_PROPOSE): Proposal from coder

Re-proposal v2 addressing tester NACK (f9af22a4-a9df-4a). Fixed three ruff-blocking issues in orchestrator/routes/pipelines.py: (1) F401 — removed unused `EventType as _EventType` import from both try/except legs in wait_pipeline_status; the route only compares via `event.event_type.value` against _STATUS_WAIT_EVENT_TYPES so the EventType class itself is dead import. (2) UP037 — unquoted `wake_q: _queue.Queue[tuple[str, Any]]` annotation since py313 does not need forward-reference quoting there. (3) ruff format — applied formatter to collapse two line-broken-for-length lines in the new route. Also ran `ruff format` on the three handoff test files under .egg-state/agent-outputs/1932-coder-tests/ so `ruff format --check .` from the repo root is clean (non-blocking observation from tester). `ruff check .` + `ruff format --check .` now pass for all coder-scope files (orchestrator/events.py, orchestrator/env_config.py, orchestrator/routes/pipelines.py, orchestrator/mcp_tools.py, and the three handoff files). All prior non-test tests still pass; the only pre-existing failure (test_mcp_tools.py::TestToolRouting::test_all_tools_registered) is tester scope and was addressed in tester's 3f00ad631. The R2 race-window observation from tester (non-blocking) is acknowledged as a real follow-up but preserved unchanged in this PR per the plan's aspirational 60s liveness floor + overseer-as-primary-deadlock-detector framing.

````yaml
id: 1f01ae37-6c0b-4e
phase: implement
metadata:
  payload:
    summary: "Re-proposal v2 addressing tester NACK (f9af22a4-a9df-4a). Fixed three\
      \ ruff-blocking issues in orchestrator/routes/pipelines.py: (1) F401 \u2014\
      \ removed unused `EventType as _EventType` import from both try/except legs\
      \ in wait_pipeline_status; the route only compares via `event.event_type.value`\
      \ against _STATUS_WAIT_EVENT_TYPES so the EventType class itself is dead import.\
      \ (2) UP037 \u2014 unquoted `wake_q: _queue.Queue[tuple[str, Any]]` annotation\
      \ since py313 does not need forward-reference quoting there. (3) ruff format\
      \ \u2014 applied formatter to collapse two line-broken-for-length lines in the\
      \ new route. Also ran `ruff format` on the three handoff test files under .egg-state/agent-outputs/1932-coder-tests/\
      \ so `ruff format --check .` from the repo root is clean (non-blocking observation\
      \ from tester). `ruff check .` + `ruff format --check .` now pass for all coder-scope\
      \ files (orchestrator/events.py, orchestrator/env_config.py, orchestrator/routes/pipelines.py,\
      \ orchestrator/mcp_tools.py, and the three handoff files). All prior non-test\
      \ tests still pass; the only pre-existing failure (test_mcp_tools.py::TestToolRouting::test_all_tools_registered)\
      \ is tester scope and was addressed in tester's 3f00ad631. The R2 race-window\
      \ observation from tester (non-blocking) is acknowledged as a real follow-up\
      \ but preserved unchanged in this PR per the plan's aspirational 60s liveness\
      \ floor + overseer-as-primary-deadlock-detector framing."
    attestation: {}
    artifacts:
    - orchestrator/events.py
    - orchestrator/env_config.py
    - orchestrator/routes/pipelines.py
    - orchestrator/mcp_tools.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    - .egg-state/agent-outputs/1932-coder-tests/HANDOFF.md
    risk_considered: All risks from prior proposal unchanged (R1, R2, R3, R4, R5,
      R6, R13, R14, R15, R16, R17). Tester's non-blocking race-window observation
      on R2 is acknowledged and will be addressed in a follow-up issue; current implementation
      relies on the aspirational 60s liveness floor + overseer-backstop per plan resolution.
    commit_sha: ac99f9c1c
    files_changed:
    - orchestrator/routes/pipelines.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    tests_run:
    - ruff check orchestrator/routes/pipelines.py orchestrator/events.py orchestrator/mcp_tools.py
      orchestrator/env_config.py (clean)
    - ruff format --check orchestrator/... .egg-state/agent-outputs/1932-coder-tests/
      (clean)
    - orchestrator/tests/test_mcp_tools.py (190 passed; 1 pre-existing test_all_tools_registered
      failure in tester scope addressed separately in 3f00ad631)
    - orchestrator/tests/test_concurrent_status.py + test_concurrent_wait.py (89 passed)
    tasks_satisfied:
    - TASK-1-1
    - TASK-1-2
    - TASK-1-3
    - TASK-1-4
    - TASK-2-1
    - TASK-2-2
    - TASK-2-3
  version: 2
  commit_sha: ac99f9c1c
````

### [2026-04-24T04:33:48Z] overseer (HEARTBEAT): heartbeat: WORKING

Cycle 22: Direct BRC query shows 23 new messages since cycle 17 (74 total). 2 new NACKs (3 total), 4 new proposals (11 total), 1 HANDOFF. coder=PROPOSED, documenter=PROPOSED, tester=WORKING+REVIEWING. Active BRC convergence underway. 0 alerts. Monitoring for tester CONSENSUS_PROPOSE.

````yaml
id: bad58831-b0a4-44
phase: implement
metadata:
  state: WORKING
````

### [2026-04-24T04:33:54Z] tester (CONSENSUS_PROPOSE): Proposal from tester

Tester role for issue #1932: TASK-4-1/-2/-3/-4/-5 test coverage for the new event-driven host-side wait primitive; fixes the TestWaitressSizing regression caused by TASK-1-4's 16 to 24 default bump. Added orchestrator/tests/test_pipelines_status_wait_route.py (16 cases), test_events_event_sequence.py (7 cases), test_host_wait_integration.py (6 e2e cases), extended test_mcp_tools.py (+7 cases + double-sleep regression + TestToolRouting update), updated test_cli.py::TestWaitressSizing for the 16->24 bump. Full suite: 4397 passed, 1 skipped; the 31 pre-existing failures (kube/gateway/deployment mocking) are unrelated. Coder NACKed on 3 pipelines.py ruff blockers (F401, UP037, format) tracked separately. Checks passed: ruff check, ruff format --check, pytest.

````yaml
id: bc96455d-5fa8-42
phase: implement
metadata:
  payload:
    summary: 'Tester role for issue #1932: TASK-4-1/-2/-3/-4/-5 test coverage for
      the new event-driven host-side wait primitive; fixes the TestWaitressSizing
      regression caused by TASK-1-4''s 16 to 24 default bump. Added orchestrator/tests/test_pipelines_status_wait_route.py
      (16 cases), test_events_event_sequence.py (7 cases), test_host_wait_integration.py
      (6 e2e cases), extended test_mcp_tools.py (+7 cases + double-sleep regression
      + TestToolRouting update), updated test_cli.py::TestWaitressSizing for the 16->24
      bump. Full suite: 4397 passed, 1 skipped; the 31 pre-existing failures (kube/gateway/deployment
      mocking) are unrelated. Coder NACKed on 3 pipelines.py ruff blockers (F401,
      UP037, format) tracked separately. Checks passed: ruff check, ruff format --check,
      pytest.'
    attestation: {}
    artifacts:
    - orchestrator/tests/test_pipelines_status_wait_route.py
    - orchestrator/tests/test_events_event_sequence.py
    - orchestrator/tests/test_host_wait_integration.py
    - orchestrator/tests/test_mcp_tools.py
    - orchestrator/tests/test_cli.py
    risk_considered: ''
    commit_sha: 3f00ad631
    files_changed:
    - orchestrator/tests/test_pipelines_status_wait_route.py
    - orchestrator/tests/test_events_event_sequence.py
    - orchestrator/tests/test_host_wait_integration.py
    - orchestrator/tests/test_mcp_tools.py
    - orchestrator/tests/test_cli.py
    tests_run:
    - orchestrator/tests/test_pipelines_status_wait_route.py
    - orchestrator/tests/test_events_event_sequence.py
    - orchestrator/tests/test_host_wait_integration.py
    - orchestrator/tests/test_mcp_tools.py::TestGetStatusWait::test_wait_for_status_change_does_not_double_sleep
    - orchestrator/tests/test_mcp_tools.py::TestWaitForStatusChange
    - orchestrator/tests/test_mcp_tools.py::TestBuildStatusSnapshotRefactor
    - orchestrator/tests/test_mcp_tools.py::TestToolRouting
    - orchestrator/tests/test_cli.py::TestWaitressSizing
    tasks_satisfied:
    - task-4-1
    - task-4-2
    - task-4-3
    - task-4-4
    - task-4-5
  version: 1
  commit_sha: 3f00ad631
````

### [2026-04-24T04:33:57Z] reviewer_code → coder (CONSENSUS_ACK): ACK from reviewer_code for coder


Re-ACKed v2 (commit ac99f9c1c). Verified the delta against my v1 ACK is pure lint/format cleanup:

1. **F401 — removed unused `EventType as _EventType` imports** from both try/except legs in `wait_pipeline_status` (`orchestrator/routes/pipelines.py:2540-2552` area). Confirmed the route compares events via `event.event_type.value` (string) against the frozenset `_STATUS_WAIT_EVENT_TYPES`, never against the `EventType` class itself. Both imports were dead. Safe to remove.

2. **UP037 — removed the quoted type annotation on `wake_q`** (`orchestrator/routes/pipelines.py:2575`). `_queue.Queue[tuple[str, Any]]` is valid subscripted syntax in py313 without forward-reference quoting.

3. **`ruff format` packed three over-wrapped lines onto single lines**. Cosmetic. The full cursor-parse error message now lives on one line, the "Event bus not available" `make_error_response` call is single-line, and the cursor-parse call is single-line.

Also applied `ruff format` to the three handoff test files under `.egg-state/agent-outputs/1932-coder-tests/` so the whole working tree is format-clean.

Syntax verified with `ast.parse` on `orchestrator/routes/pipelines.py` — clean.

No behaviour change. My v1 ACK's analysis and non-blocking observations (race window between `current_sequence()` and `subscribe()`, `_message_store_tip_id` dead code, HTTPError surfacing in MCP handler, dict-shaped phase_data path, `limit=100` pagination comment, PHASE_FAILED docstring note) all still apply — none were addressed in this commit, which is the correct scope given the focus was lint/format only.

Still ship-ready. Observation items remain as follow-up candidates, not blockers.


````yaml
id: 449f44c1-338c-46
phase: implement
metadata:
  payload:
    artifact_references:
    - orchestrator/routes/pipelines.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    reason: "\nRe-ACKed v2 (commit ac99f9c1c). Verified the delta against my v1 ACK\
      \ is pure lint/format cleanup:\n\n1. **F401 \u2014 removed unused `EventType\
      \ as _EventType` imports** from both try/except legs in `wait_pipeline_status`\
      \ (`orchestrator/routes/pipelines.py:2540-2552` area). Confirmed the route compares\
      \ events via `event.event_type.value` (string) against the frozenset `_STATUS_WAIT_EVENT_TYPES`,\
      \ never against the `EventType` class itself. Both imports were dead. Safe to\
      \ remove.\n\n2. **UP037 \u2014 removed the quoted type annotation on `wake_q`**\
      \ (`orchestrator/routes/pipelines.py:2575`). `_queue.Queue[tuple[str, Any]]`\
      \ is valid subscripted syntax in py313 without forward-reference quoting.\n\n\
      3. **`ruff format` packed three over-wrapped lines onto single lines**. Cosmetic.\
      \ The full cursor-parse error message now lives on one line, the \"Event bus\
      \ not available\" `make_error_response` call is single-line, and the cursor-parse\
      \ call is single-line.\n\nAlso applied `ruff format` to the three handoff test\
      \ files under `.egg-state/agent-outputs/1932-coder-tests/` so the whole working\
      \ tree is format-clean.\n\nSyntax verified with `ast.parse` on `orchestrator/routes/pipelines.py`\
      \ \u2014 clean.\n\nNo behaviour change. My v1 ACK's analysis and non-blocking\
      \ observations (race window between `current_sequence()` and `subscribe()`,\
      \ `_message_store_tip_id` dead code, HTTPError surfacing in MCP handler, dict-shaped\
      \ phase_data path, `limit=100` pagination comment, PHASE_FAILED docstring note)\
      \ all still apply \u2014 none were addressed in this commit, which is the correct\
      \ scope given the focus was lint/format only.\n\nStill ship-ready. Observation\
      \ items remain as follow-up candidates, not blockers.\n"
  version: 2
````

### [2026-04-24T04:34:30Z] reviewer_contract → coder (CONSENSUS_ACK): ACK from reviewer_contract for coder


### Contract Verification — coder v2 (commit ac99f9c1c)

Re-reviewed the coder v2 delta on top of v1 (20e1d083c → ac99f9c1c, net +19 / -46 across 4 files). The v2 scope is **purely non-functional** — zero changes to route behavior, cursor semantics, threading model, MCP tool handler contract, or the event/message allowlists I verified in the v1 ACK. The v1 contract verification carries forward in full.

**v2 changes (all verified as lint/format-only):**
- `orchestrator/routes/pipelines.py:2541-2554` — removed the unused `from events import EventType as _EventType` imports from both primary and fallback `try/except` legs. The route compares via `event.event_type.value` (string) against `_STATUS_WAIT_EVENT_TYPES: frozenset[str]`, never against the `EventType` class, so the imports were genuinely dead (verified at the subscribe/filter site `_on_event` where `event.event_type.value not in _STATUS_WAIT_EVENT_TYPES` is the only usage). No runtime path change.
- `orchestrator/routes/pipelines.py:2575` — `wake_q: "_queue.Queue[tuple[str, Any]]"` → `wake_q: _queue.Queue[tuple[str, Any]]` (UP037 — py3.13 does not need the forward-reference quoting). Pure annotation form.
- Two `make_error_response(...)` calls collapsed onto single lines by `ruff format` — string content verbatim, no error-body shape change (confirmed: `"Invalid 'since' cursor — expected 'msg:<id>|evt:<seq>' (either half may be empty)."` and `"Event bus not available"` both preserved character-for-character).
- `.egg-state/agent-outputs/1932-coder-tests/*.py` — `ruff format` whitespace in the three handoff files. The same test content is already in-tree under `orchestrator/tests/` via the tester's commit `3f00ad631`, so the agent-outputs copies are purely reference / audit-trail now and formatting drift there is harmless. Good hygiene either way.

**All plan-mapped coder-scope tasks remain verified:**
- **TASK-1-1** Event.sequence + EventBus._sequence + current_sequence() — unchanged in v2; still at `orchestrator/events.py:104-119, 155-161, 338-347`.
- **TASK-1-2** `/status/wait` route — unchanged behaviorally; lazy imports trimmed but both halves of the queue/daemon/wildcard-handler pattern, DECISION_RESOLVED exclusion, delphi filter, cursor-to-tip fallback semantics, and 400/404 responses are byte-identical to v1.
- **TASK-1-3** `egg_inflight_host_waits` gauge — unchanged.
- **TASK-1-4** DEFAULT_WAITRESS_THREADS 16→24 — unchanged; the tester's `3f00ad631` already landed the paired `test_default_threads_is_24` assertion update so the regression is covered.
- **TASK-2-1** `wait_for_status_change` schema — unchanged in `orchestrator/mcp_tools.py:305-353`.
- **TASK-2-2** `_build_status_snapshot` extraction — unchanged; `_handle_get_status` is still the one-line wrapper.
- **TASK-2-3** `_handle_wait_for_status_change` — unchanged at `orchestrator/mcp_tools.py:1725-1784`.
- **R16 double-sleep pin** — `orchestrator/mcp_server.py:50-67` still short-circuits on `tool_name != "get_status"`. Tester landed `test_wait_for_status_change_does_not_double_sleep` in `TestGetStatusWait` (per `3f00ad631` commit message), which pins this.

**Responsive to tester NACK:** the commit body cites tester's NACK (`f9af22a4-a9df-4a`) on v1, names each of the three blocking lint items, explains why the imports were dead, and confirms `ruff check .` + `ruff format --check .` pass on the coder-scope files. Tester's non-blocking handoff-file formatting drift is also addressed. Clean turnaround.

**Test coverage tracking (informational, not a blocker on coder):** Tester's `3f00ad631` closes TASK-4-1 (route tests), TASK-4-2 (mcp_tools additions), TASK-4-3 (Event sequence), TASK-4-4 (R16 regression pin), and TASK-4-5 (in-process integration test). All TASK-3-* and TASK-4-6/4-7 are held by documenter (v2 at `d76e1d43b`). With coder v2 ACKed, every plan-defined task has an owner that has produced artifacts.

### Non-blocking
- **Contract JSON drift (repeat from v1 review)**: `.egg-state/contracts/issue-1932.json` still has `current_phase: refine`, `tasks: []`, `acceptance_criteria: []`. The plan-phase persistence step did not populate the contract's task array from the plan draft. Post-merge `egg-contract verify-criterion` will have nothing to mark verified. Out of coder scope; flagging again so a follow-up can capture it.
- **`test_all_tools_registered` expected-set**: coder's v2 commit body notes `TestToolRouting::test_all_tools_registered` fails on v1 pending the tester's addition of `"wait_for_status_change"` to the expected set. Tester's `3f00ad631` commit body explicitly says this is addressed; worth confirming the pair lands together before PR.
- **In-process integration test scope**: tester replaced the plan's `integration_tests/test_host_wait_end_to_end.py` (live-orchestrator) with an in-process `orchestrator/tests/test_host_wait_integration.py`. Reasonable given sandbox constraints, but a follow-up to add a true end-to-end variant when the sandbox lifts the live-orchestrator restriction would close the last gap in TASK-4-5's original intent.


````yaml
id: 10fac282-05a5-45
phase: implement
metadata:
  payload:
    artifact_references:
    - orchestrator/routes/pipelines.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    reason: "\n### Contract Verification \u2014 coder v2 (commit ac99f9c1c)\n\nRe-reviewed\
      \ the coder v2 delta on top of v1 (20e1d083c \u2192 ac99f9c1c, net +19 / -46\
      \ across 4 files). The v2 scope is **purely non-functional** \u2014 zero changes\
      \ to route behavior, cursor semantics, threading model, MCP tool handler contract,\
      \ or the event/message allowlists I verified in the v1 ACK. The v1 contract\
      \ verification carries forward in full.\n\n**v2 changes (all verified as lint/format-only):**\n\
      - `orchestrator/routes/pipelines.py:2541-2554` \u2014 removed the unused `from\
      \ events import EventType as _EventType` imports from both primary and fallback\
      \ `try/except` legs. The route compares via `event.event_type.value` (string)\
      \ against `_STATUS_WAIT_EVENT_TYPES: frozenset[str]`, never against the `EventType`\
      \ class, so the imports were genuinely dead (verified at the subscribe/filter\
      \ site `_on_event` where `event.event_type.value not in _STATUS_WAIT_EVENT_TYPES`\
      \ is the only usage). No runtime path change.\n- `orchestrator/routes/pipelines.py:2575`\
      \ \u2014 `wake_q: \"_queue.Queue[tuple[str, Any]]\"` \u2192 `wake_q: _queue.Queue[tuple[str,\
      \ Any]]` (UP037 \u2014 py3.13 does not need the forward-reference quoting).\
      \ Pure annotation form.\n- Two `make_error_response(...)` calls collapsed onto\
      \ single lines by `ruff format` \u2014 string content verbatim, no error-body\
      \ shape change (confirmed: `\"Invalid 'since' cursor \u2014 expected 'msg:<id>|evt:<seq>'\
      \ (either half may be empty).\"` and `\"Event bus not available\"` both preserved\
      \ character-for-character).\n- `.egg-state/agent-outputs/1932-coder-tests/*.py`\
      \ \u2014 `ruff format` whitespace in the three handoff files. The same test\
      \ content is already in-tree under `orchestrator/tests/` via the tester's commit\
      \ `3f00ad631`, so the agent-outputs copies are purely reference / audit-trail\
      \ now and formatting drift there is harmless. Good hygiene either way.\n\n**All\
      \ plan-mapped coder-scope tasks remain verified:**\n- **TASK-1-1** Event.sequence\
      \ + EventBus._sequence + current_sequence() \u2014 unchanged in v2; still at\
      \ `orchestrator/events.py:104-119, 155-161, 338-347`.\n- **TASK-1-2** `/status/wait`\
      \ route \u2014 unchanged behaviorally; lazy imports trimmed but both halves\
      \ of the queue/daemon/wildcard-handler pattern, DECISION_RESOLVED exclusion,\
      \ delphi filter, cursor-to-tip fallback semantics, and 400/404 responses are\
      \ byte-identical to v1.\n- **TASK-1-3** `egg_inflight_host_waits` gauge \u2014\
      \ unchanged.\n- **TASK-1-4** DEFAULT_WAITRESS_THREADS 16\u219224 \u2014 unchanged;\
      \ the tester's `3f00ad631` already landed the paired `test_default_threads_is_24`\
      \ assertion update so the regression is covered.\n- **TASK-2-1** `wait_for_status_change`\
      \ schema \u2014 unchanged in `orchestrator/mcp_tools.py:305-353`.\n- **TASK-2-2**\
      \ `_build_status_snapshot` extraction \u2014 unchanged; `_handle_get_status`\
      \ is still the one-line wrapper.\n- **TASK-2-3** `_handle_wait_for_status_change`\
      \ \u2014 unchanged at `orchestrator/mcp_tools.py:1725-1784`.\n- **R16 double-sleep\
      \ pin** \u2014 `orchestrator/mcp_server.py:50-67` still short-circuits on `tool_name\
      \ != \"get_status\"`. Tester landed `test_wait_for_status_change_does_not_double_sleep`\
      \ in `TestGetStatusWait` (per `3f00ad631` commit message), which pins this.\n\
      \n**Responsive to tester NACK:** the commit body cites tester's NACK (`f9af22a4-a9df-4a`)\
      \ on v1, names each of the three blocking lint items, explains why the imports\
      \ were dead, and confirms `ruff check .` + `ruff format --check .` pass on the\
      \ coder-scope files. Tester's non-blocking handoff-file formatting drift is\
      \ also addressed. Clean turnaround.\n\n**Test coverage tracking (informational,\
      \ not a blocker on coder):** Tester's `3f00ad631` closes TASK-4-1 (route tests),\
      \ TASK-4-2 (mcp_tools additions), TASK-4-3 (Event sequence), TASK-4-4 (R16 regression\
      \ pin), and TASK-4-5 (in-process integration test). All TASK-3-* and TASK-4-6/4-7\
      \ are held by documenter (v2 at `d76e1d43b`). With coder v2 ACKed, every plan-defined\
      \ task has an owner that has produced artifacts.\n\n### Non-blocking\n- **Contract\
      \ JSON drift (repeat from v1 review)**: `.egg-state/contracts/issue-1932.json`\
      \ still has `current_phase: refine`, `tasks: []`, `acceptance_criteria: []`.\
      \ The plan-phase persistence step did not populate the contract's task array\
      \ from the plan draft. Post-merge `egg-contract verify-criterion` will have\
      \ nothing to mark verified. Out of coder scope; flagging again so a follow-up\
      \ can capture it.\n- **`test_all_tools_registered` expected-set**: coder's v2\
      \ commit body notes `TestToolRouting::test_all_tools_registered` fails on v1\
      \ pending the tester's addition of `\"wait_for_status_change\"` to the expected\
      \ set. Tester's `3f00ad631` commit body explicitly says this is addressed; worth\
      \ confirming the pair lands together before PR.\n- **In-process integration\
      \ test scope**: tester replaced the plan's `integration_tests/test_host_wait_end_to_end.py`\
      \ (live-orchestrator) with an in-process `orchestrator/tests/test_host_wait_integration.py`.\
      \ Reasonable given sandbox constraints, but a follow-up to add a true end-to-end\
      \ variant when the sandbox lifts the live-orchestrator restriction would close\
      \ the last gap in TASK-4-5's original intent.\n"
  version: 2
````

### [2026-04-24T04:34:30Z] orchestrator → coder (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 2). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 1cab3855-810e-46
phase: implement
metadata:
  fully_acked: true
  version: 2
````

### [2026-04-24T04:34:33Z] reviewer_contract (CONSENSUS_CONFIRMED): Confirmed by reviewer_contract

````yaml
id: 04ed5be3-a657-40
phase: implement
metadata:
  consensus_reached: false
````

### [2026-04-24T04:34:40Z] reviewer_contract (HEARTBEAT): heartbeat: IDLE

reviewer_contract CONFIRMED on coder v2 (ac99f9c1c). Staying alive for BRC events until orchestrator stops; no open actions for me.

````yaml
id: b1e47022-c88f-41
phase: implement
metadata:
  state: IDLE
````

### [2026-04-24T04:36:09Z] reviewer_code → tester (CONSENSUS_ACK): ACK from reviewer_code for tester


Reviewed tester commit 3f00ad631 — test coverage for TASK-4-1 through TASK-4-5 plus the TASK-1-4 regression fix for `TestWaitressSizing`. No blocking issues.

### What I verified

**Five test files, all pass `ast.parse`:**
- `orchestrator/tests/test_pipelines_status_wait_route.py` (432 lines) — applied verbatim from the coder handoff under `.egg-state/agent-outputs/1932-coder-tests/`. 16 cases: cursor roundtrip + empty + missing halves + 4 malformed patterns, timeout envelope shape, PHASE_STARTED wake, DECISION_RESOLVED exclusion (critical — pins the self-wake prevention), `since` cursor replay skip, OVERSEER_ALERT message-bus wake with `_apply_delphi_filter` passthrough, 400 malformed cursor, 404 unknown pipeline, 400 invalid wait, gauge lifecycle, queue-full burst. Uses `EventBus(async_delivery=False)` fixture for deterministic handler firing.
- `orchestrator/tests/test_events_event_sequence.py` (114 lines) — applied from handoff. 7 cases including the critical 100-publish/8-thread monotonicity test that establishes no-gap, no-duplicate guarantees for the `_sequence` counter under contention.
- `orchestrator/tests/test_mcp_tools.py` +247 lines — new `TestWaitForStatusChange` class (7 sub-cases for handler dispatch, no_change passthrough, envelope merge on event/message triggers, since URL-encoding, empty-since omission) and `TestBuildStatusSnapshotRefactor` class (byte-identical equivalence to pre-refactor `_handle_get_status` output — pins TASK-2-2 is pure extraction). Also extends `TestToolRouting.test_all_tools_registered` to include `wait_for_status_change` in the expected set.
- `orchestrator/tests/test_mcp_tools.py::TestGetStatusWait::test_wait_for_status_change_does_not_double_sleep` — this is the R16 pin for TASK-4-4. Patches `mcp_server._async_sleep` with `AsyncMock`, dispatches `wait_for_status_change` through `_apply_get_status_wait`, asserts the mock is never invoked AND `kwargs["wait"]` is preserved (i.e. the tool handler still sees it). Correctly pins the `tool_name == 'get_status'` short-circuit. I also verified the test's assertion will fail correctly if the guard is removed — removing `if tool_name != "get_status": return` from `mcp_server.py:61-62` would let the `await _async_sleep(...)` line fire on `wait_for_status_change` calls, and `mock_sleep.assert_not_called()` would raise.
- `orchestrator/tests/test_host_wait_integration.py` (402 lines, 6 cases) — the sandbox-friendly integration test. Exercises the full MCP handler → Flask route → EventBus/message-store chain without needing Docker or a live orchestrator. The plan's `integration_tests/test_host_wait_end_to_end.py` (live stack) is consciously out of scope; the rationale is documented in the module docstring. The cursor round-trip case (sub-case 4) directly tests the R2 race-window closure semantics: a call returns cursor evt:N; a second call with `since=evt:N` skips events at-or-below N but still wakes on events > N. This is the intended mitigation surface and the test pins it.

**Regression fix:**
- `orchestrator/tests/test_cli.py::TestWaitressSizing` — renamed `test_default_threads_is_16` → `test_default_threads_is_24`, updated assertion from 16 → 24, updated docstring to cite the TASK-1-4 rationale. The malformed-threads-fallback case was similarly updated. Other cases (refuse-to-boot, boundary-3/4, env-var-override, channel-timeout) are unchanged — floor/boundary/override semantics did not move in TASK-1-4. Spot-checked that the renaming is consistent.

**Test execution claim:**
The commit message reports `4397 passed, 1 skipped` on HEAD+commit, with the 31/27 pre-existing failures / errors attributed to unrelated issues (kubernetes.client.VersionApi, gateway_client fixture, deployment_routes mocking) — verified by stashing the diff and rerunning. I cannot re-run pytest inside this sandbox (no full orchestrator test environment), but the attestation is specific and falsifiable (names the failing classes), the applied test files parse cleanly, and the test logic matches the plan's acceptance criteria.

### Non-blocking observations

1. **Cursor round-trip sub-case 4 could be more adversarial.** The test fires a single event between the two calls and checks cursor-skip behaviour. Consider adding a variant where TWO events fire between calls — one at-or-below the cursor (should be skipped), one above (should wake). The current test only covers the "skip" direction.
2. **No test for the `limit=100` message-page boundary.** If an `OVERSEER_ALERT` storm fires >100 matching messages before the wake, the route returns only the first 100 and the cursor advances to that last message. Not a blocker; the pagination semantics are inherited from `message_store.get_messages` and are tested there.
3. **Handoff integration is clean.** The coder's handoff files under `.egg-state/agent-outputs/1932-coder-tests/` were applied verbatim (same line counts, same test names). This is the correct workflow given the gateway's file-role policy blocking `coder` from `orchestrator/tests/**`.
4. **No integration test against a live orchestrator.** Plan TASK-4-5 specifies `integration_tests/test_host_wait_end_to_end.py` against a running orchestrator — the tester opted for an in-process variant at `orchestrator/tests/test_host_wait_integration.py` instead. The rationale in the module docstring is reasonable (sandbox-unfriendly) but the PR description should note this deviation so a follow-up issue can capture the live-stack variant as future work.

### Matrix status after my ACK

With this ACK I'll have ACKed all three assigned producers (coder v2, documenter v2, tester v1). Ready to confirm.


````yaml
id: ba17ed5d-9d4a-41
phase: implement
metadata:
  payload:
    artifact_references:
    - orchestrator/tests/test_cli.py
    - orchestrator/tests/test_events_event_sequence.py
    - orchestrator/tests/test_host_wait_integration.py
    - orchestrator/tests/test_mcp_tools.py
    - orchestrator/tests/test_pipelines_status_wait_route.py
    reason: "\nReviewed tester commit 3f00ad631 \u2014 test coverage for TASK-4-1\
      \ through TASK-4-5 plus the TASK-1-4 regression fix for `TestWaitressSizing`.\
      \ No blocking issues.\n\n### What I verified\n\n**Five test files, all pass\
      \ `ast.parse`:**\n- `orchestrator/tests/test_pipelines_status_wait_route.py`\
      \ (432 lines) \u2014 applied verbatim from the coder handoff under `.egg-state/agent-outputs/1932-coder-tests/`.\
      \ 16 cases: cursor roundtrip + empty + missing halves + 4 malformed patterns,\
      \ timeout envelope shape, PHASE_STARTED wake, DECISION_RESOLVED exclusion (critical\
      \ \u2014 pins the self-wake prevention), `since` cursor replay skip, OVERSEER_ALERT\
      \ message-bus wake with `_apply_delphi_filter` passthrough, 400 malformed cursor,\
      \ 404 unknown pipeline, 400 invalid wait, gauge lifecycle, queue-full burst.\
      \ Uses `EventBus(async_delivery=False)` fixture for deterministic handler firing.\n\
      - `orchestrator/tests/test_events_event_sequence.py` (114 lines) \u2014 applied\
      \ from handoff. 7 cases including the critical 100-publish/8-thread monotonicity\
      \ test that establishes no-gap, no-duplicate guarantees for the `_sequence`\
      \ counter under contention.\n- `orchestrator/tests/test_mcp_tools.py` +247 lines\
      \ \u2014 new `TestWaitForStatusChange` class (7 sub-cases for handler dispatch,\
      \ no_change passthrough, envelope merge on event/message triggers, since URL-encoding,\
      \ empty-since omission) and `TestBuildStatusSnapshotRefactor` class (byte-identical\
      \ equivalence to pre-refactor `_handle_get_status` output \u2014 pins TASK-2-2\
      \ is pure extraction). Also extends `TestToolRouting.test_all_tools_registered`\
      \ to include `wait_for_status_change` in the expected set.\n- `orchestrator/tests/test_mcp_tools.py::TestGetStatusWait::test_wait_for_status_change_does_not_double_sleep`\
      \ \u2014 this is the R16 pin for TASK-4-4. Patches `mcp_server._async_sleep`\
      \ with `AsyncMock`, dispatches `wait_for_status_change` through `_apply_get_status_wait`,\
      \ asserts the mock is never invoked AND `kwargs[\"wait\"]` is preserved (i.e.\
      \ the tool handler still sees it). Correctly pins the `tool_name == 'get_status'`\
      \ short-circuit. I also verified the test's assertion will fail correctly if\
      \ the guard is removed \u2014 removing `if tool_name != \"get_status\": return`\
      \ from `mcp_server.py:61-62` would let the `await _async_sleep(...)` line fire\
      \ on `wait_for_status_change` calls, and `mock_sleep.assert_not_called()` would\
      \ raise.\n- `orchestrator/tests/test_host_wait_integration.py` (402 lines, 6\
      \ cases) \u2014 the sandbox-friendly integration test. Exercises the full MCP\
      \ handler \u2192 Flask route \u2192 EventBus/message-store chain without needing\
      \ Docker or a live orchestrator. The plan's `integration_tests/test_host_wait_end_to_end.py`\
      \ (live stack) is consciously out of scope; the rationale is documented in the\
      \ module docstring. The cursor round-trip case (sub-case 4) directly tests the\
      \ R2 race-window closure semantics: a call returns cursor evt:N; a second call\
      \ with `since=evt:N` skips events at-or-below N but still wakes on events >\
      \ N. This is the intended mitigation surface and the test pins it.\n\n**Regression\
      \ fix:**\n- `orchestrator/tests/test_cli.py::TestWaitressSizing` \u2014 renamed\
      \ `test_default_threads_is_16` \u2192 `test_default_threads_is_24`, updated\
      \ assertion from 16 \u2192 24, updated docstring to cite the TASK-1-4 rationale.\
      \ The malformed-threads-fallback case was similarly updated. Other cases (refuse-to-boot,\
      \ boundary-3/4, env-var-override, channel-timeout) are unchanged \u2014 floor/boundary/override\
      \ semantics did not move in TASK-1-4. Spot-checked that the renaming is consistent.\n\
      \n**Test execution claim:**\nThe commit message reports `4397 passed, 1 skipped`\
      \ on HEAD+commit, with the 31/27 pre-existing failures / errors attributed to\
      \ unrelated issues (kubernetes.client.VersionApi, gateway_client fixture, deployment_routes\
      \ mocking) \u2014 verified by stashing the diff and rerunning. I cannot re-run\
      \ pytest inside this sandbox (no full orchestrator test environment), but the\
      \ attestation is specific and falsifiable (names the failing classes), the applied\
      \ test files parse cleanly, and the test logic matches the plan's acceptance\
      \ criteria.\n\n### Non-blocking observations\n\n1. **Cursor round-trip sub-case\
      \ 4 could be more adversarial.** The test fires a single event between the two\
      \ calls and checks cursor-skip behaviour. Consider adding a variant where TWO\
      \ events fire between calls \u2014 one at-or-below the cursor (should be skipped),\
      \ one above (should wake). The current test only covers the \"skip\" direction.\n\
      2. **No test for the `limit=100` message-page boundary.** If an `OVERSEER_ALERT`\
      \ storm fires >100 matching messages before the wake, the route returns only\
      \ the first 100 and the cursor advances to that last message. Not a blocker;\
      \ the pagination semantics are inherited from `message_store.get_messages` and\
      \ are tested there.\n3. **Handoff integration is clean.** The coder's handoff\
      \ files under `.egg-state/agent-outputs/1932-coder-tests/` were applied verbatim\
      \ (same line counts, same test names). This is the correct workflow given the\
      \ gateway's file-role policy blocking `coder` from `orchestrator/tests/**`.\n\
      4. **No integration test against a live orchestrator.** Plan TASK-4-5 specifies\
      \ `integration_tests/test_host_wait_end_to_end.py` against a running orchestrator\
      \ \u2014 the tester opted for an in-process variant at `orchestrator/tests/test_host_wait_integration.py`\
      \ instead. The rationale in the module docstring is reasonable (sandbox-unfriendly)\
      \ but the PR description should note this deviation so a follow-up issue can\
      \ capture the live-stack variant as future work.\n\n### Matrix status after\
      \ my ACK\n\nWith this ACK I'll have ACKed all three assigned producers (coder\
      \ v2, documenter v2, tester v1). Ready to confirm.\n"
  version: 1
````

### [2026-04-24T04:36:09Z] orchestrator → tester (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 1). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 853c7ffe-387e-45
phase: implement
metadata:
  fully_acked: true
  version: 1
````

### [2026-04-24T04:36:12Z] reviewer_code (CONSENSUS_CONFIRMED): Confirmed by reviewer_code

````yaml
id: 5a8a57ae-8f53-43
phase: implement
metadata:
  consensus_reached: false
````

### [2026-04-24T04:36:13Z] tester (CONSENSUS_CONFIRMED): Confirmed by tester (pending_acks)

Reviewer tester cannot confirm: NACKed producers have re-proposed since your NACK. Re-review their latest proposal before confirming: ['coder']

````yaml
id: 79461fe4-1e13-4e
phase: implement
metadata:
  pending_acks: true
````

### [2026-04-24T04:37:11Z] tester → coder (CONSENSUS_ACK): ACK from tester for coder


Reviewed the v2 coder re-proposal (commit ac99f9c1c — "Fix lint/format issues flagged by tester NACK"). All three blocking items from my v1 NACK are resolved:

### v1 NACK items — resolved

1. **F401 unused `_EventType`** — both the primary import at line 2549 and the fallback at line 2553 in `wait_pipeline_status` are gone. The try/except now only imports `get_event_bus`. The new comment at the top of the try explains why the `EventType` class is not imported (event-type matching is done via string value against `_STATUS_WAIT_EVENT_TYPES`). Correct fix.
2. **UP037 quoted type annotation** — `wake_q: "_queue.Queue[tuple[str, Any]]"` → `wake_q: _queue.Queue[tuple[str, Any]]` on line 2575. py313 supports generic subscription on `queue.Queue` at runtime so no forward-reference quoting is needed. Correct fix.
3. **`ruff format --check` on pipelines.py** — the two line-break-for-length cases (parsed-since call at 2536, 500 error-return at 2561) are collapsed per ruff's preference. Correct fix.

Bonus: The non-blocking observation about `.egg-state/agent-outputs/1932-coder-tests/` format drift was ALSO addressed — coder ran `ruff format` on the three handoff test files so operators running `ruff format --check .` from the repo root no longer see drift. Clean working tree.

### Verification against v2 HEAD (ac99f9c1c)

- `ruff check .` → All checks passed
- `ruff format --check .` → 752 files already formatted (0 reformat needed)
- Test suite on v2 HEAD + my committed test files (5 files): 55 passed, 0 failed, 0 errors. Covers:
  * test_events_event_sequence.py — 7 cases (sequence field + concurrent monotonicity)
  * test_pipelines_status_wait_route.py — 16 cases (route end-to-end)
  * test_host_wait_integration.py — 6 cases (MCP → route chain)
  * test_mcp_tools.py::TestGetStatusWait — 8 cases (inc. new double-sleep regression)
  * test_mcp_tools.py::TestWaitForStatusChange — 6 cases (handler dispatch + envelope merge)
  * test_mcp_tools.py::TestBuildStatusSnapshotRefactor — 1 case (refactor equivalence)
  * test_mcp_tools.py::TestToolRouting — 3 cases (tool-set update)
  * test_cli.py::TestWaitressSizing — 8 cases (24-default bump regression fix)

### Non-blocking observation still open

The race-window observation from my v1 NACK remains open in scope — the route does not inspect `event_bus._history` on entry, so events fired in the gap between a prior call's `finally` unsubscribe and the current call's `subscribe` are lost. My integration test pins the suppression-of-already-seen-events direction, which is what the current implementation closes. The replay-missed-events direction would require a follow-up. Tracked in the test module docstring; not blocking this PR per the aspirational liveness-floor + overseer-as-primary-deadlock-detector argument in the plan.

Confirming.


````yaml
id: b42024dc-bcac-48
phase: implement
metadata:
  payload:
    artifact_references:
    - orchestrator/routes/pipelines.py
    - .egg-state/agent-outputs/1932-coder-tests/test_pipelines_status_wait_route.py
    - .egg-state/agent-outputs/1932-coder-tests/test_events_event_sequence.py
    - .egg-state/agent-outputs/1932-coder-tests/test_mcp_tools_additions.py
    reason: "\nReviewed the v2 coder re-proposal (commit ac99f9c1c \u2014 \"Fix lint/format\
      \ issues flagged by tester NACK\"). All three blocking items from my v1 NACK\
      \ are resolved:\n\n### v1 NACK items \u2014 resolved\n\n1. **F401 unused `_EventType`**\
      \ \u2014 both the primary import at line 2549 and the fallback at line 2553\
      \ in `wait_pipeline_status` are gone. The try/except now only imports `get_event_bus`.\
      \ The new comment at the top of the try explains why the `EventType` class is\
      \ not imported (event-type matching is done via string value against `_STATUS_WAIT_EVENT_TYPES`).\
      \ Correct fix.\n2. **UP037 quoted type annotation** \u2014 `wake_q: \"_queue.Queue[tuple[str,\
      \ Any]]\"` \u2192 `wake_q: _queue.Queue[tuple[str, Any]]` on line 2575. py313\
      \ supports generic subscription on `queue.Queue` at runtime so no forward-reference\
      \ quoting is needed. Correct fix.\n3. **`ruff format --check` on pipelines.py**\
      \ \u2014 the two line-break-for-length cases (parsed-since call at 2536, 500\
      \ error-return at 2561) are collapsed per ruff's preference. Correct fix.\n\n\
      Bonus: The non-blocking observation about `.egg-state/agent-outputs/1932-coder-tests/`\
      \ format drift was ALSO addressed \u2014 coder ran `ruff format` on the three\
      \ handoff test files so operators running `ruff format --check .` from the repo\
      \ root no longer see drift. Clean working tree.\n\n### Verification against\
      \ v2 HEAD (ac99f9c1c)\n\n- `ruff check .` \u2192 All checks passed\n- `ruff\
      \ format --check .` \u2192 752 files already formatted (0 reformat needed)\n\
      - Test suite on v2 HEAD + my committed test files (5 files): 55 passed, 0 failed,\
      \ 0 errors. Covers:\n  * test_events_event_sequence.py \u2014 7 cases (sequence\
      \ field + concurrent monotonicity)\n  * test_pipelines_status_wait_route.py\
      \ \u2014 16 cases (route end-to-end)\n  * test_host_wait_integration.py \u2014\
      \ 6 cases (MCP \u2192 route chain)\n  * test_mcp_tools.py::TestGetStatusWait\
      \ \u2014 8 cases (inc. new double-sleep regression)\n  * test_mcp_tools.py::TestWaitForStatusChange\
      \ \u2014 6 cases (handler dispatch + envelope merge)\n  * test_mcp_tools.py::TestBuildStatusSnapshotRefactor\
      \ \u2014 1 case (refactor equivalence)\n  * test_mcp_tools.py::TestToolRouting\
      \ \u2014 3 cases (tool-set update)\n  * test_cli.py::TestWaitressSizing \u2014\
      \ 8 cases (24-default bump regression fix)\n\n### Non-blocking observation still\
      \ open\n\nThe race-window observation from my v1 NACK remains open in scope\
      \ \u2014 the route does not inspect `event_bus._history` on entry, so events\
      \ fired in the gap between a prior call's `finally` unsubscribe and the current\
      \ call's `subscribe` are lost. My integration test pins the suppression-of-already-seen-events\
      \ direction, which is what the current implementation closes. The replay-missed-events\
      \ direction would require a follow-up. Tracked in the test module docstring;\
      \ not blocking this PR per the aspirational liveness-floor + overseer-as-primary-deadlock-detector\
      \ argument in the plan.\n\nConfirming.\n"
  version: 2
````

### [2026-04-24T04:37:11Z] orchestrator → coder (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 2). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 07ebc7f4-ab88-4f
phase: implement
metadata:
  fully_acked: true
  version: 2
````

### [2026-04-24T04:37:14Z] tester (CONSENSUS_CONFIRMED): Confirmed by tester

````yaml
id: 8853450e-f4fb-40
phase: implement
metadata:
  consensus_reached: false
````

### [2026-04-24T04:37:16Z] coder (CONSENSUS_CONFIRMED): Confirmed by coder

````yaml
id: 2eefa17e-b06a-4f
phase: implement
metadata:
  consensus_reached: false
````

### [2026-04-24T04:37:26Z] documenter (CONSENSUS_CONFIRMED): Confirmed by documenter

````yaml
id: 8ae94008-d629-40
phase: implement
metadata:
  consensus_reached: true
````
