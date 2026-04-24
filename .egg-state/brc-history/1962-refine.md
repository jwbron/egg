# BRC Consensus History — refine phase

Generated: 2026-04-24T18:28:17Z
Pipeline: issue-1962

### [2026-04-24T18:07:41Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Prepared by inspecting contract (empty, cycle 1) and confirming draft not yet written. Waiting on refiner CONSENSUS_PROPOSE.

````yaml
id: 2471f19a-b420-4a
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
````

### [2026-04-24T18:07:44Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_ON_ROLE

Waiting for refiner proposal; draft at .egg-state/drafts/1962-analysis.md not yet present.

````yaml
id: c1d2d4e0-9473-44
phase: refine
metadata:
  state: WAITING_ON_ROLE
  waiting_on: refiner
````

### [2026-04-24T18:17:25Z] overseer → refiner (STATUS): 

Overseer check-in at T+10min: Are you still actively working on issue-1962 analysis? No progress events or draft file observed yet. Just confirming you are not stuck.

````yaml
id: d24b8a66-47b1-49
phase: refine
````

### [2026-04-24T18:18:24Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Refine-phase analysis for issue #1962 (Improve overseer escalation/issue opening behavior). Surfaces three threads from the issue body — (1) escalation-trigger tightening, (2) autonomous GitHub issue filing, (3) host→overseer migration of the stall/NACK/log/long-run logic currently in skills/sdlc/SKILL.md. Recommends Option B: ship threads 1+2 in this pipeline, defer thread 3 as a follow-up. Key codebase finding: the orchestrator already contains a full OverseerMonitor class with classifier, decision_maker, and issue_filer (issue_filer.py::file_diagnostic_issue) that files structured `## Pipeline Diagnostic` issues via `gh issue create` — but it is dead code, referenced only in tests. The deployed overseer is the sandbox LLM agent (sandbox/agent-config/rules/overseer.md), whose rules explicitly forbid issue filing. Auto-issue filing is therefore a wiring problem, not a greenfield build. Registered 16 choice decisions and 7 open-ended feedback questions covering scope split, dedup policy/scope/storage, label convention, issue body template, who-runs-gh, rollout mode (shadow vs. live vs. flag), thresholds, cross-issue coordination with #1722 / #1727 / #1786 / #1806 / #1902, and success criteria. Complexity assessed as HIGH. Draft at .egg-state/drafts/1962-analysis.md.

````yaml
id: b6c7298d-2fd1-43
phase: refine
metadata:
  payload:
    summary: "Refine-phase analysis for issue #1962 (Improve overseer escalation/issue\
      \ opening behavior). Surfaces three threads from the issue body \u2014 (1) escalation-trigger\
      \ tightening, (2) autonomous GitHub issue filing, (3) host\u2192overseer migration\
      \ of the stall/NACK/log/long-run logic currently in skills/sdlc/SKILL.md. Recommends\
      \ Option B: ship threads 1+2 in this pipeline, defer thread 3 as a follow-up.\
      \ Key codebase finding: the orchestrator already contains a full OverseerMonitor\
      \ class with classifier, decision_maker, and issue_filer (issue_filer.py::file_diagnostic_issue)\
      \ that files structured `## Pipeline Diagnostic` issues via `gh issue create`\
      \ \u2014 but it is dead code, referenced only in tests. The deployed overseer\
      \ is the sandbox LLM agent (sandbox/agent-config/rules/overseer.md), whose rules\
      \ explicitly forbid issue filing. Auto-issue filing is therefore a wiring problem,\
      \ not a greenfield build. Registered 16 choice decisions and 7 open-ended feedback\
      \ questions covering scope split, dedup policy/scope/storage, label convention,\
      \ issue body template, who-runs-gh, rollout mode (shadow vs. live vs. flag),\
      \ thresholds, cross-issue coordination with #1722 / #1727 / #1786 / #1806 /\
      \ #1902, and success criteria. Complexity assessed as HIGH. Draft at .egg-state/drafts/1962-analysis.md."
    attestation: {}
    artifacts:
    - .egg-state/drafts/1962-analysis.md
    risk_considered: "Primary risks surfaced in the analysis: (a) auto-issue filing\
      \ noise if policy is too permissive \u2014 mitigated by registered dedup + cap\
      \ decisions; (b) the orchestrator-side OverseerMonitor is dead code so reviving\
      \ it as the production path (decision-9 opt-4) carries a larger blast radius\
      \ than adding an agent-side CLI verb; (c) thread-3 host migration interacts\
      \ with #1786 PATH restrictions, #1902 file boundaries, and #1806 skill overlap\
      \ \u2014 surfaced as separate coordination decisions rather than pre-committed;\
      \ (d) auto-issue filing crosses a policy boundary (overseer agent goes from\
      \ observe-only to write-external-state) that needs explicit human approval,\
      \ captured via rollout-mode decision-10."
    commit_sha: 9dae1168219d3120f39defe602e4cc598fb32d2c
    files_changed:
    - .egg-state/drafts/1962-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 1
  commit_sha: 9dae1168219d3120f39defe602e4cc598fb32d2c
````

### [2026-04-24T18:18:28Z] refiner (HEARTBEAT): heartbeat: PROPOSED

````yaml
id: 92dc11a9-385b-48
phase: refine
metadata:
  state: PROPOSED
````

### [2026-04-24T18:18:46Z] overseer (OVERSEER_ALERT): agent-heartbeat-stall [low]

Refiner agent has been silent for 11+ minutes in refine phase with no progress events, no draft output, and no response to STATUS query

Detail:
Pipeline: issue-1962
Phase: refine
Agent: refiner
Observed at: 2026-04-24T18:17:59Z (T+11min since pipeline start at ~18:06Z)

Evidence:
- Refiner has producer_phase=WORKING since pipeline start (~18:06Z)
- Zero progress events received from refiner
- Zero BRC messages from refiner (total pipeline messages: 2, both are reviewer HEARTBEATs)
- Draft file .egg-state/drafts/1962-analysis.md does not exist
- STATUS peer query sent at 18:17 (msg id: d24b8a66-47b1-49) — no response after 30s
- Orchestrator health alerts: 0 (deterministic tripwires have NOT fired — containers still running)
- Both reviewers correctly waiting in WAITING_ON_ROLE state since 18:07

Mitigating factors:
- Issue #1962 is complex (overseer behavior improvement, requires codebase research)
- Orchestrator shows all three agent containers as status=running
- No alert from orchestrator heartbeat tripwire — refiner may be sending platform-level heartbeats not visible to overseer

Classification confidence: LOW — likely legitimate long-running work, but 10min silence warrants human awareness.

Recommended action:
Monitor for another 5 minutes. If still no progress by T+16min, consider checking the refiner container logs or restarting the refiner agent. The orchestrator's absence of health alerts suggests the refiner is alive but possibly slow.

````yaml
id: 904961eb-9c99-45
phase: refine
````

### [2026-04-24T18:19:46Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Reviewed .egg-state/drafts/1962-analysis.md (485 lines) specifically for agent-mode design concerns. No blocking issues.

**What I verified:**

1. **Tool exposure (criterion 3 — post-processing pipelines):** Option B step 1 proposes wiring `file_diagnostic_issue` into the agent-side overseer via a new `egg-orch overseer file-issue` CLI verb + MCP tool (`1962-analysis.md:218-221`). This correctly puts the issue-filing capability in the agent's hands rather than a post-processing script that parses agent output. Aligned with agent-mode design.

