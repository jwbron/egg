# BRC Consensus History — refine phase

Generated: 2026-04-24T00:23:03Z
Pipeline: issue-1932

### [2026-04-24T00:10:25Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Waiting for refiner's CONSENSUS_PROPOSE. Draft `.egg-state/drafts/1932-analysis.md` does not yet exist on the branch.

````yaml
id: 6989f5e9-3a7a-43
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
````

### [2026-04-24T00:11:10Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_ON_ROLE

````yaml
id: 6e08ec97-f3a2-4e
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
````

### [2026-04-24T00:19:03Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Refine phase analysis for issue #1932 (event-driven wake for SDLC monitor loop). Draft at .egg-state/drafts/1932-analysis.md (426 lines) covers: (1) problem — get_status(wait=25) blind sleep in orchestrator/mcp_server.py:50-67 burns tokens and delays reaction; (2) server primitives from #1919 already in place (messages/wait endpoint, XREAD BLOCK, EventBus phase.*/decision.created/pipeline.*); (3) four options — new `wait_for_status_change` sibling tool (recommended), retrofit `get_status`, SSE, client-side-only; (4) recommended Option A with event set from the issue body; (5) open risks (host-originated event races, backend parity, prompt drift). Registered 7 HITL decisions + 1 feedback request (6 questions) via egg-contract MCP. Complexity: medium.

````yaml
id: b71eae51-d7f5-45
phase: refine
metadata:
  payload:
    summary: "Refine phase analysis for issue #1932 (event-driven wake for SDLC monitor\
      \ loop). Draft at .egg-state/drafts/1932-analysis.md (426 lines) covers: (1)\
      \ problem \u2014 get_status(wait=25) blind sleep in orchestrator/mcp_server.py:50-67\
      \ burns tokens and delays reaction; (2) server primitives from #1919 already\
      \ in place (messages/wait endpoint, XREAD BLOCK, EventBus phase.*/decision.created/pipeline.*);\
      \ (3) four options \u2014 new `wait_for_status_change` sibling tool (recommended),\
      \ retrofit `get_status`, SSE, client-side-only; (4) recommended Option A with\
      \ event set from the issue body; (5) open risks (host-originated event races,\
      \ backend parity, prompt drift). Registered 7 HITL decisions + 1 feedback request\
      \ (6 questions) via egg-contract MCP. Complexity: medium."
    attestation: {}
    artifacts:
    - .egg-state/drafts/1932-analysis.md
    risk_considered: ''
    commit_sha: 23eebdfb9
    files_changed:
    - .egg-state/drafts/1932-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 1
  commit_sha: 23eebdfb9
````

### [2026-04-24T00:19:06Z] refiner (HEARTBEAT): heartbeat: PROPOSED

refiner proposal v1 for #1932 analysis, awaiting review from reviewer_refine and reviewer_agent_design

````yaml
id: 4e7d9445-5a9d-48
phase: refine
metadata:
  state: PROPOSED
````

### [2026-04-24T00:20:31Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Reviewed `.egg-state/drafts/1932-analysis.md` against the agent-mode design criteria and cross-checked cited code at `orchestrator/mcp_server.py:40-67` and `skills/sdlc/SKILL.md:313-347`. No agent-mode anti-patterns found.

### Agent-design assessment (approve)

**Correct use of MCP tool surface.** The recommended Option A introduces a new MCP tool `wait_for_status_change` that returns structured output to the LLM-host (Claude Code running the SDLC skill). This is the MCP-native pattern: the server surfaces event state, the LLM still orchestrates the monitor loop. No post-processing pipeline, no parsing of LLM output on the server — just a better wake primitive.

**Orchestration stays in the agent.** The SDLC skill's Phase 3/S5 loops continue to drive the monitor behaviour; the refactor only swaps the wait primitive. SKILL.md churn is localized to §Phase 3 step 1, §Phase S5 step 1, and the §MCP Tools Reference (draft lines 322-325). This is a design that *extends* the agent's capability rather than constraining it.

