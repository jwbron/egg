# BRC Consensus History — refine phase

Generated: 2026-04-24T00:27:17Z
Pipeline: issue-1917

### [2026-04-24T00:12:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Prepared: contract empty, no prior review feedback, draft file does not yet exist. Waiting for refiner to propose.

````yaml
id: b6a332e0-add2-4a
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
````

### [2026-04-24T00:22:16Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Refine draft for #1917 landed at .egg-state/drafts/1917-analysis.md (commit 2906647cf, 383 lines). Analysis audits iteration 1 (#1765/PR #1920, f24110b71) — confirms 18 SDK-visible tools shipped across 5 namespaces (sdlc/brc/phase/progress/task), 3 of them from #1897's event-driven message primitives. Cross-references the #1765 capability audit at .egg-state/drafts/1765-analysis.md:317-335 against what shipped to identify ~15 remaining iter-2 verbs across 6 semantic groups: contract read/write (per #1955 live evidence — show_contract, add_commit, update_notes, complete_phase, verify_criterion), peer_read_artifact (iter-1 TD9 explicit deferral), checkpoint (list/show/search — 6 total subcommands available), peer messaging (send, poll), overseer alert, task_mark_gap (no CLI/endpoint exists), anchor (init/update/get — REST endpoints at orchestrator/routes/anchors.py exist but no egg-orch anchor CLI despite sandbox/agent-config/rules/orchestrator.md:20-24 referencing it). Enumerates 4 design options (A: ~7 minimum; B: ~15 full audit; C: staged two-PR; D: B+iter-1 carry-over); recommends B with C as safety valve if anchor/task_mark_gap design stalls. Complexity: high. Registered 13 multi-choice decisions (decision-1..13) covering scope shape, anchor approach, checkpoint coverage, task_mark_gap shape, namespace strategy, phase_get_context promotion timing, verify_criterion gating, peer_read_artifact source of truth, EGG_MCP_TOOLS flag fate, harness coverage, rule-doc drift gate, tool-timeout contingencies, and no-CLI policy. Registered feedback-1 with 4 open-ended questions (docs scope, acceptance metric, missed verbs, human-operator-only list publication per AC1.b). Analysis honors AC3 (reuse iteration-1 mechanism: create_sdk_mcp_server + @tool + handlers/ + drift gate), AC1 (every audit verb shipped/documented/superseded), and iter-1 constraints (no sys.exit in handlers, asyncio.to_thread, structured error blocks). No implementation in this phase — analysis only.