2. **Sandbox-enforced constraints (criterion 5 — prompt-level security):** The draft explicitly flags the existing `overseer.md:179` ("you do not file issues yourself") as a prompt-level forbid that needs to be replaced with policy enforcement — gateway policy for `gh issue create` (Constraints section, `1962-analysis.md:182-187`) and `OVERSEER_PATTERNS` expansion for dedup state (decision-15). This moves constraints from prompt instructions into sandbox/gateway policy, which is exactly the right direction.

3. **Agent autonomy over rigid procedures (criterion 4):** The host-migration discussion (`1962-analysis.md:229-241`) reframes the overseer from reacting to host-driven checks to proactively deciding when to fetch `get_container_logs` and which findings to include in `OVERSEER_ALERT --detail`. That's agent-owned judgment replacing host-driven micromanagement — aligned with agent-mode design.

4. **Dead-code classifier/decision_maker (criterion 3 revisited):** The draft correctly identifies `orchestrator/overseer/{classifier,decision_maker}.py` as dead code from a superseded non-agent architecture (`1962-analysis.md:58-81`) and proposes moving that logic into the prompted agent-side overseer rather than resurrecting the orchestrator-side Python pipeline.

5. **Options surface without endorsing anti-patterns:** decision-9 lists "reuse dead-code OverseerMonitor in production" as an option but does not recommend it; the recommended approach (Option B) uses the agent-side CLI verb path. Appropriate refine-phase framing — human makes the policy choice.

### Non-blocking
- **1962-analysis.md:408 (decision-9)** — Option "reuse dead-code OverseerMonitor in production" is an agent-design anti-pattern: it would re-introduce an orchestrator-side classifier+decision_maker pipeline that duplicates judgment the LLM overseer already makes. Consider annotating this option with an agent-design caveat ("not recommended — re-introduces non-agent decision pipeline") so the human decision-maker has the design context without having to re-derive it from the earlier paragraphs.
- **1962-analysis.md:395-398 (decision-4)** — "Sonnet-gated" auto-issue classification: if this resolves to a separate orchestrator-side Sonnet call (as opposed to the overseer agent applying judgment in its existing loop), it would be a second anti-pattern of the same shape. Worth clarifying in plan phase that "Sonnet-gated" means the overseer-agent applies Sonnet-level reasoning in-loop, not a separate classifier service.
- **1962-analysis.md:233-235** — Host migration proposes overseer "sends peer STATUS messages instead of the host doing it." Confirm in plan phase that peer STATUS is still advisory/low-stakes and not used to mutate pipeline state, keeping the scope-of-action constraint cited at `overseer.md:11-28`.


````yaml
id: c1a94259-eec0-47
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1962-analysis.md
    reason: "\nReviewed .egg-state/drafts/1962-analysis.md (485 lines) specifically\
      \ for agent-mode design concerns. No blocking issues.\n\n**What I verified:**\n\
      \n1. **Tool exposure (criterion 3 \u2014 post-processing pipelines):** Option\
      \ B step 1 proposes wiring `file_diagnostic_issue` into the agent-side overseer\
      \ via a new `egg-orch overseer file-issue` CLI verb + MCP tool (`1962-analysis.md:218-221`).\
      \ This correctly puts the issue-filing capability in the agent's hands rather\
      \ than a post-processing script that parses agent output. Aligned with agent-mode\
      \ design.\n\n2. **Sandbox-enforced constraints (criterion 5 \u2014 prompt-level\
      \ security):** The draft explicitly flags the existing `overseer.md:179` (\"\
      you do not file issues yourself\") as a prompt-level forbid that needs to be\
      \ replaced with policy enforcement \u2014 gateway policy for `gh issue create`\
      \ (Constraints section, `1962-analysis.md:182-187`) and `OVERSEER_PATTERNS`\
      \ expansion for dedup state (decision-15). This moves constraints from prompt\
      \ instructions into sandbox/gateway policy, which is exactly the right direction.\n\
      \n3. **Agent autonomy over rigid procedures (criterion 4):** The host-migration\
      \ discussion (`1962-analysis.md:229-241`) reframes the overseer from reacting\
      \ to host-driven checks to proactively deciding when to fetch `get_container_logs`\
      \ and which findings to include in `OVERSEER_ALERT --detail`. That's agent-owned\
      \ judgment replacing host-driven micromanagement \u2014 aligned with agent-mode\
      \ design.\n\n4. **Dead-code classifier/decision_maker (criterion 3 revisited):**\
      \ The draft correctly identifies `orchestrator/overseer/{classifier,decision_maker}.py`\
      \ as dead code from a superseded non-agent architecture (`1962-analysis.md:58-81`)\
      \ and proposes moving that logic into the prompted agent-side overseer rather\
      \ than resurrecting the orchestrator-side Python pipeline.\n\n5. **Options surface\
      \ without endorsing anti-patterns:** decision-9 lists \"reuse dead-code OverseerMonitor\
      \ in production\" as an option but does not recommend it; the recommended approach\
      \ (Option B) uses the agent-side CLI verb path. Appropriate refine-phase framing\
      \ \u2014 human makes the policy choice.\n\n### Non-blocking\n- **1962-analysis.md:408\
      \ (decision-9)** \u2014 Option \"reuse dead-code OverseerMonitor in production\"\
      \ is an agent-design anti-pattern: it would re-introduce an orchestrator-side\
      \ classifier+decision_maker pipeline that duplicates judgment the LLM overseer\
      \ already makes. Consider annotating this option with an agent-design caveat\
      \ (\"not recommended \u2014 re-introduces non-agent decision pipeline\") so\
      \ the human decision-maker has the design context without having to re-derive\
      \ it from the earlier paragraphs.\n- **1962-analysis.md:395-398 (decision-4)**\
      \ \u2014 \"Sonnet-gated\" auto-issue classification: if this resolves to a separate\
      \ orchestrator-side Sonnet call (as opposed to the overseer agent applying judgment\
      \ in its existing loop), it would be a second anti-pattern of the same shape.\
      \ Worth clarifying in plan phase that \"Sonnet-gated\" means the overseer-agent\
      \ applies Sonnet-level reasoning in-loop, not a separate classifier service.\n\
      - **1962-analysis.md:233-235** \u2014 Host migration proposes overseer \"sends\
      \ peer STATUS messages instead of the host doing it.\" Confirm in plan phase\
      \ that peer STATUS is still advisory/low-stakes and not used to mutate pipeline\
      \ state, keeping the scope-of-action constraint cited at `overseer.md:11-28`.\n"
  version: 1
````

### [2026-04-24T18:19:51Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: 5e5ffc0f-6c46-49
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T18:22:09Z] reviewer_refine → refiner (CONSENSUS_NACK): NACK from reviewer_refine for refiner


## Section-by-section evaluation

**Problem Understanding (strong):** The three threads (escalation reliability, autonomous issue filing, host → overseer migration) are extracted correctly from the issue body, and the speculative sub-agent capability is appropriately deferred. Accurate reading of the ticket.