**Correct rejection of anti-patterns in alternatives.** The cons against Option C and Option D show explicit agent-mode thinking:
- Option C rejected because Streamable-HTTP MCP does not stream tool responses (`mcp_server.py:159-176`) — correct engagement with how agents actually consume MCP output.
- Option D rejected with "LLMs are unreliable at canonical hashing" (draft line 288) — correct recognition that deterministic client-side logic belongs on the server, not in the prompt.

**No excessive pre-fetching.** The analysis cites short, targeted code snippets (max ~8 lines) for orientation only — not baking large diffs into a future agent prompt. The MCP tool's timeout envelope is intentionally minimal (`{changed, current_phase, status, phase_elapsed_seconds}`) to keep dashboard re-render cheap — a design choice that *reduces* tokens rather than inflating context.

**HITL decisions properly deferred.** Seven multiple-choice decisions and six open-ended feedback questions are registered via `add-decision` / `add-feedback` (draft lines 364-397). Notably the timeout-envelope-shape question (line 375) is explicitly deferred — good separation of refine-phase exploration from plan-phase commitment.

**Liveness + race analysis is agent-aware.** Open risks (draft lines 336-356) correctly identify:
- Host-originated `DECISION_RESOLVED` self-wake (the agent would re-render the resolved decision if not filtered) — an agent-loop race, not a server bug.
- `from_tip=True` semantics from #1925 covering the steady-state case but exposing a transition race.
- The 25s hard cap naturally enforcing the 60s liveness floor via loop composition.

### Non-blocking observations

- **Draft line 216-218**: The Option A con "SDLC skill must gain branching logic (first poll vs subsequent poll) — risk of LLM drift on the boundary" overstates the risk. `SKILL.md:317-319` already distinguishes "First poll" vs "Subsequent polls" today; swapping the subsequent-poll tool name does not introduce new branching. Worth noting in the plan phase so the implementer does not over-engineer around a non-issue.

- **Draft line 375 (envelope shape decision)**: From an agent-design standpoint, the "Full envelope" option is the lower-risk default — zero SKILL.md branching on a `changed` flag means less LLM drift on every iteration. The "Minimal envelope" saves tokens on timeout but asks the LLM to reliably branch on a boolean every loop. Worth flagging this tradeoff explicitly for the HITL answer.

- **Draft line 383 (EventBus vs message-type long-poll)**: "Both" is the right recommendation, but the plan phase should name which subsystem is authoritative for each event type so the implementer does not end up with two paths firing on the same event (double-wake → wasted host tokens — the exact problem this issue is solving).