````yaml
id: 8cef72af-ef66-4e
phase: refine
metadata:
  payload:
    summary: "Refine draft for #1917 landed at .egg-state/drafts/1917-analysis.md\
      \ (commit 2906647cf, 383 lines). Analysis audits iteration 1 (#1765/PR #1920,\
      \ f24110b71) \u2014 confirms 18 SDK-visible tools shipped across 5 namespaces\
      \ (sdlc/brc/phase/progress/task), 3 of them from #1897's event-driven message\
      \ primitives. Cross-references the #1765 capability audit at .egg-state/drafts/1765-analysis.md:317-335\
      \ against what shipped to identify ~15 remaining iter-2 verbs across 6 semantic\
      \ groups: contract read/write (per #1955 live evidence \u2014 show_contract,\
      \ add_commit, update_notes, complete_phase, verify_criterion), peer_read_artifact\
      \ (iter-1 TD9 explicit deferral), checkpoint (list/show/search \u2014 6 total\
      \ subcommands available), peer messaging (send, poll), overseer alert, task_mark_gap\
      \ (no CLI/endpoint exists), anchor (init/update/get \u2014 REST endpoints at\
      \ orchestrator/routes/anchors.py exist but no egg-orch anchor CLI despite sandbox/agent-config/rules/orchestrator.md:20-24\
      \ referencing it). Enumerates 4 design options (A: ~7 minimum; B: ~15 full audit;\
      \ C: staged two-PR; D: B+iter-1 carry-over); recommends B with C as safety valve\
      \ if anchor/task_mark_gap design stalls. Complexity: high. Registered 13 multi-choice\
      \ decisions (decision-1..13) covering scope shape, anchor approach, checkpoint\
      \ coverage, task_mark_gap shape, namespace strategy, phase_get_context promotion\
      \ timing, verify_criterion gating, peer_read_artifact source of truth, EGG_MCP_TOOLS\
      \ flag fate, harness coverage, rule-doc drift gate, tool-timeout contingencies,\
      \ and no-CLI policy. Registered feedback-1 with 4 open-ended questions (docs\
      \ scope, acceptance metric, missed verbs, human-operator-only list publication\
      \ per AC1.b). Analysis honors AC3 (reuse iteration-1 mechanism: create_sdk_mcp_server\
      \ + @tool + handlers/ + drift gate), AC1 (every audit verb shipped/documented/superseded),\
      \ and iter-1 constraints (no sys.exit in handlers, asyncio.to_thread, structured\
      \ error blocks). No implementation in this phase \u2014 analysis only."
    attestation: {}
    artifacts:
    - .egg-state/drafts/1917-analysis.md
    risk_considered: "Scope risk: full-audit Option B with anchor + task_mark_gap\
      \ requires new orchestrator endpoints and/or CLI scaffolding, which may stall\
      \ in plan phase \u2014 mitigated by explicit Option C staged fallback. Drift\
      \ risk: 17 rule-doc surfaces can silently diverge from the registered tool list\
      \ \u2014 mitigated by decision-11 (CI drift gate proposal). Timeout risk: checkpoint_search/read_peer_artifact\
      \ may hit 60s MCP cap on large data \u2014 mitigated by decision-12 (paginate/triplet/accept).\
      \ Harness risk: EGG_HARNESS=egg parallel wiring still uncovered \u2014 surfaced\
      \ as decision-10. Authz risk on verify_criterion REVIEWER-role gating \u2014\
      \ surfaced as decision-7."
    commit_sha: 2906647cf1719d8fabd247d45791d269d7165578
    files_changed:
    - .egg-state/drafts/1917-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 1
  commit_sha: 2906647cf1719d8fabd247d45791d269d7165578
````

### [2026-04-24T00:24:05Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Reviewed `.egg-state/drafts/1917-analysis.md` (383 lines) against agent-mode design criteria. Verified the proposed verb surface, constraints, and recommended approach do not introduce the standard anti-patterns.

**Clean on agent-mode design**