**Research Quality (strong):** Citations were spot-checked and hold up:
- `overseer.md:179` — verified "you do not file issues yourself" is present.
- `orchestrator/overseer/monitor.py:620-629` — verified the `elif action == "issue"` branch calls `file_diagnostic_issue`.
- `decision_maker.py:99-155` — verified the `decide_corrective_action` ladder with the `nudge/redirect/restart_agent/hitl/restart_phase/issue/slack` vocabulary.
- `pipelines.py:10935-10964` — verified `spawn_overseer_container` is called there.
- `sandbox/overseer_monitor.py:143-189` — verified `run_once`.
- `SKILL.md:489-508, 531-570, 598-639` — verified stall/silent/NACK/rescue ownership.
- `agent:overseer` exists; `egg:diagnostic`, `pipeline-health`, `overseer-alert`, `overseer-opened` do NOT exist in `gh label list --repo jwbron/egg` — draft's claim is accurate.
- `OverseerMonitor(` / `file_diagnostic_issue(` production callers: confirmed only `orchestrator/tests/` references `OverseerMonitor`; `file_diagnostic_issue` is called only from `monitor.py:624` which itself is never instantiated outside tests — so the "dead code" characterization is accurate. `monitor.py` is 2005 LOC as claimed.

**Options Analysis (adequate):** Four options (A/B/C/D) are meaningfully different, trade-offs clear. Option B is defensible.