````yaml
id: 0eadc36c-1624-47
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1932-analysis.md
    - skills/sdlc/SKILL.md
    - orchestrator/mcp_server.py
    reason: "\nReviewed `.egg-state/drafts/1932-analysis.md` against the agent-mode\
      \ design criteria and cross-checked cited code at `orchestrator/mcp_server.py:40-67`\
      \ and `skills/sdlc/SKILL.md:313-347`. No agent-mode anti-patterns found.\n\n\
      ### Agent-design assessment (approve)\n\n**Correct use of MCP tool surface.**\
      \ The recommended Option A introduces a new MCP tool `wait_for_status_change`\
      \ that returns structured output to the LLM-host (Claude Code running the SDLC\
      \ skill). This is the MCP-native pattern: the server surfaces event state, the\
      \ LLM still orchestrates the monitor loop. No post-processing pipeline, no parsing\
      \ of LLM output on the server \u2014 just a better wake primitive.\n\n**Orchestration\
      \ stays in the agent.** The SDLC skill's Phase 3/S5 loops continue to drive\
      \ the monitor behaviour; the refactor only swaps the wait primitive. SKILL.md\
      \ churn is localized to \xA7Phase 3 step 1, \xA7Phase S5 step 1, and the \xA7\
      MCP Tools Reference (draft lines 322-325). This is a design that *extends* the\
      \ agent's capability rather than constraining it.\n\n**Correct rejection of\
      \ anti-patterns in alternatives.** The cons against Option C and Option D show\
      \ explicit agent-mode thinking:\n- Option C rejected because Streamable-HTTP\
      \ MCP does not stream tool responses (`mcp_server.py:159-176`) \u2014 correct\
      \ engagement with how agents actually consume MCP output.\n- Option D rejected\
      \ with \"LLMs are unreliable at canonical hashing\" (draft line 288) \u2014\
      \ correct recognition that deterministic client-side logic belongs on the server,\
      \ not in the prompt.\n\n**No excessive pre-fetching.** The analysis cites short,\
      \ targeted code snippets (max ~8 lines) for orientation only \u2014 not baking\
      \ large diffs into a future agent prompt. The MCP tool's timeout envelope is\
      \ intentionally minimal (`{changed, current_phase, status, phase_elapsed_seconds}`)\
      \ to keep dashboard re-render cheap \u2014 a design choice that *reduces* tokens\
      \ rather than inflating context.\n\n**HITL decisions properly deferred.** Seven\
      \ multiple-choice decisions and six open-ended feedback questions are registered\
      \ via `add-decision` / `add-feedback` (draft lines 364-397). Notably the timeout-envelope-shape\
      \ question (line 375) is explicitly deferred \u2014 good separation of refine-phase\
      \ exploration from plan-phase commitment.\n\n**Liveness + race analysis is agent-aware.**\
      \ Open risks (draft lines 336-356) correctly identify:\n- Host-originated `DECISION_RESOLVED`\
      \ self-wake (the agent would re-render the resolved decision if not filtered)\
      \ \u2014 an agent-loop race, not a server bug.\n- `from_tip=True` semantics\
      \ from #1925 covering the steady-state case but exposing a transition race.\n\
      - The 25s hard cap naturally enforcing the 60s liveness floor via loop composition.\n\
      \n### Non-blocking observations\n\n- **Draft line 216-218**: The Option A con\
      \ \"SDLC skill must gain branching logic (first poll vs subsequent poll) \u2014\
      \ risk of LLM drift on the boundary\" overstates the risk. `SKILL.md:317-319`\
      \ already distinguishes \"First poll\" vs \"Subsequent polls\" today; swapping\
      \ the subsequent-poll tool name does not introduce new branching. Worth noting\
      \ in the plan phase so the implementer does not over-engineer around a non-issue.\n\
      \n- **Draft line 375 (envelope shape decision)**: From an agent-design standpoint,\
      \ the \"Full envelope\" option is the lower-risk default \u2014 zero SKILL.md\
      \ branching on a `changed` flag means less LLM drift on every iteration. The\
      \ \"Minimal envelope\" saves tokens on timeout but asks the LLM to reliably\
      \ branch on a boolean every loop. Worth flagging this tradeoff explicitly for\
      \ the HITL answer.\n\n- **Draft line 383 (EventBus vs message-type long-poll)**:\
      \ \"Both\" is the right recommendation, but the plan phase should name which\
      \ subsystem is authoritative for each event type so the implementer does not\
      \ end up with two paths firing on the same event (double-wake \u2192 wasted\
      \ host tokens \u2014 the exact problem this issue is solving).\n"
  version: 1
````

### [2026-04-24T00:20:36Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: e737411b-c3e4-47
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T00:22:51Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Reviewed `.egg-state/drafts/1932-analysis.md` (426 lines) and spot-verified every cited file/line range against HEAD. Verified all 7 HITL decisions and the 6-question feedback block are registered via `mcp__sdlc__check_hitl_answers` — contract matches the prose 1:1.

## Section-by-section assessment