- **Thesis is pro-agent-design, not anti.** §Problem Statement and §Current Behavior frame the entire iteration around *eliminating* the `egg-contract show --json | python3 -c ...` CLI-via-Bash + JSON-scrape pattern documented in #1955 — that is precisely the post-processing-pipeline anti-pattern (criterion #3), and the draft is removing it, not adding it. (Lines 16-23, 130-139.)
- **No pre-fetching.** All proposed verbs (`show_contract`, `read_peer_artifact`, `checkpoint_*`, etc.) are agent-initiated, on-demand reads. The `read_peer_artifact` verb is per-peer/per-artifact (line 240), not a bulk eager fetch. Nothing bakes large diffs or transcripts into a prompt.
- **Timeout / size awareness is already present.** The 60-s MCP tool-timeout failure mode for `peer_read_artifact` and `checkpoint_search` is explicitly called out in §Constraints (lines 180-184) and §Option B cons (lines 272-273), with pagination or start/poll/complete triplets flagged as contingencies (decision-12, line 362). The plan phase has room to pin this down — the refine phase surfaced it correctly.
- **Authz by construction**, not by prompt. §Constraints (lines 154-157): "Sandbox agents cannot import the orchestrator's MCP tools … orchestrator-privileged operations (spawn, cancel, restart) stay out." That's sandbox-enforced, not prompt-level (criterion #5 inverted — good).
- **Mechanism reuse (AC3) is pinned.** §Constraints (lines 143-150) requires the iter-1 `@tool` wrapper → `handlers/*.py` → gateway pattern. No bypassing of the Agent SDK, no raw Anthropic HTTP calls, no hardcoded model IDs (criteria #6-8 inapplicable/satisfied).
- **Structured output is for the agent, not humans** (criterion #2 inapplicable — these are MCP tools returning data to the agent, not formatting PR comments).
- **No rigid procedures.** The draft proposes capability additions, not step-by-step procedures for the agent to follow (criterion #4 inapplicable).

### Non-blocking
- **`mcp__brc__send_message` / `mcp__brc__poll_messages` tool semantics (Option B P1, table rows 10-11, lines 242-243).** The draft proposes exposing `egg-orch message send` and `message poll` as first-class MCP verbs, but the draft's own BRC protocol context (and the post-#1897 direction) explicitly says: the legacy QUESTION message type was removed, off-protocol chatter is no longer advertised, reviewer clarifications belong in NACK `--reason`, and a structured REQUEST/REPLY subsystem is planned separately. Advertising raw `send`/`poll` as tools without narrowing their intended use — e.g., scoping to HANDOFF/STATUS operator signals, not freeform reviewer→producer chatter — risks re-opening the off-protocol-chatter path that #1897 closed, and stepping on the future REQUEST/REPLY subsystem's design space. **Suggestion for plan phase:** have the architect pin down (a) which message types the two verbs advertise in their tool description, (b) whether the tool `description` should actively steer clarifications back to NACK `--reason`, and (c) whether `send_message` should be deferred to land alongside the structured REQUEST/REPLY subsystem rather than shipping the unrestricted CLI wrapper now. Worth capturing as an additional open question on the contract, or folding into decision-5 (namespace strategy) / decision-13 (CLI-counterpart policy).
- **`mcp__task__complete` vs `mcp__phase__complete_phase` vs `mcp__task__add_commit`** — three task/phase-completion verbs in close proximity (Option B P0, lines 236-239). Agent-UX risk: an agent picks the wrong one. Not a blocker on the analysis (the distinction is real and correct), but the plan phase should specify that the `description` field for each tool names its state-machine effect explicitly (same spirit as #1944 for phase tools) so the agent picks correctly without needing to re-derive the taxonomy.
- **`mcp__sdlc__show_contract` payload shape.** A live contract can run to many KB once decisions, notes, and phase artifacts accumulate. Not pre-fetching (agent-initiated), but the plan phase could consider whether the verb supports field projection (e.g., `fields=["decisions","current_phase"]`) for the common narrow queries, keeping the full dump as an opt-in. Purely a sizing optimisation — no hard anti-pattern.


````yaml
id: 5586d619-b4e2-4b
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1917-analysis.md
    - .egg-state/contracts/issue-1917.json
    reason: "\nReviewed `.egg-state/drafts/1917-analysis.md` (383 lines) against agent-mode\
      \ design criteria. Verified the proposed verb surface, constraints, and recommended\
      \ approach do not introduce the standard anti-patterns.\n\n**Clean on agent-mode\
      \ design**\n\n- **Thesis is pro-agent-design, not anti.** \xA7Problem Statement\
      \ and \xA7Current Behavior frame the entire iteration around *eliminating* the\
      \ `egg-contract show --json | python3 -c ...` CLI-via-Bash + JSON-scrape pattern\
      \ documented in #1955 \u2014 that is precisely the post-processing-pipeline\
      \ anti-pattern (criterion #3), and the draft is removing it, not adding it.\
      \ (Lines 16-23, 130-139.)\n- **No pre-fetching.** All proposed verbs (`show_contract`,\
      \ `read_peer_artifact`, `checkpoint_*`, etc.) are agent-initiated, on-demand\
      \ reads. The `read_peer_artifact` verb is per-peer/per-artifact (line 240),\
      \ not a bulk eager fetch. Nothing bakes large diffs or transcripts into a prompt.\n\
      - **Timeout / size awareness is already present.** The 60-s MCP tool-timeout\
      \ failure mode for `peer_read_artifact` and `checkpoint_search` is explicitly\
      \ called out in \xA7Constraints (lines 180-184) and \xA7Option B cons (lines\
      \ 272-273), with pagination or start/poll/complete triplets flagged as contingencies\
      \ (decision-12, line 362). The plan phase has room to pin this down \u2014 the\
      \ refine phase surfaced it correctly.\n- **Authz by construction**, not by prompt.\
      \ \xA7Constraints (lines 154-157): \"Sandbox agents cannot import the orchestrator's\
      \ MCP tools \u2026 orchestrator-privileged operations (spawn, cancel, restart)\
      \ stay out.\" That's sandbox-enforced, not prompt-level (criterion #5 inverted\
      \ \u2014 good).\n- **Mechanism reuse (AC3) is pinned.** \xA7Constraints (lines\
      \ 143-150) requires the iter-1 `@tool` wrapper \u2192 `handlers/*.py` \u2192\
      \ gateway pattern. No bypassing of the Agent SDK, no raw Anthropic HTTP calls,\
      \ no hardcoded model IDs (criteria #6-8 inapplicable/satisfied).\n- **Structured\
      \ output is for the agent, not humans** (criterion #2 inapplicable \u2014 these\
      \ are MCP tools returning data to the agent, not formatting PR comments).\n\
      - **No rigid procedures.** The draft proposes capability additions, not step-by-step\
      \ procedures for the agent to follow (criterion #4 inapplicable).\n\n### Non-blocking\n\
      - **`mcp__brc__send_message` / `mcp__brc__poll_messages` tool semantics (Option\
      \ B P1, table rows 10-11, lines 242-243).** The draft proposes exposing `egg-orch\
      \ message send` and `message poll` as first-class MCP verbs, but the draft's\
      \ own BRC protocol context (and the post-#1897 direction) explicitly says: the\
      \ legacy QUESTION message type was removed, off-protocol chatter is no longer\
      \ advertised, reviewer clarifications belong in NACK `--reason`, and a structured\
      \ REQUEST/REPLY subsystem is planned separately. Advertising raw `send`/`poll`\
      \ as tools without narrowing their intended use \u2014 e.g., scoping to HANDOFF/STATUS\
      \ operator signals, not freeform reviewer\u2192producer chatter \u2014 risks\
      \ re-opening the off-protocol-chatter path that #1897 closed, and stepping on\
      \ the future REQUEST/REPLY subsystem's design space. **Suggestion for plan phase:**\
      \ have the architect pin down (a) which message types the two verbs advertise\
      \ in their tool description, (b) whether the tool `description` should actively\
      \ steer clarifications back to NACK `--reason`, and (c) whether `send_message`\
      \ should be deferred to land alongside the structured REQUEST/REPLY subsystem\
      \ rather than shipping the unrestricted CLI wrapper now. Worth capturing as\
      \ an additional open question on the contract, or folding into decision-5 (namespace\
      \ strategy) / decision-13 (CLI-counterpart policy).\n- **`mcp__task__complete`\
      \ vs `mcp__phase__complete_phase` vs `mcp__task__add_commit`** \u2014 three\
      \ task/phase-completion verbs in close proximity (Option B P0, lines 236-239).\
      \ Agent-UX risk: an agent picks the wrong one. Not a blocker on the analysis\
      \ (the distinction is real and correct), but the plan phase should specify that\
      \ the `description` field for each tool names its state-machine effect explicitly\
      \ (same spirit as #1944 for phase tools) so the agent picks correctly without\
      \ needing to re-derive the taxonomy.\n- **`mcp__sdlc__show_contract` payload\
      \ shape.** A live contract can run to many KB once decisions, notes, and phase\
      \ artifacts accumulate. Not pre-fetching (agent-initiated), but the plan phase\
      \ could consider whether the verb supports field projection (e.g., `fields=[\"\
      decisions\",\"current_phase\"]`) for the common narrow queries, keeping the\
      \ full dump as an opt-in. Purely a sizing optimisation \u2014 no hard anti-pattern.\n"
  version: 1
````

### [2026-04-24T00:24:10Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: 3adf5d60-dee0-42
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T00:26:24Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Reviewed `.egg-state/drafts/1917-analysis.md` (383 lines) section-by-section against refine-phase criteria, spot-checked every concrete file:line citation, and confirmed HITL registration via `egg-contract show`.

### Section-by-section evaluation

**1. Problem Statement (lines 5–29)** — Excellent. Correctly anchors iter-2 scope to #1765's capability audit, references the landed iter-1 mechanism (commit f24110b71 / PR #1920 verified via `git log`), and evidences live pain with #1955 (verified — issue title: "Agents shell out to `egg-contract show` via Bash"). Desired outcome maps cleanly to the issue's AC1 (a/b/c disposition for every audit verb).

**2. Current Behavior (lines 31–139)** — Strong. All citations spot-checked:
- `sandbox/egg_agent_tools/tools/` — confirmed 6 modules, `REGISTRATIONS` counts per file: brc=6, message=3, phase=2, progress=3, sdlc=3, task=1, total 18. The "18-vs-docs-says-15" doc drift is real (`docs/reference/agent-tools.md:25, 39, 41, 126` still say "15 tools").
- `contract_cli.py:1386` (`verify-criterion` REVIEWER gating) — verified.
- `checkpoint_cli.py:1942–2063` (6 subcommands list/show/browse/context/cost/search) — verified; `add_parser` lines at 1942, 1976, 1984, 1994, 2022, 2035.
- `sandbox/overseer_monitor.py:74–78` (status query) — verified (`query_pipeline_status`).
- **High-value finding** (lines 99–105): the anchor-CLI-vs-rule-doc gap is real and correctly surfaced. `sandbox/egg_lib/orch_cli.py` contains zero "anchor" occurrences, while `sandbox/agent-config/rules/orchestrator.md:20–24` advertises `egg-orch anchor init/update/show/validate/cleanup`. This is a documentation-vs-code bug the plan phase needs to resolve before any `mcp__anchor__*` wrapper lands.

**3. Options Considered (lines 189–315)** — Meaningfully distinct (A=7 verbs minimum, B=15 verbs full, C=staged split, D=B+carryover), tradeoffs articulated in pros/cons, drift-gate and timeout implications called out per option. Good recognition that Options A and D don't satisfy AC1 on their own. The table at lines 233–250 with per-verb P0/P1/P2 priorities and backing-mechanism column is exactly what the plan phase needs to decompose into tasks.

**4. Constraints (lines 141–187)** — Comprehensive. Inherits iter-1 learnings (no `sys.exit` handler rule from TD8; `asyncio.to_thread` wrapper; typed `GatewayError`/`HandlerError`; drift gate). Adds iter-2-specific constraints (60-s MCP timeout, verified at `docs/reference/agent-tools.md:297`; rule-doc drift gate gap; new-capability verbs needing orchestrator endpoints before MCP wrappers). The SDK pin (`>=0.1.65,<0.2`) and private-mode dep-freeze constraints are accurate.

**5. Recommended Approach (lines 317–342)** — Option B with Option C fallback is well-justified against each AC and leaves `phase_get_context` field promotion explicitly out of scope (good — that's a tool-shape change, not a verb addition).

**6. Open Questions (lines 344–367)** — All registered. Verified via `egg-contract --issue 1917 show --json`: 14 `decision-*` entries + 1 `feedback-1` with 4 sub-questions (Q1–Q4). All decisions properly scoped to the `refine` phase.

### Non-blocking observations

- **.egg-state/drafts/1917-analysis.md:347, 349** — Summary table says "13 decisions + 1 feedback request with 4 sub-questions = 17 open items" but the contract actually has **14** decisions (decision-14: "mcp__brc__send_message / mcp__brc__poll_messages tool semantics: post-#1897 the legacy QUESTION message type was removed…"), so the real count is 14 + 4 = 18. decision-14 is missing from the table on lines 349–363. Fix: add the decision-14 row.

- **Missing "Recommended" markers on decisions 2–14** — Only decision-1 marks a "Recommended" option in its labels; the other 13 decisions have neutral option text even though the analysis prose implies preferences (e.g., Options Considered strongly leans toward `opt-1` for decision-8 peer-read source, toward pagination for decision-12 timeout, toward opt-1 for decision-12 tool-timeout). The iter-1 #1765 analysis marked "Recommended" on each decision's preferred option, which made the HITL UX cleaner. Non-blocking for the refine ACK but would be a cheap win — append `(Recommended — <one-liner>)` on the preferred option for each of decisions 2–14.

- **.egg-state/drafts/1917-analysis.md:39** ("docs at `docs/reference/agent-tools.md` still say '15' — iteration 1 shipped 15, and #1897 added 3 more…") — The doc drift is real and iter-2 will further widen it. Plan-phase task list should include a doc-refresh task against `docs/reference/agent-tools.md` (inventory table, `Tool inventory (15 verbs)` heading on line 39, "Total: **15 tools**" on line 126, and the `15 additional verbs` prose on line 293 which will become 0 once iter-2 merges). Consider calling this out explicitly in `feedback-1 Q1` as a must-include doc.

- **.egg-state/drafts/1917-analysis.md:320, 336** — "explicit early-split fallback to Option C if the anchor/task_mark_gap design stalls" — "stalls" is subjective. Suggest a concrete trigger for the plan phase: "split to Option C if decision-2 resolves to `opt-2` (add CLI first) OR decision-4 resolves to `opt-1` (new endpoint + new contract field)". Both require pre-MCP orchestrator work and naturally fence off into PR-2b.

- **.egg-state/drafts/1917-analysis.md:251** — `task_mark_gap` wins decision-4 as a genuinely new capability (no CLI, no endpoint, no contract field). Plan phase should consider whether to file this as a sub-issue so the design work (endpoint shape + contract field + CLI decision) does not silently block the iter-2 PR.

- **.egg-state/drafts/1917-analysis.md:170–176** (Rule-doc churn constraint) — The rule docs include `sandbox/agent-config/rules/orchestrator.md:20–24`, which advertises `egg-orch anchor *` subcommands that don't exist. If the plan picks `opt-2` on decision-2 (add CLI first), the rule doc is already aligned; if `opt-1` (REST-wrap with `cli_command=None`), the rule doc needs to be rewritten to point at `mcp__anchor__*` and explicitly retract the phantom CLI names. Worth explicit plan-phase callout.

- **Lines 225–250 — Option B verb table** — The priority column (P0/P1/P2) is useful but informal. Plan phase should promote this to a ranked list that maps 1:1 to tasks. `task_add_commit` and `task_update_notes` are both P0 but share a handler shape (linked-SHA / append-notes on the same `tasks` field); they can likely be one task in the task-planner's decomposition.

No blocking issues — the analysis meets all refine-phase criteria: problem is clear, research is grounded in verified file:line citations, options are distinct with articulated tradeoffs, constraints are thorough, open questions are specific and contract-registered, and the recommendation aligns with findings.


````yaml
id: ed208add-46d2-40
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1917-analysis.md
    - .egg-state/contracts/issue-1917.json
    - sandbox/egg_agent_tools/tools/brc.py
    - sandbox/egg_agent_tools/tools/message.py
    - sandbox/egg_agent_tools/tools/phase.py
    - sandbox/egg_agent_tools/tools/progress.py
    - sandbox/egg_agent_tools/tools/sdlc.py
    - sandbox/egg_agent_tools/tools/task.py
    - sandbox/egg_lib/orch_cli.py
    - sandbox/agent-config/rules/orchestrator.md
    - docs/reference/agent-tools.md
    - shared/egg_contracts/checkpoint_cli.py
    reason: "\nReviewed `.egg-state/drafts/1917-analysis.md` (383 lines) section-by-section\
      \ against refine-phase criteria, spot-checked every concrete file:line citation,\
      \ and confirmed HITL registration via `egg-contract show`.\n\n### Section-by-section\
      \ evaluation\n\n**1. Problem Statement (lines 5\u201329)** \u2014 Excellent.\
      \ Correctly anchors iter-2 scope to #1765's capability audit, references the\
      \ landed iter-1 mechanism (commit f24110b71 / PR #1920 verified via `git log`),\
      \ and evidences live pain with #1955 (verified \u2014 issue title: \"Agents\
      \ shell out to `egg-contract show` via Bash\"). Desired outcome maps cleanly\
      \ to the issue's AC1 (a/b/c disposition for every audit verb).\n\n**2. Current\
      \ Behavior (lines 31\u2013139)** \u2014 Strong. All citations spot-checked:\n\
      - `sandbox/egg_agent_tools/tools/` \u2014 confirmed 6 modules, `REGISTRATIONS`\
      \ counts per file: brc=6, message=3, phase=2, progress=3, sdlc=3, task=1, total\
      \ 18. The \"18-vs-docs-says-15\" doc drift is real (`docs/reference/agent-tools.md:25,\
      \ 39, 41, 126` still say \"15 tools\").\n- `contract_cli.py:1386` (`verify-criterion`\
      \ REVIEWER gating) \u2014 verified.\n- `checkpoint_cli.py:1942\u20132063` (6\
      \ subcommands list/show/browse/context/cost/search) \u2014 verified; `add_parser`\
      \ lines at 1942, 1976, 1984, 1994, 2022, 2035.\n- `sandbox/overseer_monitor.py:74\u2013\
      78` (status query) \u2014 verified (`query_pipeline_status`).\n- **High-value\
      \ finding** (lines 99\u2013105): the anchor-CLI-vs-rule-doc gap is real and\
      \ correctly surfaced. `sandbox/egg_lib/orch_cli.py` contains zero \"anchor\"\
      \ occurrences, while `sandbox/agent-config/rules/orchestrator.md:20\u201324`\
      \ advertises `egg-orch anchor init/update/show/validate/cleanup`. This is a\
      \ documentation-vs-code bug the plan phase needs to resolve before any `mcp__anchor__*`\
      \ wrapper lands.\n\n**3. Options Considered (lines 189\u2013315)** \u2014 Meaningfully\
      \ distinct (A=7 verbs minimum, B=15 verbs full, C=staged split, D=B+carryover),\
      \ tradeoffs articulated in pros/cons, drift-gate and timeout implications called\
      \ out per option. Good recognition that Options A and D don't satisfy AC1 on\
      \ their own. The table at lines 233\u2013250 with per-verb P0/P1/P2 priorities\
      \ and backing-mechanism column is exactly what the plan phase needs to decompose\
      \ into tasks.\n\n**4. Constraints (lines 141\u2013187)** \u2014 Comprehensive.\
      \ Inherits iter-1 learnings (no `sys.exit` handler rule from TD8; `asyncio.to_thread`\
      \ wrapper; typed `GatewayError`/`HandlerError`; drift gate). Adds iter-2-specific\
      \ constraints (60-s MCP timeout, verified at `docs/reference/agent-tools.md:297`;\
      \ rule-doc drift gate gap; new-capability verbs needing orchestrator endpoints\
      \ before MCP wrappers). The SDK pin (`>=0.1.65,<0.2`) and private-mode dep-freeze\
      \ constraints are accurate.\n\n**5. Recommended Approach (lines 317\u2013342)**\
      \ \u2014 Option B with Option C fallback is well-justified against each AC and\
      \ leaves `phase_get_context` field promotion explicitly out of scope (good \u2014\
      \ that's a tool-shape change, not a verb addition).\n\n**6. Open Questions (lines\
      \ 344\u2013367)** \u2014 All registered. Verified via `egg-contract --issue\
      \ 1917 show --json`: 14 `decision-*` entries + 1 `feedback-1` with 4 sub-questions\
      \ (Q1\u2013Q4). All decisions properly scoped to the `refine` phase.\n\n###\
      \ Non-blocking observations\n\n- **.egg-state/drafts/1917-analysis.md:347, 349**\
      \ \u2014 Summary table says \"13 decisions + 1 feedback request with 4 sub-questions\
      \ = 17 open items\" but the contract actually has **14** decisions (decision-14:\
      \ \"mcp__brc__send_message / mcp__brc__poll_messages tool semantics: post-#1897\
      \ the legacy QUESTION message type was removed\u2026\"), so the real count is\
      \ 14 + 4 = 18. decision-14 is missing from the table on lines 349\u2013363.\
      \ Fix: add the decision-14 row.\n\n- **Missing \"Recommended\" markers on decisions\
      \ 2\u201314** \u2014 Only decision-1 marks a \"Recommended\" option in its labels;\
      \ the other 13 decisions have neutral option text even though the analysis prose\
      \ implies preferences (e.g., Options Considered strongly leans toward `opt-1`\
      \ for decision-8 peer-read source, toward pagination for decision-12 timeout,\
      \ toward opt-1 for decision-12 tool-timeout). The iter-1 #1765 analysis marked\
      \ \"Recommended\" on each decision's preferred option, which made the HITL UX\
      \ cleaner. Non-blocking for the refine ACK but would be a cheap win \u2014 append\
      \ `(Recommended \u2014 <one-liner>)` on the preferred option for each of decisions\
      \ 2\u201314.\n\n- **.egg-state/drafts/1917-analysis.md:39** (\"docs at `docs/reference/agent-tools.md`\
      \ still say '15' \u2014 iteration 1 shipped 15, and #1897 added 3 more\u2026\
      \") \u2014 The doc drift is real and iter-2 will further widen it. Plan-phase\
      \ task list should include a doc-refresh task against `docs/reference/agent-tools.md`\
      \ (inventory table, `Tool inventory (15 verbs)` heading on line 39, \"Total:\
      \ **15 tools**\" on line 126, and the `15 additional verbs` prose on line 293\
      \ which will become 0 once iter-2 merges). Consider calling this out explicitly\
      \ in `feedback-1 Q1` as a must-include doc.\n\n- **.egg-state/drafts/1917-analysis.md:320,\
      \ 336** \u2014 \"explicit early-split fallback to Option C if the anchor/task_mark_gap\
      \ design stalls\" \u2014 \"stalls\" is subjective. Suggest a concrete trigger\
      \ for the plan phase: \"split to Option C if decision-2 resolves to `opt-2`\
      \ (add CLI first) OR decision-4 resolves to `opt-1` (new endpoint + new contract\
      \ field)\". Both require pre-MCP orchestrator work and naturally fence off into\
      \ PR-2b.\n\n- **.egg-state/drafts/1917-analysis.md:251** \u2014 `task_mark_gap`\
      \ wins decision-4 as a genuinely new capability (no CLI, no endpoint, no contract\
      \ field). Plan phase should consider whether to file this as a sub-issue so\
      \ the design work (endpoint shape + contract field + CLI decision) does not\
      \ silently block the iter-2 PR.\n\n- **.egg-state/drafts/1917-analysis.md:170\u2013\
      176** (Rule-doc churn constraint) \u2014 The rule docs include `sandbox/agent-config/rules/orchestrator.md:20\u2013\
      24`, which advertises `egg-orch anchor *` subcommands that don't exist. If the\
      \ plan picks `opt-2` on decision-2 (add CLI first), the rule doc is already\
      \ aligned; if `opt-1` (REST-wrap with `cli_command=None`), the rule doc needs\
      \ to be rewritten to point at `mcp__anchor__*` and explicitly retract the phantom\
      \ CLI names. Worth explicit plan-phase callout.\n\n- **Lines 225\u2013250 \u2014\
      \ Option B verb table** \u2014 The priority column (P0/P1/P2) is useful but\
      \ informal. Plan phase should promote this to a ranked list that maps 1:1 to\
      \ tasks. `task_add_commit` and `task_update_notes` are both P0 but share a handler\
      \ shape (linked-SHA / append-notes on the same `tasks` field); they can likely\
      \ be one task in the task-planner's decomposition.\n\nNo blocking issues \u2014\
      \ the analysis meets all refine-phase criteria: problem is clear, research is\
      \ grounded in verified file:line citations, options are distinct with articulated\
      \ tradeoffs, constraints are thorough, open questions are specific and contract-registered,\
      \ and the recommendation aligns with findings.\n"
  version: 1
````

### [2026-04-24T00:26:24Z] orchestrator → refiner (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 1). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: f45c8a6b-10c1-47
phase: refine
metadata:
  fully_acked: true
  version: 1
````

### [2026-04-24T00:26:29Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: 1e2c1273-cf7d-4e
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T00:27:17Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: f323bd2d-598b-43
phase: refine
metadata:
  consensus_reached: true
````