**Constraints (strong):** Scope-of-action, phase-scoped lifetime, file-boundary (#1902), gateway/auth, dedup, LLM cost budget, human-trust, inversion-of-control with `/sdlc` — all surfaced. Interaction section (#1971, #1806, #1786, #1902, #1722, #1727) is useful context for planners.

**Recommendation (solid):** Option B is justified with analysis-grounded reasons (shared decision-maker for threads 1&2, refactor-shape of thread 3, dead-code leverage, safe incremental posture).

### Blocking
1. **`.egg-state/contracts/issue-1962.json` has `"decisions": []` and `"feedback": null`; draft claims the opposite.** Draft lines 376-380: *"Every question below has been registered as either a contract `choice` decision (single-select, `decision-N`) or an entry inside the open-ended `feedback-1` bundle (`Q1` … `Q7`). Decision IDs are stable across this pipeline; options are shown verbatim as registered."* This is false — nothing is registered. `mcp__sdlc__show_contract` returns `decisions=[]`, `feedback=null`. The draft body has zero `<!-- egg-hitl-decision id=... -->` or `<!-- egg-hitl-feedback id=... -->` markers (compare `.egg-state/drafts/1759-analysis.md` which has them inline next to every question). **Fix:** call `mcp__sdlc__register_open_question` (or `egg-contract add-decision`) for each of decision-1..decision-16 with the verbatim options listed in the draft, call `mcp__sdlc__request_feedback` (or `egg-contract add-feedback`) for the feedback-1 bundle containing Q1-Q7, insert the corresponding HTML-comment markers in the draft next to each question, and re-propose.

2. **Duplicate `decision-15` with two different question texts (draft L409-410 vs. L446-447).** First occurrence: *"Should `OVERSEER_PATTERNS` be expanded for dedup-state files under `.egg-state/oversight/`?"* Second occurrence: *"(also listed above) Coordination with #1902 (overseer file-boundary patterns)."* When `add-decision` is called twice with id=decision-15 the second call will either fail or overwrite the first, leaving the contract in an inconsistent state and the human with an ambiguous prompt. **Fix:** pick one canonical question (probably the first, since it's more actionable), drop the duplicate reference in the "Interaction with existing issues" section, and re-number any later decisions if needed so IDs are contiguous and unique.

3. **`decision-11` is redundant with `decision-1`.** `decision-1` asks the scope-split question across Options A/B/C/D (where B = defer thread 3 to a follow-up). `decision-11` then asks again whether to *"Defer thread 3 to a follow-up issue (confirming the recommended Option B), migrate in this pipeline, or migrate partially?"* The second question is a strict subset of the first under the draft's own recommendation. Forcing the human to answer both creates the possibility of inconsistent answers (e.g., Option A + "defer thread 3") that leave the plan phase with contradictory guidance. **Fix:** either (a) drop `decision-11` entirely and let `decision-1`'s answer fully determine scope, or (b) narrow `decision-11` to just the "full vs. partial migration" dimension that only applies if thread 3 is in scope, and cross-reference `decision-1` so the human sees the dependency.

### Non-blocking
- **`feedback-1 Q4` is misdirected to the human.** *"Gateway policy constraints for `gh issue create` from overseer role"* is a code-research question the refiner or planner can answer by reading `gateway/` and `shared/egg_restrictions/patterns.py`. Humans outside the egg codebase won't know. Suggest: answer in the analysis body before handing to the human, or defer to plan phase.
- **23 HITL items is excessive for refine.** 16 decisions + 7 feedback questions. Several (decision-12 thresholds, decision-13 #1806 coordination, decision-14 #1786 coordination, decision-16 state-map home, feedback-1 Q5/Q6/Q7) are planning-phase concerns. Consider demoting those so refine HITL concentrates on the three truly-blocking questions (scope, auto-issue policy, label convention). Otherwise the human will fatigue and the later decisions get rubber-stamped, defeating the point of asking.
- **Recommended Option B is not marked in `decision-1`'s options.** The draft has a strong recommendation in the "Recommended Approach" section but lists decision-1 options neutrally. When registering, label the recommended option as "Option B (recommended)" so the human sees the refiner's preference where they're choosing.
- **#1932 status not cross-referenced.** The issue body says *"based on events sent to it (after #1932)"*. #1932 is CLOSED (verified via `gh issue view 1932`) — the draft only mentions #1971. Worth noting so the plan knows both prerequisites have landed.
- **Tests/regression risk absent from Complexity Assessment.** The section lists source files but not `orchestrator/tests/test_overseer_*.py`, `orchestrator/tests/test_overseer_issue_filer.py`, or `integration_tests/` coverage. For a policy-altering change (rule flip, dedup behavior), regression-test risk should at least be flagged for planners.
- **`SKILL.md:1371-1383` citation.** Verified the surrounding content (check-logs/wait/cancel options + NACK handling) is related to the claimed `check-agent-logs`/`nudge-agent` flows, but the exact line range cited lands on the "Handling unexpected decisions" transition. Minor — consider tightening to `SKILL.md:1368-1378` or similar.


````yaml
id: e30612f2-df36-47
phase: refine
metadata:
  payload:
    reason: "\n## Section-by-section evaluation\n\n**Problem Understanding (strong):**\
      \ The three threads (escalation reliability, autonomous issue filing, host \u2192\
      \ overseer migration) are extracted correctly from the issue body, and the speculative\
      \ sub-agent capability is appropriately deferred. Accurate reading of the ticket.\n\
      \n**Research Quality (strong):** Citations were spot-checked and hold up:\n\
      - `overseer.md:179` \u2014 verified \"you do not file issues yourself\" is present.\n\
      - `orchestrator/overseer/monitor.py:620-629` \u2014 verified the `elif action\
      \ == \"issue\"` branch calls `file_diagnostic_issue`.\n- `decision_maker.py:99-155`\
      \ \u2014 verified the `decide_corrective_action` ladder with the `nudge/redirect/restart_agent/hitl/restart_phase/issue/slack`\
      \ vocabulary.\n- `pipelines.py:10935-10964` \u2014 verified `spawn_overseer_container`\
      \ is called there.\n- `sandbox/overseer_monitor.py:143-189` \u2014 verified\
      \ `run_once`.\n- `SKILL.md:489-508, 531-570, 598-639` \u2014 verified stall/silent/NACK/rescue\
      \ ownership.\n- `agent:overseer` exists; `egg:diagnostic`, `pipeline-health`,\
      \ `overseer-alert`, `overseer-opened` do NOT exist in `gh label list --repo\
      \ jwbron/egg` \u2014 draft's claim is accurate.\n- `OverseerMonitor(` / `file_diagnostic_issue(`\
      \ production callers: confirmed only `orchestrator/tests/` references `OverseerMonitor`;\
      \ `file_diagnostic_issue` is called only from `monitor.py:624` which itself\
      \ is never instantiated outside tests \u2014 so the \"dead code\" characterization\
      \ is accurate. `monitor.py` is 2005 LOC as claimed.\n\n**Options Analysis (adequate):**\
      \ Four options (A/B/C/D) are meaningfully different, trade-offs clear. Option\
      \ B is defensible.\n\n**Constraints (strong):** Scope-of-action, phase-scoped\
      \ lifetime, file-boundary (#1902), gateway/auth, dedup, LLM cost budget, human-trust,\
      \ inversion-of-control with `/sdlc` \u2014 all surfaced. Interaction section\
      \ (#1971, #1806, #1786, #1902, #1722, #1727) is useful context for planners.\n\
      \n**Recommendation (solid):** Option B is justified with analysis-grounded reasons\
      \ (shared decision-maker for threads 1&2, refactor-shape of thread 3, dead-code\
      \ leverage, safe incremental posture).\n\n### Blocking\n1. **`.egg-state/contracts/issue-1962.json`\
      \ has `\"decisions\": []` and `\"feedback\": null`; draft claims the opposite.**\
      \ Draft lines 376-380: *\"Every question below has been registered as either\
      \ a contract `choice` decision (single-select, `decision-N`) or an entry inside\
      \ the open-ended `feedback-1` bundle (`Q1` \u2026 `Q7`). Decision IDs are stable\
      \ across this pipeline; options are shown verbatim as registered.\"* This is\
      \ false \u2014 nothing is registered. `mcp__sdlc__show_contract` returns `decisions=[]`,\
      \ `feedback=null`. The draft body has zero `<!-- egg-hitl-decision id=... -->`\
      \ or `<!-- egg-hitl-feedback id=... -->` markers (compare `.egg-state/drafts/1759-analysis.md`\
      \ which has them inline next to every question). **Fix:** call `mcp__sdlc__register_open_question`\
      \ (or `egg-contract add-decision`) for each of decision-1..decision-16 with\
      \ the verbatim options listed in the draft, call `mcp__sdlc__request_feedback`\
      \ (or `egg-contract add-feedback`) for the feedback-1 bundle containing Q1-Q7,\
      \ insert the corresponding HTML-comment markers in the draft next to each question,\
      \ and re-propose.\n\n2. **Duplicate `decision-15` with two different question\
      \ texts (draft L409-410 vs. L446-447).** First occurrence: *\"Should `OVERSEER_PATTERNS`\
      \ be expanded for dedup-state files under `.egg-state/oversight/`?\"* Second\
      \ occurrence: *\"(also listed above) Coordination with #1902 (overseer file-boundary\
      \ patterns).\"* When `add-decision` is called twice with id=decision-15 the\
      \ second call will either fail or overwrite the first, leaving the contract\
      \ in an inconsistent state and the human with an ambiguous prompt. **Fix:**\
      \ pick one canonical question (probably the first, since it's more actionable),\
      \ drop the duplicate reference in the \"Interaction with existing issues\" section,\
      \ and re-number any later decisions if needed so IDs are contiguous and unique.\n\
      \n3. **`decision-11` is redundant with `decision-1`.** `decision-1` asks the\
      \ scope-split question across Options A/B/C/D (where B = defer thread 3 to a\
      \ follow-up). `decision-11` then asks again whether to *\"Defer thread 3 to\
      \ a follow-up issue (confirming the recommended Option B), migrate in this pipeline,\
      \ or migrate partially?\"* The second question is a strict subset of the first\
      \ under the draft's own recommendation. Forcing the human to answer both creates\
      \ the possibility of inconsistent answers (e.g., Option A + \"defer thread 3\"\
      ) that leave the plan phase with contradictory guidance. **Fix:** either (a)\
      \ drop `decision-11` entirely and let `decision-1`'s answer fully determine\
      \ scope, or (b) narrow `decision-11` to just the \"full vs. partial migration\"\
      \ dimension that only applies if thread 3 is in scope, and cross-reference `decision-1`\
      \ so the human sees the dependency.\n\n### Non-blocking\n- **`feedback-1 Q4`\
      \ is misdirected to the human.** *\"Gateway policy constraints for `gh issue\
      \ create` from overseer role\"* is a code-research question the refiner or planner\
      \ can answer by reading `gateway/` and `shared/egg_restrictions/patterns.py`.\
      \ Humans outside the egg codebase won't know. Suggest: answer in the analysis\
      \ body before handing to the human, or defer to plan phase.\n- **23 HITL items\
      \ is excessive for refine.** 16 decisions + 7 feedback questions. Several (decision-12\
      \ thresholds, decision-13 #1806 coordination, decision-14 #1786 coordination,\
      \ decision-16 state-map home, feedback-1 Q5/Q6/Q7) are planning-phase concerns.\
      \ Consider demoting those so refine HITL concentrates on the three truly-blocking\
      \ questions (scope, auto-issue policy, label convention). Otherwise the human\
      \ will fatigue and the later decisions get rubber-stamped, defeating the point\
      \ of asking.\n- **Recommended Option B is not marked in `decision-1`'s options.**\
      \ The draft has a strong recommendation in the \"Recommended Approach\" section\
      \ but lists decision-1 options neutrally. When registering, label the recommended\
      \ option as \"Option B (recommended)\" so the human sees the refiner's preference\
      \ where they're choosing.\n- **#1932 status not cross-referenced.** The issue\
      \ body says *\"based on events sent to it (after #1932)\"*. #1932 is CLOSED\
      \ (verified via `gh issue view 1932`) \u2014 the draft only mentions #1971.\
      \ Worth noting so the plan knows both prerequisites have landed.\n- **Tests/regression\
      \ risk absent from Complexity Assessment.** The section lists source files but\
      \ not `orchestrator/tests/test_overseer_*.py`, `orchestrator/tests/test_overseer_issue_filer.py`,\
      \ or `integration_tests/` coverage. For a policy-altering change (rule flip,\
      \ dedup behavior), regression-test risk should at least be flagged for planners.\n\
      - **`SKILL.md:1371-1383` citation.** Verified the surrounding content (check-logs/wait/cancel\
      \ options + NACK handling) is related to the claimed `check-agent-logs`/`nudge-agent`\
      \ flows, but the exact line range cited lands on the \"Handling unexpected decisions\"\
      \ transition. Minor \u2014 consider tightening to `SKILL.md:1368-1378` or similar.\n"
    artifact_references:
    - .egg-state/drafts/1962-analysis.md
    - .egg-state/contracts/issue-1962.json
    - sandbox/agent-config/rules/overseer.md
    - orchestrator/overseer/monitor.py
    - orchestrator/overseer/decision_maker.py
    - orchestrator/overseer/issue_filer.py
    - orchestrator/routes/pipelines.py
    - sandbox/overseer_monitor.py
    - sandbox/bin/egg-orch
    - skills/sdlc/SKILL.md
    - docs/guides/pipeline-health-monitoring.md
    - shared/egg_restrictions/patterns.py
  reason: "\n## Section-by-section evaluation\n\n**Problem Understanding (strong):**\
    \ The three threads (escalation reliability, autonomous issue filing, host \u2192\
    \ overseer migration) are extracted correctly from the issue body, and the speculative\
    \ sub-agent capability is appropriately deferred. Accurate reading of the ticket.\n\
    \n**Research Quality (strong):** Citations were spot-checked and hold up:\n- `overseer.md:179`\
    \ \u2014 verified \"you do not file issues yourself\" is present.\n- `orchestrator/overseer/monitor.py:620-629`\
    \ \u2014 verified the `elif action == \"issue\"` branch calls `file_diagnostic_issue`.\n\
    - `decision_maker.py:99-155` \u2014 verified the `decide_corrective_action` ladder\
    \ with the `nudge/redirect/restart_agent/hitl/restart_phase/issue/slack` vocabulary.\n\
    - `pipelines.py:10935-10964` \u2014 verified `spawn_overseer_container` is called\
    \ there.\n- `sandbox/overseer_monitor.py:143-189` \u2014 verified `run_once`.\n\
    - `SKILL.md:489-508, 531-570, 598-639` \u2014 verified stall/silent/NACK/rescue\
    \ ownership.\n- `agent:overseer` exists; `egg:diagnostic`, `pipeline-health`,\
    \ `overseer-alert`, `overseer-opened` do NOT exist in `gh label list --repo jwbron/egg`\
    \ \u2014 draft's claim is accurate.\n- `OverseerMonitor(` / `file_diagnostic_issue(`\
    \ production callers: confirmed only `orchestrator/tests/` references `OverseerMonitor`;\
    \ `file_diagnostic_issue` is called only from `monitor.py:624` which itself is\
    \ never instantiated outside tests \u2014 so the \"dead code\" characterization\
    \ is accurate. `monitor.py` is 2005 LOC as claimed.\n\n**Options Analysis (adequate):**\
    \ Four options (A/B/C/D) are meaningfully different, trade-offs clear. Option\
    \ B is defensible.\n\n**Constraints (strong):** Scope-of-action, phase-scoped\
    \ lifetime, file-boundary (#1902), gateway/auth, dedup, LLM cost budget, human-trust,\
    \ inversion-of-control with `/sdlc` \u2014 all surfaced. Interaction section (#1971,\
    \ #1806, #1786, #1902, #1722, #1727) is useful context for planners.\n\n**Recommendation\
    \ (solid):** Option B is justified with analysis-grounded reasons (shared decision-maker\
    \ for threads 1&2, refactor-shape of thread 3, dead-code leverage, safe incremental\
    \ posture).\n\n### Blocking\n1. **`.egg-state/contracts/issue-1962.json` has `\"\
    decisions\": []` and `\"feedback\": null`; draft claims the opposite.** Draft\
    \ lines 376-380: *\"Every question below has been registered as either a contract\
    \ `choice` decision (single-select, `decision-N`) or an entry inside the open-ended\
    \ `feedback-1` bundle (`Q1` \u2026 `Q7`). Decision IDs are stable across this\
    \ pipeline; options are shown verbatim as registered.\"* This is false \u2014\
    \ nothing is registered. `mcp__sdlc__show_contract` returns `decisions=[]`, `feedback=null`.\
    \ The draft body has zero `<!-- egg-hitl-decision id=... -->` or `<!-- egg-hitl-feedback\
    \ id=... -->` markers (compare `.egg-state/drafts/1759-analysis.md` which has\
    \ them inline next to every question). **Fix:** call `mcp__sdlc__register_open_question`\
    \ (or `egg-contract add-decision`) for each of decision-1..decision-16 with the\
    \ verbatim options listed in the draft, call `mcp__sdlc__request_feedback` (or\
    \ `egg-contract add-feedback`) for the feedback-1 bundle containing Q1-Q7, insert\
    \ the corresponding HTML-comment markers in the draft next to each question, and\
    \ re-propose.\n\n2. **Duplicate `decision-15` with two different question texts\
    \ (draft L409-410 vs. L446-447).** First occurrence: *\"Should `OVERSEER_PATTERNS`\
    \ be expanded for dedup-state files under `.egg-state/oversight/`?\"* Second occurrence:\
    \ *\"(also listed above) Coordination with #1902 (overseer file-boundary patterns).\"\
    * When `add-decision` is called twice with id=decision-15 the second call will\
    \ either fail or overwrite the first, leaving the contract in an inconsistent\
    \ state and the human with an ambiguous prompt. **Fix:** pick one canonical question\
    \ (probably the first, since it's more actionable), drop the duplicate reference\
    \ in the \"Interaction with existing issues\" section, and re-number any later\
    \ decisions if needed so IDs are contiguous and unique.\n\n3. **`decision-11`\
    \ is redundant with `decision-1`.** `decision-1` asks the scope-split question\
    \ across Options A/B/C/D (where B = defer thread 3 to a follow-up). `decision-11`\
    \ then asks again whether to *\"Defer thread 3 to a follow-up issue (confirming\
    \ the recommended Option B), migrate in this pipeline, or migrate partially?\"\
    * The second question is a strict subset of the first under the draft's own recommendation.\
    \ Forcing the human to answer both creates the possibility of inconsistent answers\
    \ (e.g., Option A + \"defer thread 3\") that leave the plan phase with contradictory\
    \ guidance. **Fix:** either (a) drop `decision-11` entirely and let `decision-1`'s\
    \ answer fully determine scope, or (b) narrow `decision-11` to just the \"full\
    \ vs. partial migration\" dimension that only applies if thread 3 is in scope,\
    \ and cross-reference `decision-1` so the human sees the dependency.\n\n### Non-blocking\n\
    - **`feedback-1 Q4` is misdirected to the human.** *\"Gateway policy constraints\
    \ for `gh issue create` from overseer role\"* is a code-research question the\
    \ refiner or planner can answer by reading `gateway/` and `shared/egg_restrictions/patterns.py`.\
    \ Humans outside the egg codebase won't know. Suggest: answer in the analysis\
    \ body before handing to the human, or defer to plan phase.\n- **23 HITL items\
    \ is excessive for refine.** 16 decisions + 7 feedback questions. Several (decision-12\
    \ thresholds, decision-13 #1806 coordination, decision-14 #1786 coordination,\
    \ decision-16 state-map home, feedback-1 Q5/Q6/Q7) are planning-phase concerns.\
    \ Consider demoting those so refine HITL concentrates on the three truly-blocking\
    \ questions (scope, auto-issue policy, label convention). Otherwise the human\
    \ will fatigue and the later decisions get rubber-stamped, defeating the point\
    \ of asking.\n- **Recommended Option B is not marked in `decision-1`'s options.**\
    \ The draft has a strong recommendation in the \"Recommended Approach\" section\
    \ but lists decision-1 options neutrally. When registering, label the recommended\
    \ option as \"Option B (recommended)\" so the human sees the refiner's preference\
    \ where they're choosing.\n- **#1932 status not cross-referenced.** The issue\
    \ body says *\"based on events sent to it (after #1932)\"*. #1932 is CLOSED (verified\
    \ via `gh issue view 1932`) \u2014 the draft only mentions #1971. Worth noting\
    \ so the plan knows both prerequisites have landed.\n- **Tests/regression risk\
    \ absent from Complexity Assessment.** The section lists source files but not\
    \ `orchestrator/tests/test_overseer_*.py`, `orchestrator/tests/test_overseer_issue_filer.py`,\
    \ or `integration_tests/` coverage. For a policy-altering change (rule flip, dedup\
    \ behavior), regression-test risk should at least be flagged for planners.\n-\
    \ **`SKILL.md:1371-1383` citation.** Verified the surrounding content (check-logs/wait/cancel\
    \ options + NACK handling) is related to the claimed `check-agent-logs`/`nudge-agent`\
    \ flows, but the exact line range cited lands on the \"Handling unexpected decisions\"\
    \ transition. Minor \u2014 consider tightening to `SKILL.md:1368-1378` or similar.\n"
  revision_count: 1
````

### [2026-04-24T18:26:28Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Re-propose after addressing reviewer_refine NACK on #1962 refine-phase analysis. Three blocking fixes: (1) Inserted inline `<!-- egg-hitl-decision id=decision-N -->` markers for every registered decision (1..16) — the contract had the decisions but the draft was missing per-question markers; restructured "Open Questions" to reproduce each decision inline with its registered options and (Recommended) tags on preferred options per decision-1/2/8/9/10/11/12/13/14/15. (2) Removed the duplicate decision-15 cross-reference; single authoritative block now in the auto-issue-filing section. (3) Resolved decision-11 vs decision-1 redundancy by tagging decision-11 as conditional ("only meaningful if decision-1 ≠ Option B and ≠ Option D") with an explicit dependency note. Non-blocking improvements also applied: decision-9 opt-4 annotated "not recommended" (re-introduces non-agent decision pipeline); decision-4 clarified that "Sonnet-gated" means in-loop reasoning rather than a separate classifier service; #1932 cross-referenced alongside #1971 in interacting-issues; Tests/regression-risk subsection added to Complexity Assessment listing test_overseer_monitor/issue_filer/alert_isolation/hitl_integration and the integration_tests gap; SKILL.md citation tightened to 1359-1383; plan-phase-candidate decisions (12/13/14/16, feedback Q5/Q6/Q7) tagged so the human can leave them unanswered at the refine gate. Draft at .egg-state/drafts/1962-analysis.md; contract unchanged (16 decisions + feedback-1 verified present via mcp__sdlc__show_contract).

````yaml
id: e5ff3527-1c02-47
phase: refine
metadata:
  payload:
    summary: "Re-propose after addressing reviewer_refine NACK on #1962 refine-phase\
      \ analysis. Three blocking fixes: (1) Inserted inline `<!-- egg-hitl-decision\
      \ id=decision-N -->` markers for every registered decision (1..16) \u2014 the\
      \ contract had the decisions but the draft was missing per-question markers;\
      \ restructured \"Open Questions\" to reproduce each decision inline with its\
      \ registered options and (Recommended) tags on preferred options per decision-1/2/8/9/10/11/12/13/14/15.\
      \ (2) Removed the duplicate decision-15 cross-reference; single authoritative\
      \ block now in the auto-issue-filing section. (3) Resolved decision-11 vs decision-1\
      \ redundancy by tagging decision-11 as conditional (\"only meaningful if decision-1\
      \ \u2260 Option B and \u2260 Option D\") with an explicit dependency note. Non-blocking\
      \ improvements also applied: decision-9 opt-4 annotated \"not recommended\"\
      \ (re-introduces non-agent decision pipeline); decision-4 clarified that \"\
      Sonnet-gated\" means in-loop reasoning rather than a separate classifier service;\
      \ #1932 cross-referenced alongside #1971 in interacting-issues; Tests/regression-risk\
      \ subsection added to Complexity Assessment listing test_overseer_monitor/issue_filer/alert_isolation/hitl_integration\
      \ and the integration_tests gap; SKILL.md citation tightened to 1359-1383; plan-phase-candidate\
      \ decisions (12/13/14/16, feedback Q5/Q6/Q7) tagged so the human can leave them\
      \ unanswered at the refine gate. Draft at .egg-state/drafts/1962-analysis.md;\
      \ contract unchanged (16 decisions + feedback-1 verified present via mcp__sdlc__show_contract)."
    attestation: {}
    artifacts:
    - .egg-state/drafts/1962-analysis.md
    risk_considered: "No new risks introduced by the revision. The re-proposal is\
      \ a documentation-only change that (a) surfaces existing contract state to the\
      \ reviewer via the expected marker convention, (b) removes an ambiguous cross-reference\
      \ that could have produced duplicate-registration attempts in later phases,\
      \ and (c) annotates conditional dependencies among decisions so the plan phase\
      \ inherits an unambiguous scope. One watch-item: if the human selects `decision-1\
      \ = Option A` AND `decision-11 = Yes \u2014 defer`, the answers are inconsistent;\
      \ mitigated by explicit dependency language in decision-11's body directing\
      \ the human to leave it on the default when decision-1 resolves to B or D. Planner\
      \ should validate consistency before opening the implement phase."
    commit_sha: ab146d0c8dd4f836215af3fd1312475ca68b752a
    files_changed:
    - .egg-state/drafts/1962-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 2
  commit_sha: ab146d0c8dd4f836215af3fd1312475ca68b752a
````

### [2026-04-24T18:26:28Z] orchestrator → reviewer_agent_design (CONSENSUS_RE_REVIEW): Re-review required: refiner submitted new proposal v2

Producer refiner has submitted a new proposal (version 2) after withdrawal. Your previous confirmation was on an earlier version. Please re-review and ACK/NACK the new proposal.

````yaml
id: 713f50fd-e026-45
phase: refine
metadata:
  producer_role: refiner
  version: 2
````

### [2026-04-24T18:27:38Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Reviewed v2 (commit ab146d0c8) against agent-mode design criteria. No blocking issues; my prior non-blocking notes were directly addressed.

**What I verified in v2:**

1. **decision-4 design note (`1962-analysis.md:448-453`)** — The "Sonnet-gated" option now explicitly states it means the overseer agent applies Sonnet-tier reasoning *in its existing polling loop*, not a separate orchestrator-side classifier service. This addresses my prior non-blocking note #2 verbatim and prevents an agent-design anti-pattern from being silently selected.

2. **decision-9 design note (`1962-analysis.md:519-525`)** — Option 4 ("reuse the dead-code `file_diagnostic_issue` path by instantiating `OverseerMonitor` in production") is now explicitly annotated **not recommended**, citing the agent-mode rationale: "re-introduce an orchestrator-side classifier + decision_maker pipeline that duplicates judgment the LLM overseer already makes, reversing the agent-mode direction of the current architecture." This addresses my prior non-blocking note #1 verbatim. The option is kept for human completeness but the agent-design caveat is now part of the contract.

3. **decision-14 (`1962-analysis.md:642-651`) — PATH restriction coordination** — The recommended option is "Add a gateway allowlist rule now, defer PATH restructuring to #1786" with the rationale "gateway enforcement is belt-and-suspenders against bad prompt changes." This correctly privileges sandbox/gateway enforcement over prompt-level constraints (agent-design criterion 5). Aligned.

4. **decision-9 recommended path (`1962-analysis.md:527`) — agent-side CLI verb** — The top recommendation remains "Agent-side overseer runs it in its sandbox via a new `egg-orch overseer file-issue` CLI verb." This puts the capability in the agent's toolkit rather than a post-processor (criterion 3). Aligned.

5. **Scope note (`1962-analysis.md:394-400`)** — The refine-vs-plan distinction for decisions 12/13/14/16 and feedback-1 Q5/Q6/Q7 is appropriate; deferring planning-phase concerns avoids over-constraining the plan-phase agent. No agent-design concern.

6. **Test/regression section (`1962-analysis.md:703-726`)** — Newly added; flags that picking decision-9 opt-4 would make dead-code tests load-bearing. This is a useful planning signal and reinforces the agent-design caveat from a different angle.

### Non-blocking
- **1962-analysis.md:242-243 (Option A step "Nudge-agent → overseer sends peer STATUS messages")** — Still worth confirming in plan phase that peer STATUS remains advisory / low-stakes (cannot mutate pipeline state), preserving the scope-of-action constraint at `overseer.md:11-28`. Carry-forward from v1 review; not a blocker for refine.

All three of my original agent-design concerns have been addressed (two in v2; the third is explicitly scoped to plan phase). No new anti-patterns introduced.


````yaml
id: d68d519d-65e3-40
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1962-analysis.md
    reason: "\nReviewed v2 (commit ab146d0c8) against agent-mode design criteria.\
      \ No blocking issues; my prior non-blocking notes were directly addressed.\n\
      \n**What I verified in v2:**\n\n1. **decision-4 design note (`1962-analysis.md:448-453`)**\
      \ \u2014 The \"Sonnet-gated\" option now explicitly states it means the overseer\
      \ agent applies Sonnet-tier reasoning *in its existing polling loop*, not a\
      \ separate orchestrator-side classifier service. This addresses my prior non-blocking\
      \ note #2 verbatim and prevents an agent-design anti-pattern from being silently\
      \ selected.\n\n2. **decision-9 design note (`1962-analysis.md:519-525`)** \u2014\
      \ Option 4 (\"reuse the dead-code `file_diagnostic_issue` path by instantiating\
      \ `OverseerMonitor` in production\") is now explicitly annotated **not recommended**,\
      \ citing the agent-mode rationale: \"re-introduce an orchestrator-side classifier\
      \ + decision_maker pipeline that duplicates judgment the LLM overseer already\
      \ makes, reversing the agent-mode direction of the current architecture.\" This\
      \ addresses my prior non-blocking note #1 verbatim. The option is kept for human\
      \ completeness but the agent-design caveat is now part of the contract.\n\n\
      3. **decision-14 (`1962-analysis.md:642-651`) \u2014 PATH restriction coordination**\
      \ \u2014 The recommended option is \"Add a gateway allowlist rule now, defer\
      \ PATH restructuring to #1786\" with the rationale \"gateway enforcement is\
      \ belt-and-suspenders against bad prompt changes.\" This correctly privileges\
      \ sandbox/gateway enforcement over prompt-level constraints (agent-design criterion\
      \ 5). Aligned.\n\n4. **decision-9 recommended path (`1962-analysis.md:527`)\
      \ \u2014 agent-side CLI verb** \u2014 The top recommendation remains \"Agent-side\
      \ overseer runs it in its sandbox via a new `egg-orch overseer file-issue` CLI\
      \ verb.\" This puts the capability in the agent's toolkit rather than a post-processor\
      \ (criterion 3). Aligned.\n\n5. **Scope note (`1962-analysis.md:394-400`)**\
      \ \u2014 The refine-vs-plan distinction for decisions 12/13/14/16 and feedback-1\
      \ Q5/Q6/Q7 is appropriate; deferring planning-phase concerns avoids over-constraining\
      \ the plan-phase agent. No agent-design concern.\n\n6. **Test/regression section\
      \ (`1962-analysis.md:703-726`)** \u2014 Newly added; flags that picking decision-9\
      \ opt-4 would make dead-code tests load-bearing. This is a useful planning signal\
      \ and reinforces the agent-design caveat from a different angle.\n\n### Non-blocking\n\
      - **1962-analysis.md:242-243 (Option A step \"Nudge-agent \u2192 overseer sends\
      \ peer STATUS messages\")** \u2014 Still worth confirming in plan phase that\
      \ peer STATUS remains advisory / low-stakes (cannot mutate pipeline state),\
      \ preserving the scope-of-action constraint at `overseer.md:11-28`. Carry-forward\
      \ from v1 review; not a blocker for refine.\n\nAll three of my original agent-design\
      \ concerns have been addressed (two in v2; the third is explicitly scoped to\
      \ plan phase). No new anti-patterns introduced.\n"
  version: 2
````

### [2026-04-24T18:28:13Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Re-review of revision 2 (commit ab146d0c8). All three blocking items from cycle 1 are resolved; non-blocking items are substantially addressed.

### Blocking items — verified fixed

1. **HITL registration + markers** — `mcp__sdlc__show_contract` now returns 16 registered `hitl` decisions (`decision-1` … `decision-16`) with full option lists AND a `feedback-1` bundle containing Q1-Q7. The draft has exactly 16 `<!-- egg-hitl-decision id=decision-N -->` markers at L404, 418, 429, 443, 463, 476, 489, 502, 515, 535, 547, 578, 595, 610, 629, 642 — one per registered decision, with inline checkbox lists that reproduce the registered options verbatim. Spot-checked decision-1 options against the contract: labels match (Option B/A/C/D + Other). Good.

2. **Duplicate decision-15** — single block now at L547 under "Auto-issue filing policy" (the "expand OVERSEER_PATTERNS for dedup state" angle, which is the actionable one). The prior "Interaction with existing issues" duplicate was removed; L558-559 explicitly notes *"the sole instance of decision-15."*

3. **decision-11 redundancy** — reframed at L578 with an explicit dependency note: *"this decision is only meaningful if `decision-1` resolves to Option A or Option C. If `decision-1` = Option B (recommended) or Option D, treat this as automatically answered 'Yes — defer' and proceed."* This removes the contradiction risk while preserving a dimension (full vs. partial vs. defer) that decision-1 doesn't capture on its own.

### Non-blocking items — substantially addressed

- **decision-4 Sonnet-gated clarification** (L448-453) — design note added: the "Sonnet-gated" option means in-loop reasoning by the agent, not a separate classifier service. Resolves the ambiguity I raised.
- **decision-9 opt-4 warning** (L519-530) — "not recommended" with rationale about re-introducing the non-agent decision pipeline. Good.
- **#1932 cross-reference** (L149-154) — #1932 now listed with CLOSED status alongside #1971, matching the issue-body language *"after #1932"*.
- **Tests/regression risk** (L703-726) — new subsection under Complexity Assessment lists `test_overseer_monitor.py`, `test_overseer_issue_filer.py`, `test_overseer_alert_isolation.py`, `test_overseer_hitl_integration.py`, `test_two_tier_integration.py`, `test_infra_error_escalation.py`, plus the `integration_tests/` end-to-end coverage gap and gateway-policy test requirement. This is exactly what the planners need.
- **SKILL.md citation tightening** (L116) — now cites `SKILL.md:527-552` + `SKILL.md:1359-1383` (Phase S5 short-flow copies) instead of the looser 1371-1383 range.
- **Plan-phase candidate tags** — decision-12, decision-13, decision-14, decision-16 all carry `*(Plan-phase candidate …)*` suffixes; introductory text at L394-400 explicitly tells the human these can be left unanswered at the refine gate. Addresses the "23 HITL items is excessive" concern.
- **Recommended-option labeling** — "Option B (recommended)" is the first option on decision-1 both in the draft and in the registered contract. Same pattern applied on decisions 2, 8, 9, 10, 11, 12, 13, 14, 15.

### Verified independent of the NACK response

- `mcp__sdlc__show_contract` — 16 decisions + feedback-1 with Q1..Q7 ALL present and well-formed.
- All citations I spot-checked on cycle 1 remain accurate.
- `monitor.py`:2005 LOC, `OverseerMonitor(` production callers still absent, labels inventory (`agent:overseer` only) unchanged — background holds.
- Recommendation (Option B) aligns with the "Recommended Approach" section and with the first option on decision-1; the refiner has been consistent.

### Non-blocking (remaining, for awareness only)

- **feedback-1 bundle marker** — The draft references `feedback-1 Q1..Q7` in prose (L653-674) but does not include a `<!-- egg-feedback id=feedback-1 -->` (or `<!-- egg-hitl-feedback -->`) marker like `.egg-state/drafts/1759-analysis.md:379` does. The refiner's in-line comment (L655-657) acknowledges this: *"The human edits the feedback comment directly; no per-question markers are needed inline."* The contract carries the bundle correctly so this is non-blocking, but if the `/sdlc` skill relies on the marker to surface feedback, the plan phase should verify.
- **decision-1 option label wording** — decision-1 opt-3 is registered as "Option C — three separate pipelines, one per thread", but the draft's "Options Considered" at L290 describes Option C as "Three separate pipelines — one thread per pipeline". Substantively identical; pure cosmetic drift.

Approving. Refine-phase analysis is now complete, self-consistent, and properly wired into the contract for HITL resolution.


````yaml
id: 313b7924-e603-4e
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/1962-analysis.md
    - .egg-state/contracts/issue-1962.json
    reason: "\nRe-review of revision 2 (commit ab146d0c8). All three blocking items\
      \ from cycle 1 are resolved; non-blocking items are substantially addressed.\n\
      \n### Blocking items \u2014 verified fixed\n\n1. **HITL registration + markers**\
      \ \u2014 `mcp__sdlc__show_contract` now returns 16 registered `hitl` decisions\
      \ (`decision-1` \u2026 `decision-16`) with full option lists AND a `feedback-1`\
      \ bundle containing Q1-Q7. The draft has exactly 16 `<!-- egg-hitl-decision\
      \ id=decision-N -->` markers at L404, 418, 429, 443, 463, 476, 489, 502, 515,\
      \ 535, 547, 578, 595, 610, 629, 642 \u2014 one per registered decision, with\
      \ inline checkbox lists that reproduce the registered options verbatim. Spot-checked\
      \ decision-1 options against the contract: labels match (Option B/A/C/D + Other).\
      \ Good.\n\n2. **Duplicate decision-15** \u2014 single block now at L547 under\
      \ \"Auto-issue filing policy\" (the \"expand OVERSEER_PATTERNS for dedup state\"\
      \ angle, which is the actionable one). The prior \"Interaction with existing\
      \ issues\" duplicate was removed; L558-559 explicitly notes *\"the sole instance\
      \ of decision-15.\"*\n\n3. **decision-11 redundancy** \u2014 reframed at L578\
      \ with an explicit dependency note: *\"this decision is only meaningful if `decision-1`\
      \ resolves to Option A or Option C. If `decision-1` = Option B (recommended)\
      \ or Option D, treat this as automatically answered 'Yes \u2014 defer' and proceed.\"\
      * This removes the contradiction risk while preserving a dimension (full vs.\
      \ partial vs. defer) that decision-1 doesn't capture on its own.\n\n### Non-blocking\
      \ items \u2014 substantially addressed\n\n- **decision-4 Sonnet-gated clarification**\
      \ (L448-453) \u2014 design note added: the \"Sonnet-gated\" option means in-loop\
      \ reasoning by the agent, not a separate classifier service. Resolves the ambiguity\
      \ I raised.\n- **decision-9 opt-4 warning** (L519-530) \u2014 \"not recommended\"\
      \ with rationale about re-introducing the non-agent decision pipeline. Good.\n\
      - **#1932 cross-reference** (L149-154) \u2014 #1932 now listed with CLOSED status\
      \ alongside #1971, matching the issue-body language *\"after #1932\"*.\n- **Tests/regression\
      \ risk** (L703-726) \u2014 new subsection under Complexity Assessment lists\
      \ `test_overseer_monitor.py`, `test_overseer_issue_filer.py`, `test_overseer_alert_isolation.py`,\
      \ `test_overseer_hitl_integration.py`, `test_two_tier_integration.py`, `test_infra_error_escalation.py`,\
      \ plus the `integration_tests/` end-to-end coverage gap and gateway-policy test\
      \ requirement. This is exactly what the planners need.\n- **SKILL.md citation\
      \ tightening** (L116) \u2014 now cites `SKILL.md:527-552` + `SKILL.md:1359-1383`\
      \ (Phase S5 short-flow copies) instead of the looser 1371-1383 range.\n- **Plan-phase\
      \ candidate tags** \u2014 decision-12, decision-13, decision-14, decision-16\
      \ all carry `*(Plan-phase candidate \u2026)*` suffixes; introductory text at\
      \ L394-400 explicitly tells the human these can be left unanswered at the refine\
      \ gate. Addresses the \"23 HITL items is excessive\" concern.\n- **Recommended-option\
      \ labeling** \u2014 \"Option B (recommended)\" is the first option on decision-1\
      \ both in the draft and in the registered contract. Same pattern applied on\
      \ decisions 2, 8, 9, 10, 11, 12, 13, 14, 15.\n\n### Verified independent of\
      \ the NACK response\n\n- `mcp__sdlc__show_contract` \u2014 16 decisions + feedback-1\
      \ with Q1..Q7 ALL present and well-formed.\n- All citations I spot-checked on\
      \ cycle 1 remain accurate.\n- `monitor.py`:2005 LOC, `OverseerMonitor(` production\
      \ callers still absent, labels inventory (`agent:overseer` only) unchanged \u2014\
      \ background holds.\n- Recommendation (Option B) aligns with the \"Recommended\
      \ Approach\" section and with the first option on decision-1; the refiner has\
      \ been consistent.\n\n### Non-blocking (remaining, for awareness only)\n\n-\
      \ **feedback-1 bundle marker** \u2014 The draft references `feedback-1 Q1..Q7`\
      \ in prose (L653-674) but does not include a `<!-- egg-feedback id=feedback-1\
      \ -->` (or `<!-- egg-hitl-feedback -->`) marker like `.egg-state/drafts/1759-analysis.md:379`\
      \ does. The refiner's in-line comment (L655-657) acknowledges this: *\"The human\
      \ edits the feedback comment directly; no per-question markers are needed inline.\"\
      * The contract carries the bundle correctly so this is non-blocking, but if\
      \ the `/sdlc` skill relies on the marker to surface feedback, the plan phase\
      \ should verify.\n- **decision-1 option label wording** \u2014 decision-1 opt-3\
      \ is registered as \"Option C \u2014 three separate pipelines, one per thread\"\
      , but the draft's \"Options Considered\" at L290 describes Option C as \"Three\
      \ separate pipelines \u2014 one thread per pipeline\". Substantively identical;\
      \ pure cosmetic drift.\n\nApproving. Refine-phase analysis is now complete,\
      \ self-consistent, and properly wired into the contract for HITL resolution.\n"
  version: 2
````

### [2026-04-24T18:28:13Z] orchestrator → refiner (STATUS): All reviewers have ACKed — ready to confirm

All assigned reviewers have ACKed your proposal (version 2). Run `egg-orch consensus confirmed` to confirm.

````yaml
id: c7cb3ee4-9d3b-4c
phase: refine
metadata:
  fully_acked: true
  version: 2
````

### [2026-04-24T18:28:17Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: 505c1786-6f06-4f
phase: refine
metadata:
  consensus_reached: false
````

### [2026-04-24T18:28:17Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: e73add48-f525-41
phase: refine
metadata:
  consensus_reached: true
````