### 1. Problem Understanding — STRONG
Captures the token-waste and reaction-latency problems precisely. Correctly identifies that the 25s cap is transport-enforced (anthropics/claude-code#20335), not a tuning knob, and is out-of-scope per the issue. Calls out OVERSEER_ALERT delay specifically, which matches the user-facing impact described in `SKILL.md:365-397`.

### 2. Research Quality — STRONG
Citations I verified:
- `mcp_server.py:50-67` (`_apply_get_status_wait`) — ✓ accurate, `GET_STATUS_MAX_WAIT = 25` sits at the line number shown
- `mcp_tools.py:1548-1655` (`_handle_get_status`) — ✓ handler starts at 1548, does the REST fetches + `/messages?limit=10` enrichment as described
- `mcp_tools.py:277-304` (`get_status` schema) — ✓ matches
- `routes/messages.py:347-436` (`wait_messages`) — ✓ route starts at 347, uses `from_tip=since_id is None` as described
- `redis_message_store.py:158-329` — ✓ `get_messages` with `wait_for_types` is at 158, inner-loop cap `_WAIT_FOR_TYPES_MAX_INNER_LOOPS` exists
- `cli.py:280-310` — ✓ Waitress thread config and `channel_timeout = max(poll_cap * 2 + 30, 120)` match
- `routes/pipelines.py:12002-12062` SSE stream — ✓ `stream_pipeline` at 12002
- `routes/pipelines.py:11029` decision.created emit — ✓ `_emit_pipeline_event(pipeline, "decision.created")` at 11029
- `gateway/squid.conf:135-137` read_timeout — ✓
- `docs/reference/agent-wait-patterns.md:18-80` canonical idiom — ✓

### 3. Options Analysis — STRONG
Four options (A: new sibling tool, B: retrofit `get_status`, C: SSE, D: shorter polls+hash) are meaningfully different and pros/cons are concrete. Option C's con ("FastMCP binding returns a single JSON string from each tool call (`mcp_server.py:159-176`)") is a real constraint — correctly kills SSE as a viable path. Option D's criticism ("5s cadence still burns tokens") is fair. The table-form trigger set in the recommendation is a nice forcing function for the plan phase.

### 4. Constraints and Dependencies — STRONG
Covers: transport timeout, Squid not in host path but still worth noting, Waitress pool budget, backend parity (Redis vs in-memory), additive/back-compat, liveness floor, dedup coexistence, MCP tool surface versioned via prompts, HITL decision wake-up race. The observation that "the host-side wait must stay ≤ 25s regardless" of Squid is correct — host talks MCP directly, not via the gateway.

### 5. Open Questions — STRONG
All 7 decisions and 6 feedback questions in the contract, verified via `mcp__sdlc__check_hitl_answers`. Questions are specific, actionable, and cover the genuinely-ambiguous axes: tool shape (A/B/C/D), trigger set (minimal/issue-as-written/maximal), envelope shape, `since` cursor, EventBus vs message-bus source, `recent_messages` fetch policy, self-wake handling for `provide_input`. Feedback Q2 on concurrency load and Q4 on the 60s liveness-floor interpretation are exactly the right open-ended questions.

### 6. Recommendation Quality — STRONG
Option A is justified with four specific reasons (back-compat, reuse of #1919 primitives, minimal no-change payload affordance, localized prompt churn). The "Open risks" subsection surfaces three real issues (host-sent input race, event-backlog race on first transition, consensus trigger set fidelity) and defers them to plan phase — the right move.

### 7. HITL Decision Registration — VERIFIED
7/7 decisions registered as structured HITL items in the contract; all have `resolved: false` and correct option lists. Feedback block has 6 questions. No "open questions exist as prose but weren't registered" gap.

## Non-blocking observations

- **Phase number mislabel (cosmetic).** The opening paragraph says *"Phase 3 (Monitor) and Phase S3 (Short-flow Monitor)"*, but the actual heading in `skills/sdlc/SKILL.md:1174` is **"## Phase S5 — Monitor"**. The issue body itself uses the wrong "Phase S3" label, so the refiner inherited the error. Later in the analysis (Current Behavior and Constraints sections) the correct "§Phase S5 (lines 1174-1206)" citation appears — so the analysis is internally inconsistent. Fix in a future pass: replace "Phase S3" → "Phase S5" in the Problem Statement opening, and consider noting in the issue that the original reference was wrong.
- **Liveness-floor reframing.** The Recommended Approach reframes the issue's explicit 60s liveness floor as "the 25s hard cap on each call" / "the loop structure naturally enforces the floor". This is *almost* right — per-call wall-clock is ≤25s, but aggregate quiet time depends on how fast the LLM between calls renders the dashboard + reissues the tool. On a slow LLM pass an idle pipeline could exceed 60s total quiet time. Feedback Q4 asks whether the 60s is literal vs aspirational, so this is captured as a HITL question — just flagging that the recommendation's parenthetical leans "aspirational" while the issue body reads "literal".
- **Rate-limiting not enumerated.** `mcp_server.RateLimiter` (30 req/min default) is adjacent to `_apply_get_status_wait`. The new `wait_for_status_change` will inherit it by default, but whether long-poll tools should share the same bucket as one-shot tools is a real question. Could be surfaced as a plan-phase concern or an additional open feedback question.
- **Test surface sketch.** The complexity assessment says "tests" without naming the target suites. For a plan-phase handoff it'd help to name them: `orchestrator/tests/test_mcp_tools.py` for tool schema + handler, `orchestrator/tests/test_wait_endpoint.py` for the new route, `tests/test_sdlc_skill_*` for the SKILL.md contract. Non-blocking — plan phase can enumerate.
- **`RateLimiter` sharing question + initial-call race.** Option A says "call `get_status` once at loop start, `wait_for_status_change` on subsequent iterations". Decision-4 captures the `since` cursor race, but the narrower variant "what event ID does the host pass on the first `wait_for_status_change`? The response ID from the preceding `get_status` doesn't exist because `get_status` doesn't return a message-stream cursor today" isn't spelled out. Plan phase concern; flagging for visibility.

Overall quality is high. The research is unusually specific and verifiable, the options are well-differentiated, the recommendation is justified, and HITL registration is complete. Refine phase is ready to advance once the decisions/feedback are answered by a human.


````yaml
id: 7e0f32ad-fda5-47
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1932-analysis.md
    - .egg-state/contracts/issue-1932.json
    - skills/sdlc/SKILL.md
    - orchestrator/mcp_server.py
    - orchestrator/mcp_tools.py
    - orchestrator/routes/messages.py
    - orchestrator/routes/pipelines.py
    - orchestrator/cli.py
    - orchestrator/redis_message_store.py
    - docs/reference/agent-wait-patterns.md
    - gateway/squid.conf
    reason: "\nReviewed `.egg-state/drafts/1932-analysis.md` (426 lines) and spot-verified\
      \ every cited file/line range against HEAD. Verified all 7 HITL decisions and\
      \ the 6-question feedback block are registered via `mcp__sdlc__check_hitl_answers`\
      \ \u2014 contract matches the prose 1:1.\n\n## Section-by-section assessment\n\
      \n### 1. Problem Understanding \u2014 STRONG\nCaptures the token-waste and reaction-latency\
      \ problems precisely. Correctly identifies that the 25s cap is transport-enforced\
      \ (anthropics/claude-code#20335), not a tuning knob, and is out-of-scope per\
      \ the issue. Calls out OVERSEER_ALERT delay specifically, which matches the\
      \ user-facing impact described in `SKILL.md:365-397`.\n\n### 2. Research Quality\
      \ \u2014 STRONG\nCitations I verified:\n- `mcp_server.py:50-67` (`_apply_get_status_wait`)\
      \ \u2014 \u2713 accurate, `GET_STATUS_MAX_WAIT = 25` sits at the line number\
      \ shown\n- `mcp_tools.py:1548-1655` (`_handle_get_status`) \u2014 \u2713 handler\
      \ starts at 1548, does the REST fetches + `/messages?limit=10` enrichment as\
      \ described\n- `mcp_tools.py:277-304` (`get_status` schema) \u2014 \u2713 matches\n\
      - `routes/messages.py:347-436` (`wait_messages`) \u2014 \u2713 route starts\
      \ at 347, uses `from_tip=since_id is None` as described\n- `redis_message_store.py:158-329`\
      \ \u2014 \u2713 `get_messages` with `wait_for_types` is at 158, inner-loop cap\
      \ `_WAIT_FOR_TYPES_MAX_INNER_LOOPS` exists\n- `cli.py:280-310` \u2014 \u2713\
      \ Waitress thread config and `channel_timeout = max(poll_cap * 2 + 30, 120)`\
      \ match\n- `routes/pipelines.py:12002-12062` SSE stream \u2014 \u2713 `stream_pipeline`\
      \ at 12002\n- `routes/pipelines.py:11029` decision.created emit \u2014 \u2713\
      \ `_emit_pipeline_event(pipeline, \"decision.created\")` at 11029\n- `gateway/squid.conf:135-137`\
      \ read_timeout \u2014 \u2713\n- `docs/reference/agent-wait-patterns.md:18-80`\
      \ canonical idiom \u2014 \u2713\n\n### 3. Options Analysis \u2014 STRONG\nFour\
      \ options (A: new sibling tool, B: retrofit `get_status`, C: SSE, D: shorter\
      \ polls+hash) are meaningfully different and pros/cons are concrete. Option\
      \ C's con (\"FastMCP binding returns a single JSON string from each tool call\
      \ (`mcp_server.py:159-176`)\") is a real constraint \u2014 correctly kills SSE\
      \ as a viable path. Option D's criticism (\"5s cadence still burns tokens\"\
      ) is fair. The table-form trigger set in the recommendation is a nice forcing\
      \ function for the plan phase.\n\n### 4. Constraints and Dependencies \u2014\
      \ STRONG\nCovers: transport timeout, Squid not in host path but still worth\
      \ noting, Waitress pool budget, backend parity (Redis vs in-memory), additive/back-compat,\
      \ liveness floor, dedup coexistence, MCP tool surface versioned via prompts,\
      \ HITL decision wake-up race. The observation that \"the host-side wait must\
      \ stay \u2264 25s regardless\" of Squid is correct \u2014 host talks MCP directly,\
      \ not via the gateway.\n\n### 5. Open Questions \u2014 STRONG\nAll 7 decisions\
      \ and 6 feedback questions in the contract, verified via `mcp__sdlc__check_hitl_answers`.\
      \ Questions are specific, actionable, and cover the genuinely-ambiguous axes:\
      \ tool shape (A/B/C/D), trigger set (minimal/issue-as-written/maximal), envelope\
      \ shape, `since` cursor, EventBus vs message-bus source, `recent_messages` fetch\
      \ policy, self-wake handling for `provide_input`. Feedback Q2 on concurrency\
      \ load and Q4 on the 60s liveness-floor interpretation are exactly the right\
      \ open-ended questions.\n\n### 6. Recommendation Quality \u2014 STRONG\nOption\
      \ A is justified with four specific reasons (back-compat, reuse of #1919 primitives,\
      \ minimal no-change payload affordance, localized prompt churn). The \"Open\
      \ risks\" subsection surfaces three real issues (host-sent input race, event-backlog\
      \ race on first transition, consensus trigger set fidelity) and defers them\
      \ to plan phase \u2014 the right move.\n\n### 7. HITL Decision Registration\
      \ \u2014 VERIFIED\n7/7 decisions registered as structured HITL items in the\
      \ contract; all have `resolved: false` and correct option lists. Feedback block\
      \ has 6 questions. No \"open questions exist as prose but weren't registered\"\
      \ gap.\n\n## Non-blocking observations\n\n- **Phase number mislabel (cosmetic).**\
      \ The opening paragraph says *\"Phase 3 (Monitor) and Phase S3 (Short-flow Monitor)\"\
      *, but the actual heading in `skills/sdlc/SKILL.md:1174` is **\"## Phase S5\
      \ \u2014 Monitor\"**. The issue body itself uses the wrong \"Phase S3\" label,\
      \ so the refiner inherited the error. Later in the analysis (Current Behavior\
      \ and Constraints sections) the correct \"\xA7Phase S5 (lines 1174-1206)\" citation\
      \ appears \u2014 so the analysis is internally inconsistent. Fix in a future\
      \ pass: replace \"Phase S3\" \u2192 \"Phase S5\" in the Problem Statement opening,\
      \ and consider noting in the issue that the original reference was wrong.\n\
      - **Liveness-floor reframing.** The Recommended Approach reframes the issue's\
      \ explicit 60s liveness floor as \"the 25s hard cap on each call\" / \"the loop\
      \ structure naturally enforces the floor\". This is *almost* right \u2014 per-call\
      \ wall-clock is \u226425s, but aggregate quiet time depends on how fast the\
      \ LLM between calls renders the dashboard + reissues the tool. On a slow LLM\
      \ pass an idle pipeline could exceed 60s total quiet time. Feedback Q4 asks\
      \ whether the 60s is literal vs aspirational, so this is captured as a HITL\
      \ question \u2014 just flagging that the recommendation's parenthetical leans\
      \ \"aspirational\" while the issue body reads \"literal\".\n- **Rate-limiting\
      \ not enumerated.** `mcp_server.RateLimiter` (30 req/min default) is adjacent\
      \ to `_apply_get_status_wait`. The new `wait_for_status_change` will inherit\
      \ it by default, but whether long-poll tools should share the same bucket as\
      \ one-shot tools is a real question. Could be surfaced as a plan-phase concern\
      \ or an additional open feedback question.\n- **Test surface sketch.** The complexity\
      \ assessment says \"tests\" without naming the target suites. For a plan-phase\
      \ handoff it'd help to name them: `orchestrator/tests/test_mcp_tools.py` for\
      \ tool schema + handler, `orchestrator/tests/test_wait_endpoint.py` for the\
      \ new route, `tests/test_sdlc_skill_*` for the SKILL.md contract. Non-blocking\
      \ \u2014 plan phase can enumerate.\n- **`RateLimiter` sharing question + initial-call\
      \ race.** Option A says \"call `get_status` once at loop start, `wait_for_status_change`\
      \ on subsequent iterations\". Decision-4 captures the `since` cursor race, but\
      \ the narrower variant \"what event ID does the host pass on the first `wait_for_status_change`?\
      \ The response ID from the preceding `get_status` doesn't exist because `get_status`\
      \ doesn't return a message-stream cursor today\" isn't spelled out. Plan phase\
      \ concern; flagging for visibility.\n\nOverall quality is high. The research\
      \ is unusually specific and verifiable, the options are well-differentiated,\
      \ the recommendation is justified, and HITL registration is complete. Refine\
      \ phase is ready to advance once the decisions/feedback are answered by a human.\n"
  version: 1
````

### [2026-04-24T00:22:51Z] orchestrator → refiner (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 1). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 163ec016-fb47-44
phase: refine
metadata:
  fully_acked: true
  version: 1
````

### [2026-04-24T00:22:59Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: 8ad7e249-4172-4b
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T00:22:59Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: bf0039f0-e384-41
phase: refine
metadata:
  consensus_reached: true
````

### [2026-04-24T00:23:03Z] reviewer_refine (HEARTBEAT): heartbeat: IDLE

````yaml
id: 11313304-0500-4d
phase: refine
metadata:
  state: IDLE
````
